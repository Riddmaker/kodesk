"""Install, update, remove plugins and patches on fake reader trees (B-08, B-09)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kodesk.core import catalogue as cat
from kodesk.core.archive import StagedPlugin, stage_plugin
from kodesk.core.installer import (
    Action,
    InstallerError,
    apply,
    plan_patch,
    plan_plugin,
    plan_remove,
)
from kodesk.core.records import Records
from kodesk.core.snapshot import SnapshotStore, WriteRejectedError, file_sha256
from kodesk.core.sources import Download
from tests.conftest import (
    THIRD_PARTY_PLUGIN,
    USER_PATCH,
    fake_catalogue,
    plugin_files,
    plugin_zip,
)

PROTECTED = ("settings.reader.lua", "history.lua", "settings/vocabulary_builder.sqlite3")


def _entry(name: str) -> cat.Entry:
    return next(e for e in cat.parse(*fake_catalogue()).entries if e.name == name)


def _download(tag: str) -> Download:
    return Download("bob", "readingclock.koplugin", "https://codeload.github.com/x", tag=tag)


def _hashes(reader: Path) -> dict[str, str]:
    return {p: file_sha256(reader / p) for p in PROTECTED}


def _stage(tmp_path: Path, version: str, extra: dict[str, bytes] | None = None) -> StagedPlugin:
    files = {**plugin_files("readingclock.koplugin", version), **(extra or {})}
    return stage_plugin(plugin_zip(files), "readingclock.koplugin", tmp_path / f"stage-{version}")


def test_install_update_remove_keep_the_setup(
    reader: Path, store: SnapshotStore, tmp_path: Path
) -> None:
    records = Records(store.base)
    before = _hashes(reader)
    entry = _entry("readingclock")
    folder = reader / "plugins" / "readingclock.koplugin"

    first = _stage(tmp_path, "1.0", {"readingclock.koplugin/old.lua": b"-- dropped in 2.0\n"})
    change = plan_plugin(reader, first, entry, _download("v1.0"), None)
    assert change.action is Action.INSTALL and len(change.add) == 4 and not change.replace
    apply(store, reader, change, records)
    assert (folder / "lib" / "util.lua").read_bytes() == b"-- util 1.0\n"
    record = records.get(cat.Kind.PLUGIN, "readingclock")
    assert record is not None and record.version == "v1.0" and record.source.startswith("bob/")

    (folder / "cache.db").write_bytes(b"made by the plugin")  # the plugin's own data
    second = _stage(tmp_path, "2.0")
    update = plan_plugin(reader, second, entry, _download("v2.0"), record)
    assert update.action is Action.UPDATE
    assert update.remove == ("plugins/readingclock.koplugin/old.lua",)
    assert update.keep == ("plugins/readingclock.koplugin/cache.db",)
    assert len(update.replace) == 3
    apply(store, reader, update, Records(store.base))
    assert (folder / "main.lua").read_bytes() == b"-- main 2.0\n"
    assert (folder / "cache.db").read_bytes() == b"made by the plugin"
    assert not (folder / "old.lua").exists()

    records = Records(store.base)  # reloaded from installs.json on the PC
    record = records.get(cat.Kind.PLUGIN, "readingclock")
    assert record is not None and record.version == "v2.0"
    removal = plan_remove(reader, cat.Kind.PLUGIN, "readingclock", record)
    assert "settings/" in removal.notes[0]
    apply(store, reader, removal, records)
    assert not folder.exists()
    assert records.get(cat.Kind.PLUGIN, "readingclock") is None
    assert _hashes(reader) == before  # the reader's setup is untouched throughout
    assert (reader / "plugins" / f"{THIRD_PARTY_PLUGIN}.koplugin" / "main.lua").is_file()


def test_update_without_a_record_keeps_every_extra_file(
    reader: Path, store: SnapshotStore, tmp_path: Path
) -> None:
    folder = reader / "plugins" / f"{THIRD_PARTY_PLUGIN}.koplugin"
    (folder / "user-data.lua").write_text("-- kept\n")
    files = plugin_files(f"{THIRD_PARTY_PLUGIN}.koplugin", "9")
    staged = stage_plugin(plugin_zip(files), "x", tmp_path / "s")
    change = plan_plugin(reader, staged, _entry(THIRD_PARTY_PLUGIN), _download("v9"), None)
    assert change.action is Action.UPDATE and not change.remove
    assert f"plugins/{THIRD_PARTY_PLUGIN}.koplugin/user-data.lua" in change.keep
    assert change.notes and "did not install" in change.notes[0]
    apply(store, reader, change, Records(store.base))  # a protected third-party plugin: allowed
    assert (folder / "user-data.lua").read_text() == "-- kept\n"


def test_built_in_plugins_are_never_changed(reader: Path, tmp_path: Path) -> None:
    staged = stage_plugin(plugin_zip(plugin_files("statistics.koplugin")), "x", tmp_path / "s")
    with pytest.raises(InstallerError, match="part of KOReader"):
        plan_plugin(reader, staged, _entry("readingclock"), _download("v1"), None)
    with pytest.raises(InstallerError, match="part of KOReader"):
        plan_remove(reader, cat.Kind.PLUGIN, "statistics", None)
    (reader / "ota" / "package.index").unlink()  # origin unknown: only what kodesk installed
    with pytest.raises(InstallerError, match="cannot tell"):
        plan_remove(reader, cat.Kind.PLUGIN, THIRD_PARTY_PLUGIN, None)


def test_patches(reader: Path, store: SnapshotStore) -> None:
    records = Records(store.base)
    entry = _entry(USER_PATCH)
    change = plan_patch(reader, entry, b"-- newer patch\n", matches_index=False)
    assert change.action is Action.UPDATE and change.replace == (f"patches/{USER_PATCH}",)
    assert any("changed in its repository" in n for n in change.notes)
    apply(store, reader, change, records)
    assert (reader / "patches" / USER_PATCH).read_bytes() == b"-- newer patch\n"
    assert records.get(cat.Kind.PATCH, USER_PATCH) is not None
    odd = cat.Entry(**{**entry.__dict__, "name": "footer.lua"})
    new = plan_patch(reader, odd, b"--\n", matches_index=True)
    assert new.action is Action.INSTALL and "will not run" in new.notes[0]
    apply(store, reader, plan_remove(reader, cat.Kind.PATCH, USER_PATCH, None), records)
    assert not (reader / "patches" / USER_PATCH).exists()
    assert records.get(cat.Kind.PATCH, USER_PATCH) is None
    with pytest.raises(InstallerError, match="unsafe name"):
        plan_patch(reader, cat.Entry(**{**entry.__dict__, "name": "../x.lua"}), b"", None)


def test_a_failing_write_is_undone(reader: Path, store: SnapshotStore, tmp_path: Path) -> None:
    staged = _stage(tmp_path, "1.0")
    change = plan_plugin(reader, staged, _entry("readingclock"), _download("v1"), None)
    (staged.folder / "lib" / "util.lua").write_bytes(b"changed after the preview")
    records = Records(store.base)
    with pytest.raises(WriteRejectedError, match="reads back differently"):
        apply(store, reader, change, records)
    assert not (reader / "plugins" / "readingclock.koplugin").exists()
    assert records.get(cat.Kind.PLUGIN, "readingclock") is None
