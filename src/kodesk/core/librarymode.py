"""The library's display mode: list or mosaic (B-21); no Qt.

KOReader's file browser is drawn by its Cover browser plugin, or by Project: Title when that is
installed and Cover browser is switched off (projecttitle main.lua:26-33 disables itself
otherwise). Both keep the mode in a ``config`` table (key TEXT PRIMARY KEY, value TEXT) of their
own SQLite cache in ``settings/`` and save it with ``INSERT OR REPLACE`` (bookinfomanager.lua
saveSetting); a missing row or NULL means the classic list, an unknown value too
(setupFileManagerDisplayMode). With "unified display mode" (value "Y") a change in the menu also
sets history and collections, so kodesk writes those rows too, like KOReader does.

Checked against KOReader v2026.07.2 (plugins/coverbrowser.koplugin) and Project: Title
2026.07-v3.8.3 (github.com/joshuacant/ProjectTitle).

Writing: SQLite files are never byte-identical after a write, so the check after the write
compares content: every table and row except the changed config rows must be the same as
before (:func:`content_digest`). The write itself runs inside ``guarded_write`` (snapshot first,
restore on any failure) and only while KOReader is closed: the cache must still be the file
kodesk read (size and time), with no journal or WAL file beside it (KOReader may use WAL,
bookinfomanager.lua createDB). Reading opens the file ``immutable``, so it never creates a
file on the reader.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from kodesk.core.luadump import SettingsDocument
from kodesk.core.snapshot import Progress, Snapshot, SnapshotStore, guarded_write, no_progress

FILEMANAGER_KEY = "filemanager_display_mode"
UNIFIED_KEY = "unified_display_mode"
UNIFIED_KEYS = ("history_display_mode", "collection_display_mode")
SIDECARS = ("-journal", "-wal", "-shm")  # SQLite files beside the cache while it is open


@dataclass(frozen=True)
class Drawer:
    """The plugin that draws the library, its cache file and the modes it offers."""

    plugin: str
    title: str
    cache: str  # relative to the KOReader folder
    modes: tuple[tuple[str | None, str], ...]  # (value, KOReader's menu label); None = classic
    first_start: str | None  # the mode it sets on its first start (when no mode is saved)
    first_start_flag: str  # settings.reader.lua key that says that first start happened


COVER_BROWSER = Drawer(
    "coverbrowser",
    "Cover browser",
    "settings/bookinfo_cache.sqlite3",
    (  # coverbrowser main.lua:67-74
        (None, "Classic (filename only)"),
        ("mosaic_image", "Mosaic with cover images"),
        ("mosaic_text", "Mosaic with text covers"),
        ("list_image_meta", "Detailed list with cover images and metadata"),
        ("list_only_meta", "Detailed list with metadata, no images"),
        ("list_image_filename", "Detailed list with cover images and filenames"),
    ),
    "list_image_meta",  # coverbrowser main.lua:86-95
    "coverbrowser_initial_default_setup_done",
)
PROJECT_TITLE = Drawer(
    "projecttitle",
    "Project: Title",
    "settings/PT_bookinfo_cache.sqlite3",
    (  # projecttitle main.lua:163-168 (no classic entry in its menu)
        ("list_image_meta", "Cover List"),
        ("mosaic_image", "Cover Grid"),
        ("list_only_meta", "Details List"),
        ("list_no_meta", "Filenames List"),
    ),
    "list_image_meta",  # projecttitle main.lua:220-237
    "aaaProjectTitle_initial_default_setup_done2",
)
CLASSIC_ONLY = Drawer("", "KOReader (Cover browser off)", "", ((None, "Classic"),), None, "")


class LibraryModeError(Exception):
    """The mode could not be read or written; nothing was changed."""


def _disabled(settings: SettingsDocument, plugin: str) -> bool:
    return bool(settings.get(("plugins_disabled", plugin)))  # as kodesk.core.inventory


def drawer(settings: SettingsDocument, plugins: Iterable[str]) -> Drawer:
    """Which plugin draws the library, given settings.reader.lua and the installed plugins."""
    installed = {p.lower() for p in plugins}
    if not _disabled(settings, "coverbrowser"):
        return COVER_BROWSER
    if "projecttitle" in installed and not _disabled(settings, "projecttitle"):
        return PROJECT_TITLE
    return CLASSIC_ONLY


def kind(mode: str | None) -> str:
    """ "classic", "list" or "mosaic" (main.lua initGrid: the part before the first "_")."""
    return "classic" if not mode else mode.split("_", 1)[0]


def _connect_read_only(path: Path) -> sqlite3.Connection:
    """No locks, no -shm/-wal files: nothing is created on the reader."""
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)


@dataclass(frozen=True)
class CacheState:
    """What the cache file looked like when kodesk read it (cheap: no hashing of covers)."""

    size: int
    mtime_ns: int

    @classmethod
    def of(cls, path: Path) -> CacheState | None:
        if not path.is_file():
            return None
        stat = path.stat()
        return cls(stat.st_size, stat.st_mtime_ns)


def sidecars(path: Path) -> list[str]:
    """Journal / WAL files beside the cache: KOReader has it open (or did not close)."""
    return [path.name + s for s in SIDECARS if path.with_name(path.name + s).exists()]


def read_config(path: Path) -> dict[str, str | None]:
    """The cache's config table (read-only); empty when the file or the table is missing."""
    if not path.is_file():
        return {}
    try:
        with closing(_connect_read_only(path)) as conn:
            rows = conn.execute("SELECT key, value FROM config").fetchall()
    except sqlite3.Error as error:
        if "no such table" in str(error):
            return {}
        raise LibraryModeError(f"{path.name} could not be read: {error}") from error
    return {str(k): (None if v is None else str(v)) for k, v in rows}


def current_mode(drawer_: Drawer, koreader_dir: Path, settings: SettingsDocument) -> str | None:
    """The mode KOReader shows: the saved row, or what the plugin sets on its first start."""
    if not drawer_.cache:
        return None
    config = read_config(koreader_dir / drawer_.cache)
    if (
        settings.get((drawer_.first_start_flag,)) is not True
        and not config.get(FILEMANAGER_KEY)
        and not config.get("history_display_mode")
    ):
        return drawer_.first_start
    mode = config.get(FILEMANAGER_KEY)
    return mode if mode in {m for m, _ in drawer_.modes if m} else None


def label(drawer_: Drawer, mode: str | None) -> str:
    """KOReader's menu label of ``mode`` ("Classic" for none)."""
    return dict(drawer_.modes).get(mode, "Classic")


def _count(config: dict[str, str | None], key: str, default: int) -> int:
    """A page-size setting (coverbrowser main.lua:551-557), within sane bounds."""
    try:
        return max(1, min(int(config.get(key) or default), 30))
    except ValueError:
        return default


@dataclass(frozen=True)
class LibraryLook:
    """Everything the Garage shows and writes about the library's look (read-only)."""

    drawer: Drawer
    mode: str | None
    unified: bool = False  # a change also sets history and collections
    grid: tuple[int, int] = (3, 3)  # mosaic columns × rows in portrait
    per_page: int = 10  # list rows per page in portrait
    read: CacheState | None = None

    def rows(self, mode: str | None) -> dict[str, str | None]:
        """The config rows a change to ``mode`` writes, like the plugin's menu does."""
        keys = (FILEMANAGER_KEY, *UNIFIED_KEYS) if self.unified else (FILEMANAGER_KEY,)
        return dict.fromkeys(keys, mode)


def read_look(
    koreader_dir: Path, settings: SettingsDocument, plugins: Iterable[str]
) -> LibraryLook:
    """The library's look on a reader (reads only; never creates a file)."""
    drawer_ = drawer(settings, plugins)
    if not drawer_.cache:
        return LibraryLook(drawer_, None)
    path = koreader_dir / drawer_.cache
    try:
        config = read_config(path)
    except LibraryModeError:  # damaged: show the classic list, and never write to it
        return LibraryLook(drawer_, None)
    return LibraryLook(
        drawer_,
        current_mode(drawer_, koreader_dir, settings),
        config.get(UNIFIED_KEY) == "Y",
        (_count(config, "nb_cols_portrait", 3), _count(config, "nb_rows_portrait", 3)),
        _count(config, "files_per_page", 10),
        CacheState.of(path),
    )


def content_digest(conn: sqlite3.Connection, skip_config: Iterable[str] = ()) -> str:
    """A hash over the schema and every row of every table, leaving out the ``skip_config``
    rows of the config table: what must not change when only those rows are written."""
    skip = set(skip_config)
    digest = hashlib.sha256()
    tables = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type IN ('table', 'index') ORDER BY name"
    ).fetchall()
    for name, sql in tables:
        digest.update(repr((name, sql)).encode())
        if sql is None or not sql.lstrip().upper().startswith("CREATE TABLE"):
            continue
        quoted = '"' + str(name).replace('"', '""') + '"'
        for row in conn.execute(f"SELECT * FROM {quoted} ORDER BY rowid"):  # noqa: S608
            if name == "config" and row and row[0] in skip:
                continue
            digest.update(repr(row).encode())
    return digest.hexdigest()


def write_rows(path: Path, rows: dict[str, str | None]) -> None:
    """Write ``rows`` into the config table the way the plugin does, then check that nothing
    else in the file changed and the rows read back; raises on any difference."""
    if not path.is_file():
        raise LibraryModeError(f"{path.name} is not on the reader")
    try:
        with closing(sqlite3.connect(path, isolation_level=None)) as conn:
            before = content_digest(conn, rows)
            conn.execute("BEGIN IMMEDIATE")
            conn.executemany(
                "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", rows.items()
            )
            conn.execute("COMMIT")
        with closing(_connect_read_only(path)) as conn:
            after = content_digest(conn, rows)
            config = dict(conn.execute("SELECT key, value FROM config").fetchall())
            saved = {key: config.get(key, "missing") for key in rows}
    except sqlite3.Error as error:
        raise LibraryModeError(f"{path.name} could not be written: {error}") from error
    if after != before:
        raise LibraryModeError(f"other content of {path.name} changed while writing")
    if saved != rows:
        raise LibraryModeError(f"{path.name} reads back differently after writing")


class StaleCacheError(LibraryModeError):
    """The cache changed since kodesk read it, or KOReader has it open."""


def write_mode(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    store: SnapshotStore,
    koreader_dir: Path,
    drawer_: Drawer,
    rows: dict[str, str | None],
    read: CacheState | None,
    progress: Progress = no_progress,
) -> Snapshot:
    """Write ``rows`` (see :meth:`LibraryLook.rows`) inside a snapshot; returns the snapshot."""
    if not drawer_.cache:
        raise LibraryModeError("KOReader draws the library without a plugin: nothing to set")
    path = koreader_dir / drawer_.cache
    if read is None or CacheState.of(path) != read or sidecars(path):
        raise StaleCacheError(
            f"{drawer_.title}'s cache on your reader changed since kodesk read it, or "
            "KOReader still has it open. Close KOReader on the reader, then reopen the reader "
            "in kodesk. Nothing was written."
        )

    def write() -> None:
        write_rows(path, rows)
        left = sidecars(path)
        if left:
            raise LibraryModeError("SQLite left files behind: " + ", ".join(left))

    return guarded_write(store, koreader_dir, write, allowed={drawer_.cache}, progress=progress)
