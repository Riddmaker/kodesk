"""Writing the garage's changes into settings.reader.lua on fake reader trees."""

from __future__ import annotations

from pathlib import Path

import pytest

from kodesk.core.install import read_settings_file
from kodesk.core.settings_write import StaleSettingsError, write_settings
from kodesk.core.snapshot import SnapshotStore


def test_writes_only_the_changed_line_after_a_snapshot(reader: Path, store: SnapshotStore) -> None:
    path = reader / "settings.reader.lua"
    before = read_settings_file(path)
    old_lines = path.read_text().splitlines()
    snapshot = write_settings(store, reader, before, before.set(("copt_font_size",), 26))
    new_lines = path.read_text().splitlines()
    assert [(a, b) for a, b in zip(old_lines, new_lines, strict=True) if a != b] == [
        ('    ["copt_font_size"] = 22,', '    ["copt_font_size"] = 26,')
    ]
    assert store.snapshots() == [snapshot]
    assert not (reader / "settings.reader.lua.kodesk-tmp").exists()


def test_refuses_when_koreader_wrote_in_between(reader: Path, store: SnapshotStore) -> None:
    path = reader / "settings.reader.lua"
    before = read_settings_file(path)
    path.write_text(path.read_text().replace("22", "24"))  # KOReader flushed on exit
    with pytest.raises(StaleSettingsError, match="KOReader still"):
        write_settings(store, reader, before, before.set(("copt_font_size",), 26))
    assert '["copt_font_size"] = 24' in path.read_text()
    assert not store.snapshots()


def test_restore_brings_back_the_old_settings(reader: Path, store: SnapshotStore) -> None:
    path = reader / "settings.reader.lua"
    original = path.read_bytes()
    before = read_settings_file(path)
    snapshot = write_settings(store, reader, before, before.set(("night_mode",), False))
    store.restore(reader, snapshot)
    assert path.read_bytes() == original
