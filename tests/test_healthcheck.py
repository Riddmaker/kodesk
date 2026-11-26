"""Health check after a write (B-10): parse crash.log, only what KOReader logged since then.

The log lines are real excerpts from KOReader issues (#11394, #15417, #16156, #9181, #16020),
except ``Failed to initialize``, which follows the format in ``frontend/pluginloader.lua``.
"""

from __future__ import annotations

from pathlib import Path

from kodesk.core import healthcheck as hc
from kodesk.core.healthcheck import Failure, Report
from kodesk.core.snapshot import SnapshotStore, guarded_write, walk

OLD_SESSION = (
    "09/07/26-15:13:29 INFO  initializing for device PBVerseProColor \n"
    "01/20/24-22:52:55 WARN  Error when loading plugins/terminal.koplugin/main.lua nil \n"
)
NEW_SESSION = (
    "05/19/26-13:41:50 WARN  Error when loading plugins/statistics.koplugin/main.lua: "
    "frontend/apps/filemanager/filemanager.lua:16: module "
    "'apps/filemanager/filemanagerfilesearcher' not found:\n"
    "05/19/26-13:41:50 WARN  Error when loading plugins/kosync.koplugin/main.lua: "
    "plugins/kosync.koplugin/main.lua:3: loop or previous error loading module 'dispatcher' \n"
    "05/19/26-13:42:06 WARN  Error when loading plugins/statistics.koplugin/main.lua: "
    "frontend/apps/filemanager/filemanager.lua:16: module "
    "'apps/filemanager/filemanagerfilesearcher' not found:\n"
    "08/28/26-21:06:52 INFO  Applying patch: ./patches/2-screensaver-chapter.lua \n"
    "08/28/26-21:06:54 INFO  Applying patch: ./patches/2-overlap_arrow_32.lua \n"
    "08/28/26-21:06:54 WARN  Patching failed: ./patches/2-overlap_arrow_32.lua:5: "
    "function arguments expected near '='\n"
    "08/28/26-21:06:55 ERROR Failed to initialize projecttitle plugin: "
    "plugins/projecttitle.koplugin/main.lua:40: attempt to index a nil value\n"
)


def test_parses_plugins_and_patches_once_each() -> None:
    assert hc.parse_failures(NEW_SESSION) == (
        Failure(
            "plugin",
            "statistics",
            "frontend/apps/filemanager/filemanager.lua:16: module "
            "'apps/filemanager/filemanagerfilesearcher' not found:",
        ),
        Failure(
            "plugin",
            "kosync",
            "plugins/kosync.koplugin/main.lua:3: loop or previous error loading module "
            "'dispatcher'",
        ),
        Failure(
            "patch",
            "2-overlap_arrow_32.lua",
            "./patches/2-overlap_arrow_32.lua:5: function arguments expected near '='",
        ),
        Failure(
            "plugin",
            "projecttitle",
            "plugins/projecttitle.koplugin/main.lua:40: attempt to index a nil value",
        ),
    )


def test_plugin_that_returns_nothing() -> None:
    assert hc.parse_failures(OLD_SESSION) == (
        Failure("plugin", "terminal", "main.lua did not return a plugin"),
    )


def test_patch_name_from_the_applying_line_and_paths_with_spaces() -> None:
    log = (
        "11/13/25-21:35:03 INFO  Applying patch: /Users/x/Library/Application Support/koreader/"
        "patches/2-anki modify.lua \n"
        "11/13/25-21:35:03 WARN  Patching failed: attempt to call a nil value\n"
        "11/13/25-21:35:04 WARN  Error when loading /Users/x/Library/Application Support/"
        "koreader/plugins/my plugin.koplugin/main.lua: boom\n"
    )
    assert [(f.kind, f.name) for f in hc.parse_failures(log)] == [
        ("patch", "2-anki modify.lua"),
        ("plugin", "my plugin"),
    ]


def test_long_messages_are_cut() -> None:
    failure = hc.parse_failures("ERROR Failed to initialize x plugin: " + "y" * 500)[0]
    assert len(failure.message) == hc.MAX_MESSAGE and failure.message.endswith("…")


def test_only_the_log_after_the_anchor_counts() -> None:
    old = OLD_SESSION.encode() * 3
    anchor = old[-hc.ANCHOR_SIZE :]
    assert hc.new_log(old + NEW_SESSION.encode(), anchor) == NEW_SESSION.encode()
    assert hc.new_log(old, anchor) == b""
    trimmed = NEW_SESSION.encode()  # launcher kept only the newest 500 kB: all of it is new
    assert hc.new_log(trimmed, anchor) == trimmed
    assert hc.new_log(trimmed, b"") == trimmed


def _log(reader: Path) -> Path:
    return reader / hc.CRASH_LOG


def test_a_good_write_leaves_a_pending_check(reader: Path, store: SnapshotStore) -> None:
    _log(reader).write_text(OLD_SESSION)
    taken = guarded_write(store, reader, lambda: None)
    pending = hc.load_pending(store.base)
    assert pending is not None and pending.snapshot_id == taken.id
    assert hc.check(reader, pending) == Report(started=False)  # KOReader not run yet
    with _log(reader).open("a") as log:
        log.write(NEW_SESSION)
    report = hc.check(reader, pending)
    assert report.started and len(report.failures) == 4
    hc.clear_pending(store.base)
    assert hc.load_pending(store.base) is None


def test_clean_start_reports_no_failures(reader: Path, store: SnapshotStore) -> None:
    pending = hc.remember(store.base, reader, "20261126T130000Z")  # no crash.log yet
    assert hc.check(reader, pending) == Report(started=False)
    _log(reader).write_text("11/26/26-14:00:00 INFO  initializing for device PBVerseProColor\n")
    assert hc.check(reader, pending) == Report(started=True)


def test_crash_log_is_not_part_of_a_snapshot(reader: Path) -> None:
    _log(reader).write_text(OLD_SESSION)
    (reader / "crash.log.new").write_text("")
    assert hc.CRASH_LOG not in walk(reader) and "crash.log.new" not in walk(reader)


def test_damaged_pending_file_is_ignored(tmp_path: Path) -> None:
    for content in ("{", '{"snapshot": 1, "anchor": ""}', '{"snapshot": "x", "anchor": "%%"}'):
        (tmp_path / hc.PENDING_FILE).write_text(content)
        assert hc.load_pending(tmp_path) is None
    assert hc.load_pending(tmp_path / "missing") is None
