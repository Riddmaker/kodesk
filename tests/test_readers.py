"""Readers plugged in and out while kodesk runs (B-03), with fake volumes. Never the real reader."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from PySide6.QtCore import QStandardPaths
from PySide6.QtWidgets import QApplication, QSlider, QWidget
from pytestqt.qtbot import QtBot

from kodesk.core.install import KOReaderInstall, find_install
from kodesk.core.snapshot import SnapshotError, SnapshotStore
from kodesk.ui.app import MainWindow, demo_context
from kodesk.ui.readers import ReaderWatcher, reader_name
from tests.conftest import make_pocketbook

pytestmark = pytest.mark.gui


class Mounts:
    """What the fake system has mounted: volumes that hold a KOReader installation."""

    def __init__(self) -> None:
        self.volumes: list[Path] = []

    def __call__(self) -> list[KOReaderInstall]:
        return [find_install(v) for v in self.volumes if v.is_dir()]


@pytest.fixture(name="mounts")
def fixture_mounts() -> Mounts:
    return Mounts()


@pytest.fixture(name="window")
def fixture_window(qtbot: QtBot) -> MainWindow:
    QStandardPaths.setTestModeEnabled(True)  # never the user's real app data folder
    window = MainWindow(demo_context(), offline=True)
    qtbot.addWidget(window)
    return window


def _plug(mounts: Mounts, volume: Path) -> Path:
    make_pocketbook(volume)
    mounts.volumes.append(volume)
    return volume


def _unplug(volume: Path, parking: Path) -> None:
    shutil.move(volume, parking)  # the mount point disappears with the volume


def _menu_texts(watcher: ReaderWatcher) -> list[str]:
    return [a.text() for a in watcher.menu.actions()]


def test_plugging_in_opens_the_reader(window: MainWindow, mounts: Mounts, tmp_path: Path) -> None:
    watcher = window.watch_readers(mounts)
    assert window.install is None and _menu_texts(watcher) == ["No reader connected"]
    _plug(mounts, tmp_path / "PB634K3")
    watcher.poll()
    assert window.install is not None and window.install.root.parent.parent.name == "PB634K3"
    assert _menu_texts(watcher) == ["&1 PocketBook Verse Pro Color (PB634K3)"]
    assert watcher.menu.actions()[0].isChecked()


def test_unplugging_goes_back_to_the_demo(
    window: MainWindow, mounts: Mounts, tmp_path: Path
) -> None:
    watcher = window.watch_readers(mounts)
    volume = _plug(mounts, tmp_path / "PB634K3")
    watcher.poll()
    assert window.install is not None
    _unplug(volume, tmp_path / "away")
    watcher.poll()
    assert window.install is None
    assert window.statusBar().currentMessage() == "Reader disconnected."
    shutil.move(tmp_path / "away", volume)  # plugged in again
    watcher.poll()
    assert window.install is not None


def test_unwritten_changes_keep_the_reader_until_it_is_back(
    window: MainWindow, mounts: Mounts, tmp_path: Path
) -> None:
    watcher = window.watch_readers(mounts)
    volume = _plug(mounts, tmp_path / "PB634K3")
    watcher.poll()
    slider = window.garage.findChild(QSlider, "font-size")
    assert slider is not None
    slider.setValue(slider.value() + 2)
    shown = window.install
    _unplug(volume, tmp_path / "away")
    watcher.poll()
    assert window.install is shown and window.garage.changed()
    assert "Your changes are kept" in window.statusBar().currentMessage()
    shutil.move(tmp_path / "away", volume)
    watcher.poll()
    assert window.install is shown and window.garage.changed()
    assert window.statusBar().currentMessage().endswith("is connected again.")


def test_a_second_reader_is_offered_not_switched_to(
    window: MainWindow, mounts: Mounts, tmp_path: Path
) -> None:
    watcher = window.watch_readers(mounts)
    _plug(mounts, tmp_path / "PB634K3")
    watcher.poll()
    first = window.install
    _plug(mounts, tmp_path / "PB700")
    watcher.poll()
    assert window.install is first
    assert "Reader → Connected readers" in window.statusBar().currentMessage()
    second = watcher.menu.actions()[1]
    assert second.text().endswith("(PB700)") and not second.isChecked()
    second.trigger()
    assert window.install is not None and window.install.root.parent.parent.name == "PB700"
    assert [a.isChecked() for a in watcher.menu.actions()] == [False, True]


def test_readers_mounted_at_start_are_not_opened_again(
    window: MainWindow, mounts: Mounts, tmp_path: Path
) -> None:
    _plug(mounts, tmp_path / "PB634K3")  # e.g. shown from the start, or not shown on purpose
    watcher = window.watch_readers(mounts)
    watcher.poll()
    assert window.install is None and len(watcher.menu.actions()) == 1


def test_nothing_changes_while_a_dialog_is_open(
    window: MainWindow, mounts: Mounts, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    watcher = window.watch_readers(mounts)
    _plug(mounts, tmp_path / "PB634K3")
    monkeypatch.setattr(QApplication, "activeModalWidget", staticmethod(QWidget))
    watcher.poll()
    assert window.install is None
    monkeypatch.undo()
    watcher.poll()
    assert window.install is not None


def test_unknown_model_is_named_not_opened(
    window: MainWindow, mounts: Mounts, tmp_path: Path
) -> None:
    watcher = window.watch_readers(mounts)
    volume = _plug(mounts, tmp_path / "KOBO")
    (volume / "applications" / "koreader" / "version.log").write_text("x,SomeOtherReader\n")
    watcher.poll()
    assert window.install is None
    assert "SomeOtherReader has no device definition" in window.statusBar().currentMessage()


def test_reader_name_without_a_known_model(tmp_path: Path) -> None:
    volume = make_pocketbook(tmp_path / "READER")
    (volume / "applications" / "koreader" / "version.log").unlink()
    assert reader_name(find_install(volume)) == "Reader (READER)"


def test_no_snapshot_of_a_reader_that_is_gone(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / "snapshots")
    with pytest.raises(SnapshotError, match="not connected"):
        store.take(tmp_path / "unplugged" / "applications" / "koreader")
    assert not store.snapshots()
