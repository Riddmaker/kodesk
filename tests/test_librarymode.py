"""The library's display mode (B-21) on fake Cover browser / Project: Title caches."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from kodesk.core import librarymode as lm
from kodesk.core.luadump import SettingsDocument
from kodesk.core.snapshot import SnapshotStore, WriteRejectedError

SCHEMA = """
CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE bookinfo (bcid INTEGER PRIMARY KEY, directory TEXT, filename TEXT, title TEXT,
                       cover_bb_data BLOB);
CREATE UNIQUE INDEX dir_filename ON bookinfo(directory, filename);
"""


def make_cache(path: Path, config: dict[str, str | None], wal: bool = False) -> Path:
    """A cache like bookinfomanager.lua's: a config table and a book table with a cover blob."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn:
        if wal:
            conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        conn.executemany("INSERT INTO config VALUES (?, ?)", config.items())
        conn.execute(
            "INSERT INTO bookinfo VALUES (1, '/mnt/ext1/Books/', 'a.epub', 'A', ?)",
            (b"\x00cover",),
        )
        conn.commit()
    return path


def settings(text: str = "") -> SettingsDocument:
    return SettingsDocument.parse(f"return {{\n{text}\n}}")


DONE = '["coverbrowser_initial_default_setup_done"] = true,'
CB_OFF = '["plugins_disabled"] = { ["coverbrowser"] = true, },'


def test_drawer_follows_plugins_disabled() -> None:
    assert lm.drawer(settings(), ["projecttitle"]) is lm.COVER_BROWSER  # PT switches itself off
    assert lm.drawer(settings(CB_OFF), ["projecttitle"]) is lm.PROJECT_TITLE
    assert lm.drawer(settings(CB_OFF), ["ProjectTitle"]) is lm.PROJECT_TITLE
    assert lm.drawer(settings(CB_OFF), []) is lm.CLASSIC_ONLY
    both_off = '["plugins_disabled"] = { ["coverbrowser"] = true, ["projecttitle"] = true, },'
    assert lm.drawer(settings(both_off), ["projecttitle"]) is lm.CLASSIC_ONLY


def test_kind_and_label() -> None:
    assert lm.kind(None) == "classic"
    assert lm.kind("mosaic_text") == "mosaic"
    assert lm.kind("list_image_meta") == "list"
    assert lm.label(lm.PROJECT_TITLE, "mosaic_image") == "Cover Grid"
    assert lm.label(lm.COVER_BROWSER, None) == "Classic (filename only)"


def test_current_mode_reads_the_saved_row(reader: Path) -> None:
    make_cache(reader / lm.COVER_BROWSER.cache, {"filemanager_display_mode": "mosaic_image"})
    assert lm.current_mode(lm.COVER_BROWSER, reader, settings(DONE)) == "mosaic_image"


def test_current_mode_classic_for_null_or_unknown(reader: Path) -> None:
    cache = make_cache(reader / lm.COVER_BROWSER.cache, {"filemanager_display_mode": None})
    assert lm.current_mode(lm.COVER_BROWSER, reader, settings(DONE)) is None
    cache.unlink()
    make_cache(cache, {"filemanager_display_mode": "list_no_meta"})  # Project: Title's only
    assert lm.current_mode(lm.COVER_BROWSER, reader, settings(DONE)) is None


def test_current_mode_before_the_first_start(reader: Path) -> None:
    """No cache yet and no first-start flag: KOReader will start with its default."""
    assert lm.current_mode(lm.COVER_BROWSER, reader, settings()) == "list_image_meta"
    assert lm.current_mode(lm.COVER_BROWSER, reader, settings(DONE)) is None


def test_reading_creates_no_file_on_the_reader(reader: Path) -> None:
    cache = make_cache(reader / lm.COVER_BROWSER.cache, {}, wal=True)
    before = sorted(p.name for p in cache.parent.iterdir())
    lm.read_config(cache)
    assert sorted(p.name for p in cache.parent.iterdir()) == before


PT = CB_OFF + '\n["aaaProjectTitle_initial_default_setup_done2"] = true,'


def test_read_look_with_unified_mode_and_page_sizes(reader: Path) -> None:
    cache = make_cache(
        reader / lm.PROJECT_TITLE.cache,
        {
            "unified_display_mode": "Y",
            "filemanager_display_mode": "mosaic_image",
            "nb_cols_portrait": "4",
            "nb_rows_portrait": "junk",
            "files_per_page": "500",
        },
    )
    look = lm.read_look(reader, settings(PT), ["projecttitle"])
    assert look.drawer is lm.PROJECT_TITLE
    assert look.mode == "mosaic_image"
    assert (look.grid, look.per_page) == ((4, 3), 30)
    assert look.read == lm.CacheState.of(cache)
    assert look.rows("list_only_meta") == {
        "filemanager_display_mode": "list_only_meta",
        "history_display_mode": "list_only_meta",
        "collection_display_mode": "list_only_meta",
    }


def test_read_look_without_unified_mode_or_plugin(reader: Path) -> None:
    make_cache(reader / lm.COVER_BROWSER.cache, {"unified_display_mode": None})
    look = lm.read_look(reader, settings(DONE), [])
    assert look.rows(None) == {"filemanager_display_mode": None}
    assert (look.grid, look.per_page) == ((3, 3), 10)
    assert lm.read_look(reader, settings(CB_OFF), []) == lm.LibraryLook(lm.CLASSIC_ONLY, None)


@pytest.mark.parametrize("wal", [False, True])
def test_write_changes_only_the_mode_rows(reader: Path, store: SnapshotStore, wal: bool) -> None:
    cache = make_cache(
        reader / lm.COVER_BROWSER.cache,
        {"filemanager_display_mode": "list_image_meta", "series_mode": "x"},
        wal,
    )
    with closing(sqlite3.connect(cache)) as conn:
        untouched = lm.content_digest(conn, ["filemanager_display_mode"])
    look = lm.read_look(reader, settings(DONE), [])
    snapshot = lm.write_mode(store, reader, look.drawer, look.rows("mosaic_image"), look.read)
    assert lm.read_config(cache) == {
        "filemanager_display_mode": "mosaic_image",
        "series_mode": "x",
    }
    with closing(sqlite3.connect(cache)) as conn:
        assert lm.content_digest(conn, ["filemanager_display_mode"]) == untouched
    assert not lm.sidecars(cache)
    assert store.snapshots() == [snapshot]


def test_write_classic_saves_null(reader: Path, store: SnapshotStore) -> None:
    cache = make_cache(reader / lm.COVER_BROWSER.cache, {"filemanager_display_mode": "mosaic_text"})
    look = lm.read_look(reader, settings(DONE), [])
    lm.write_mode(store, reader, look.drawer, look.rows(None), look.read)
    assert lm.read_config(cache) == {"filemanager_display_mode": None}


def test_refuses_when_the_cache_changed(reader: Path, store: SnapshotStore) -> None:
    cache = make_cache(reader / lm.COVER_BROWSER.cache, {})
    read = lm.CacheState.of(cache)
    with closing(sqlite3.connect(cache)) as conn:  # KOReader indexed a book in between
        conn.execute("INSERT INTO bookinfo (directory, filename) VALUES ('/b/', 'b.epub')")
        conn.commit()
    with pytest.raises(lm.StaleCacheError, match="Nothing was written"):
        lm.write_mode(store, reader, lm.COVER_BROWSER, {"filemanager_display_mode": "x"}, read)
    assert not store.snapshots()


def test_refuses_while_koreader_has_it_open(reader: Path, store: SnapshotStore) -> None:
    cache = make_cache(reader / lm.COVER_BROWSER.cache, {})
    read = lm.CacheState.of(cache)
    cache.with_name(cache.name + "-wal").write_bytes(b"")
    with pytest.raises(lm.StaleCacheError):
        lm.write_mode(store, reader, lm.COVER_BROWSER, {"filemanager_display_mode": "x"}, read)
    assert not store.snapshots()


def test_refuses_without_a_cache(reader: Path, store: SnapshotStore) -> None:
    with pytest.raises(lm.StaleCacheError):
        lm.write_mode(store, reader, lm.COVER_BROWSER, {"filemanager_display_mode": "x"}, None)
    with pytest.raises(lm.LibraryModeError, match="without a plugin"):
        lm.write_mode(store, reader, lm.CLASSIC_ONLY, {}, None)


def test_a_failed_check_restores_the_snapshot(
    reader: Path, store: SnapshotStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = make_cache(reader / lm.COVER_BROWSER.cache, {"filemanager_display_mode": None})
    original = cache.read_bytes()

    def broken(conn: sqlite3.Connection, _skip: object = ()) -> str:
        return str(conn.total_changes) + str(id(conn))  # differs before and after

    monkeypatch.setattr(lm, "content_digest", broken)
    with pytest.raises(WriteRejectedError, match="undone"):
        lm.write_mode(
            store,
            reader,
            lm.COVER_BROWSER,
            {"filemanager_display_mode": "mosaic_image"},
            lm.CacheState.of(cache),
        )
    assert cache.read_bytes() == original


def test_content_digest_sees_other_changes(tmp_path: Path) -> None:
    cache = make_cache(tmp_path / "c.sqlite3", {"filemanager_display_mode": "a", "k": "v"})
    with closing(sqlite3.connect(cache)) as conn:
        before = lm.content_digest(conn, ["filemanager_display_mode"])
        conn.execute("UPDATE config SET value = 'b' WHERE key = 'filemanager_display_mode'")
        assert lm.content_digest(conn, ["filemanager_display_mode"]) == before
        conn.execute("UPDATE config SET value = 'w' WHERE key = 'k'")
        assert lm.content_digest(conn, ["filemanager_display_mode"]) != before
        conn.execute("UPDATE config SET value = 'v' WHERE key = 'k'")
        conn.execute("UPDATE bookinfo SET cover_bb_data = x'01'")
        assert lm.content_digest(conn, ["filemanager_display_mode"]) != before


def test_a_damaged_cache_is_shown_but_never_written(reader: Path, store: SnapshotStore) -> None:
    cache = reader / lm.COVER_BROWSER.cache
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(b"not a database" * 100)
    look = lm.read_look(reader, settings(DONE), [])
    assert look == lm.LibraryLook(lm.COVER_BROWSER, None)
    with pytest.raises(lm.StaleCacheError):
        lm.write_mode(store, reader, look.drawer, look.rows("mosaic_image"), look.read)
