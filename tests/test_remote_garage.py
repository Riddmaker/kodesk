"""Tests for kodesk.core.remote_garage and the reader patch it sends (B-26).

The patch itself runs under LuaJIT with stand-ins for KOReader's modules (tests/lua/run_patch.lua)
against a fake data folder in tmp; never on a reader.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest

from kodesk.core import remote_garage as rg
from kodesk.core.hotcorners import GESTURES_FILE
from kodesk.core.luadump import LuaTable, SettingsDocument, dump_file
from kodesk.core.remote_garage import Change, RemoteGarageError

VERSION = "0.1.0.dev0"
RUNNER = Path(__file__).parent / "lua" / "run_patch.lua"
LUAJIT = shutil.which("luajit")


def _lua_ready() -> bool:
    if LUAJIT is None:
        return False
    probe = 'require("cjson"); require("lsqlite3"); require("lfs")'
    return subprocess.run([LUAJIT, "-e", probe], capture_output=True, check=False).returncode == 0


needs_lua = pytest.mark.skipif(
    not _lua_ready(), reason="luajit with cjson, lsqlite3, lfs is in the nix dev shell"
)

SETTINGS: LuaTable = {
    "copt_font_size": 22,
    "cre_font": "Noto Serif",
    "night_mode": False,
    "footer": {"clock": True, "order": {1: "time", 2: "pages"}},
    "lastfile": "/mnt/ext1/Books/a.epub",
    "koassistant_api_key": "secret-stays-put",
}
GESTURES: LuaTable = {
    "gesture_reader": {"tap_top_left_corner": {"toggle_bookmark": True}},
    "gesture_fm": {"tap_top_left_corner": {"show_menu": True}},
}


# --------------------------------------------------------------------------- pure logic


def test_values_travel_as_json_and_back() -> None:
    value: LuaTable = {1: "time", 2: "pages", "on": True, "size": 1.5, "inner": {"x": 0}}
    encoded = rg.encode_value(value)
    assert encoded["table"][0] == [1, "time"]
    assert rg.decode_value(json.loads(json.dumps(encoded))) == value


@pytest.mark.parametrize(
    "value",
    [float("nan"), "\udcff", "x" * (rg.MAX_STRING + 1), {True: 1}, {1.5: 1}],
)
def test_values_that_cannot_travel_are_refused(value: object) -> None:
    with pytest.raises(RemoteGarageError):
        rg.encode_value(value)  # type: ignore[arg-type]


def test_settings_changes_are_leaves_with_old_and_new() -> None:
    after: LuaTable = {
        **SETTINGS,
        "copt_font_size": 24,
        "footer": {"order": {1: "time", 2: "pages"}},
    }
    changes = rg.settings_changes(rg.SETTINGS_FILE, SETTINGS, after)
    assert changes == [
        Change("reader", ("copt_font_size",), 22, 24),
        Change("reader", ("footer", "clock"), True, None),
    ]
    assert changes[1].to_json() == {"target": "reader", "path": ["footer", "clock"], "old": True}


def test_only_the_garages_keys_are_sent() -> None:
    with pytest.raises(RemoteGarageError, match="lastfile"):
        rg.settings_changes(rg.SETTINGS_FILE, SETTINGS, {**SETTINGS, "lastfile": "/b.epub"})
    with pytest.raises(RemoteGarageError):
        rg.settings_changes("history.lua", {}, {"x": 1})
    with pytest.raises(RemoteGarageError, match="gesture_fm"):
        rg.settings_changes(GESTURES_FILE, GESTURES, {**GESTURES, "gesture_fm": {}})


def test_merge_keeps_the_readers_old_value_and_the_newest_new_one() -> None:
    first = [Change("reader", ("copt_font_size",), 22, 24)]
    merged = rg.merge(first, [Change("reader", ("copt_font_size",), 24, 26)])
    assert merged == [Change("reader", ("copt_font_size",), 22, 26)]
    assert not rg.merge(merged, [Change("reader", ("copt_font_size",), 26, 22)])


def test_merge_keeps_order_between_a_table_and_its_keys() -> None:
    pending = [
        Change("gestures", ("gesture_reader", "tap", "a"), True, None),
        Change("gestures", ("gesture_reader", "tap"), {"b": True}, None),
    ]
    later = Change("gestures", ("gesture_reader", "tap", "a"), None, True)
    assert rg.merge(pending, [later]) == [*pending, later]


def test_merge_tells_true_from_one() -> None:
    assert rg.merge([], [Change("reader", ("night_mode",), 1, True)])


def test_pending_document_round_trip_and_limits() -> None:
    changes = [
        Change("reader", ("footer",), None, {"clock": True}),
        Change("library", (rg.LIBRARY_DBS[0], "filemanager_display_mode"), None, "mosaic_image"),
    ]
    data = rg.pending_document("20261127T100000Z-abc123", changes)
    assert rg.read_pending(data) == changes
    with pytest.raises(RemoteGarageError):
        rg.pending_document("x", [])
    many = [Change("reader", ("copt_font_size",), i, i + 1) for i in range(rg.MAX_CHANGES + 1)]
    with pytest.raises(RemoteGarageError, match="too many"):
        rg.pending_document("x", many)


@pytest.mark.parametrize(
    "raw",
    [
        {"target": "reader", "path": ["lastfile"], "new": "x"},
        {"target": "reader", "path": [], "new": 1},
        {"target": "reader", "path": ["cre_font"]},
        {"target": "reader", "path": ["cre_font"], "new": None},
        {"target": "reader", "path": ["cre_font"], "new": "x", "extra": 1},
        {"target": "library", "db": "settings/other.sqlite3", "key": "filemanager_display_mode"},
        {"target": "library", "db": rg.LIBRARY_DBS[0], "key": "nb_cols_portrait", "new": "9"},
        {"target": "shell", "path": ["x"], "new": 1},
    ],
)
def test_files_from_the_reader_are_checked(raw: dict[str, object]) -> None:
    with pytest.raises(RemoteGarageError):
        Change.from_json(raw)


def test_result_accepts_luajsons_empty_objects() -> None:
    data = json.dumps(
        {"format": 1, "id": "a-1", "applied": {}, "already": {}, "skipped": {}, "restored": False}
    ).encode()
    assert rg.read_result(data) == rg.Result("a-1")
    where = '{"format": 1, "error": "/x/1-kodesk-apply.lua:12: /y/run.lua:3: no table"}'
    assert rg.read_result(where.encode()).error == "no table"


def test_applied_changes_are_checked_against_the_files_read_later() -> None:
    change = Change("reader", ("copt_font_size",), 22, 24)
    assert not rg.not_as_sent([change], {rg.SETTINGS_FILE: {"copt_font_size": 24}})
    assert rg.not_as_sent([change], {rg.SETTINGS_FILE: {"copt_font_size": 23}}) == [change]


def test_patch_has_the_allow_list_and_no_way_out() -> None:
    source = rg.patch_source(VERSION).decode()
    assert f"kodesk {VERSION}" in source
    assert '["copt_font_size"] = true' in source
    assert '["gesture_reader"] = true' in source
    assert "lastfile" not in source
    assert set(re.findall(r'require\("([^"]+)"\)', source)) == {
        "libs/libkoreader-lfs",
        "datastorage",
        "logger",
        "json",
        "luasettings",
        "lua-ljsqlite3/init",
    }
    for forbidden in ("os.execute", "io.popen", "loadstring", "load(", "dofile", "socket", "ffi"):
        assert forbidden not in source


def test_patch_state(tmp_path: Path) -> None:
    assert rg.patch_state(tmp_path, VERSION) == "missing"
    (tmp_path / "patches").mkdir()
    (tmp_path / rg.PATCH).write_bytes(rg.patch_source(VERSION))
    assert rg.patch_state(tmp_path, VERSION) == "current"
    assert rg.patch_state(tmp_path, "0.2.0") == "other"


# --------------------------------------------------------------------------- the patch on LuaJIT


def _reader(tmp_path: Path) -> Path:
    data = tmp_path / "koreader"
    (data / "settings").mkdir(parents=True)
    (data / "kodesk").mkdir()
    (data / "settings.reader.lua").write_text(dump_file(SETTINGS, "./settings.reader.lua"))
    (data / GESTURES_FILE).write_text(dump_file(GESTURES, "./settings/gestures.lua"))
    with sqlite3.connect(data / rg.LIBRARY_DBS[0]) as conn:
        conn.execute("CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO config VALUES ('filemanager_display_mode', 'list_image_meta')")
        conn.execute("INSERT INTO config VALUES ('nb_cols_portrait', '3')")
    conn.close()
    return data


def _run(tmp_path: Path, data: Path, changes: list[Change] | None, **env: str) -> dict[str, Any]:
    patch = tmp_path / rg.PATCH_NAME
    patch.write_bytes(rg.patch_source(VERSION))
    if changes is not None:
        (data / rg.PENDING).write_bytes(rg.pending_document("20261127T100000Z-abc123", changes))
    assert LUAJIT is not None
    subprocess.run(
        [LUAJIT, str(RUNNER), str(patch), str(data)],
        capture_output=True,
        check=True,
        timeout=30,
        env={**os.environ, **env},
    )
    result = data / rg.RESULT
    return json.loads(result.read_bytes()) if result.exists() else {}


def _settings(data: Path, name: str = rg.SETTINGS_FILE) -> LuaTable:
    return SettingsDocument.parse((data / name).read_bytes()).data


def _config(data: Path) -> dict[str, str | None]:
    with sqlite3.connect(data / rg.LIBRARY_DBS[0]) as conn:
        rows = dict(conn.execute("SELECT key, value FROM config"))
    conn.close()
    return rows


@needs_lua
def test_without_a_change_file_the_patch_does_nothing(tmp_path: Path) -> None:
    data = _reader(tmp_path)
    before = (data / "settings.reader.lua").read_bytes()
    assert _run(tmp_path, data, None) == {}
    assert (data / "settings.reader.lua").read_bytes() == before
    assert not sorted(p.name for p in (data / "kodesk").iterdir())


@needs_lua
def test_the_patch_applies_exactly_the_changes(tmp_path: Path) -> None:
    data = _reader(tmp_path)
    after_settings: LuaTable = {
        **SETTINGS,
        "copt_font_size": 26,
        "footer": {"order": {1: "time", 2: "pages"}},
    }
    after_gestures = {**GESTURES, "gesture_reader": {}}
    changes = [
        *rg.settings_changes(rg.SETTINGS_FILE, SETTINGS, after_settings),
        Change("reader", ("night_mode",), False, True),
        *rg.settings_changes(GESTURES_FILE, GESTURES, after_gestures),
        *rg.library_changes(
            rg.LIBRARY_DBS[0], _config(data), {"filemanager_display_mode": "mosaic_image"}
        ),
    ]
    result = rg.read_result(json.dumps(_run(tmp_path, data, changes)).encode())
    assert result.error is None
    assert list(result.applied) == changes
    assert _settings(data) == {**after_settings, "night_mode": True}
    assert _settings(data, GESTURES_FILE) == after_gestures
    assert _config(data) == {"filemanager_display_mode": "mosaic_image", "nb_cols_portrait": "3"}
    assert not (data / rg.PENDING).exists()
    assert sorted(p.name for p in (data / "kodesk" / "backup").iterdir()) == [
        "20261127T100000Z-abc123.settings.reader.lua",
        "20261127T100000Z-abc123.settings_gestures.lua",
    ]


@needs_lua
def test_a_key_changed_on_the_reader_is_left_alone(tmp_path: Path) -> None:
    data = _reader(tmp_path)
    changes = [
        Change("reader", ("copt_font_size",), 20, 26),  # the reader has 22 by now
        Change("reader", ("cre_font",), "Noto Serif", "Literata"),
        Change("reader", ("night_mode",), True, False),  # already so
    ]
    result = rg.read_result(json.dumps(_run(tmp_path, data, changes)).encode())
    assert result.applied == (changes[1],)
    assert result.already == (changes[2],)
    assert result.skipped == ((changes[0], "changed on the reader"),)
    assert _settings(data) == {**SETTINGS, "cre_font": "Literata"}


@needs_lua
@pytest.mark.parametrize(
    "pending",
    [
        b"not json",
        b'{"format": 1, "id": "a-1", "changes": [{"target": "reader", "path": ["lastfile"], '
        b'"old": "/mnt/ext1/Books/a.epub", "new": "/x"}]}',
        b'{"format": 1, "id": "a-1", "changes": [{"target": "reader", "path": ["cre_font"], '
        b'"old": "Noto Serif", "new": null}]}',
        b'{"format": 2, "id": "a-1", "changes": []}',
        b'{"format": 1, "id": "../x", "changes": []}',
    ],
)
def test_a_change_file_out_of_bounds_changes_nothing(tmp_path: Path, pending: bytes) -> None:
    data = _reader(tmp_path)
    before = (data / "settings.reader.lua").read_bytes()
    (data / rg.PENDING).write_bytes(pending)
    result = _run(tmp_path, data, None)
    assert result["error"]
    assert (data / "settings.reader.lua").read_bytes() == before
    assert not (data / rg.PENDING).exists()
    assert (data / rg.REJECTED).read_bytes() == pending


@needs_lua
def test_a_write_that_reads_back_wrong_is_undone(tmp_path: Path) -> None:
    data = _reader(tmp_path)
    before = (data / "settings.reader.lua").read_bytes()
    library = rg.library_changes(
        rg.LIBRARY_DBS[0], _config(data), {"filemanager_display_mode": None}
    )
    changes = [Change("reader", ("copt_font_size",), 22, 26), *library]
    result = _run(tmp_path, data, changes, KODESK_TEST_BAD_FLUSH="1")
    assert "reads back differently" in result["error"]
    assert result["restored"] is True
    assert (data / "settings.reader.lua").read_bytes() == before
    assert _config(data)["filemanager_display_mode"] == "list_image_meta"
    assert (data / rg.REJECTED).exists()


@needs_lua
def test_a_broken_library_cache_undoes_the_settings_too(tmp_path: Path) -> None:
    data = _reader(tmp_path)
    before = (data / "settings.reader.lua").read_bytes()
    (data / rg.LIBRARY_DBS[0]).write_bytes(b"not a database")
    changes = [
        Change("reader", ("copt_font_size",), 22, 26),
        Change("library", (rg.LIBRARY_DBS[0], "filemanager_display_mode"), None, "mosaic_image"),
    ]
    result = _run(tmp_path, data, changes)
    assert result["error"] and result["restored"] is True
    assert (data / "settings.reader.lua").read_bytes() == before


@needs_lua
def test_running_again_after_a_crash_finds_the_changes_already_made(tmp_path: Path) -> None:
    data = _reader(tmp_path)
    changes = [Change("reader", ("copt_font_size",), 22, 26)]
    _run(tmp_path, data, changes)
    result = rg.read_result(json.dumps(_run(tmp_path, data, changes)).encode())
    assert result.already == tuple(changes) and not result.applied


@needs_lua
def test_only_the_last_three_backups_are_kept(tmp_path: Path) -> None:
    data = _reader(tmp_path)
    for i in range(5):
        (data / rg.PENDING).write_bytes(
            rg.pending_document(
                f"2026112{i}T100000Z-a", [Change("reader", ("copt_font_size",), 22 + i, 23 + i)]
            )
        )
        _run(tmp_path, data, None)
    names = sorted(p.name.split(".")[0] for p in (data / "kodesk" / "backup").iterdir())
    assert names == ["20261122T100000Z-a", "20261123T100000Z-a", "20261124T100000Z-a"]
