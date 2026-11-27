"""B-28 in the GUI: a known reader without KOReader is plugged in; kodesk offers to install it."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QStandardPaths, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox
from pytestqt.qtbot import QtBot

from kodesk.core.fresh_install import BareReader, FreshPlan, find_bare
from kodesk.core.install import InstallError, KOReaderInstall, find_install
from kodesk.ui.app import MainWindow, demo_context
from kodesk.ui.koinstall import KOReaderInstaller, offer_text, preview_text
from tests.conftest import RELEASE, FakeGitHub
from tests.test_fresh_install import LABEL, bare_volume, github_with_release

pytestmark = pytest.mark.gui


class Volumes:
    """What the fake system has mounted, as the two listers see it."""

    def __init__(self) -> None:
        self.mounted: list[Path] = []

    def readers(self) -> list[KOReaderInstall]:
        found = []
        for volume in self.mounted:
            try:
                found.append(find_install(volume))
            except InstallError:
                continue
        return found

    def bare(self) -> list[BareReader]:
        found = (find_bare(volume, volume.name) for volume in self.mounted)
        return [reader for reader in found if reader is not None]


@pytest.fixture(name="window")
def fixture_window(qtbot: QtBot, tmp_path: Path) -> tuple[MainWindow, FakeGitHub]:
    QStandardPaths.setTestModeEnabled(True)  # never the user's real app data folder
    window = MainWindow(demo_context(), offline=True)
    qtbot.addWidget(window)
    github = github_with_release(tmp_path)
    window.koupdate.get, window.koupdate.save = github, github.save
    return window, github


class Answers:
    """Stands in for the modal boxes: offer, preview and result are answered as given."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, install: bool = True) -> None:
        self.offers: list[str] = []
        self.previews: list[tuple[str, str, str]] = []
        self.results: list[str] = []
        self.messages: list[str] = []

        def offer(runner: KOReaderInstaller, reader: BareReader) -> bool:
            self.offers.append(offer_text(reader)[0])
            if install:
                runner.start(reader)
            return install

        def confirm(_runner: KOReaderInstaller, plan: FreshPlan) -> bool:
            self.previews.append(preview_text(plan))
            return True

        monkeypatch.setattr(KOReaderInstaller, "offer", offer)
        monkeypatch.setattr(KOReaderInstaller, "confirm", confirm)
        monkeypatch.setattr(
            KOReaderInstaller,
            "result",
            lambda _r, plan, _done: self.results.append(plan.release.tag),
        )
        for name in ("information", "warning"):
            monkeypatch.setattr(QMessageBox, name, lambda _p, _t, text: self.messages.append(text))


def test_plugging_in_a_reader_without_koreader_installs_it(
    window: tuple[MainWindow, FakeGitHub],
    qtbot: QtBot,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main, _github = window
    answers = Answers(monkeypatch)
    volumes = Volumes()
    watcher = main.watch_readers(volumes.readers, volumes.bare)
    volume = bare_volume(tmp_path / LABEL)
    volumes.mounted.append(volume)
    watcher.poll()
    assert answers.offers == [
        "KOReader is not installed on this PocketBook Verse Pro Color (PB634K3) yet."
    ]
    qtbot.waitUntil(lambda: not watcher.installer.busy, timeout=10000)
    assert (
        answers.previews[0][0]
        == f"Install KOReader {RELEASE} on PocketBook Verse Pro Color (PB634K3)?"
    )
    assert answers.results == [RELEASE]
    assert find_install(volume).version == RELEASE
    assert (volume / "Books" / "Alice.epub").read_bytes() == b"PK book"

    watcher.poll()  # KOReader is there now, but has not started yet: nothing to show
    assert main.install is None and len(answers.offers) == 1
    assert [a.text() for a in watcher.menu.actions()] == ["No reader connected"]


def test_not_now_is_not_asked_again_until_plugged_in_again(
    window: tuple[MainWindow, FakeGitHub], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main, _github = window
    answers = Answers(monkeypatch, install=False)
    volumes = Volumes()
    watcher = main.watch_readers(volumes.readers, volumes.bare)
    volumes.mounted.append(bare_volume(tmp_path / LABEL))
    watcher.poll()
    watcher.poll()
    assert len(answers.offers) == 1
    menu = [a.text() for a in watcher.menu.actions()]
    assert menu == ["&Install KOReader on PocketBook Verse Pro Color (PB634K3)…"]
    watcher.menu.actions()[0].trigger()  # the menu asks again
    assert len(answers.offers) == 2
    volumes.mounted.clear()
    watcher.poll()
    volumes.mounted.append(tmp_path / LABEL)
    watcher.poll()
    assert len(answers.offers) == 3
    assert not (tmp_path / LABEL / "applications" / "koreader").exists()


def test_offline_names_the_reader_and_does_not_ask(
    window: tuple[MainWindow, FakeGitHub], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main, _github = window
    answers = Answers(monkeypatch)
    main.koupdate.get = main.koupdate.save = None
    volumes = Volumes()
    watcher = main.watch_readers(volumes.readers, volumes.bare)
    volumes.mounted.append(bare_volume(tmp_path / LABEL))
    watcher.poll()
    assert not answers.offers
    assert "has no KOReader yet" in main.statusBar().currentMessage()


def test_remove_it_again_leaves_the_reader_as_it_was(
    window: tuple[MainWindow, FakeGitHub],
    qtbot: QtBot,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main, _github = window
    volume = bare_volume(tmp_path / LABEL)
    before = sorted(p.relative_to(volume) for p in volume.rglob("*"))
    answers = Answers(monkeypatch)
    monkeypatch.setattr(
        KOReaderInstaller, "result", lambda r, plan, done: r.undo(plan.reader, done)
    )
    watcher = main.watch_readers(list, list)
    reader = find_bare(volume, LABEL)
    assert reader is not None
    watcher.installer.start(reader)
    qtbot.waitUntil(lambda: not watcher.installer.busy, timeout=10000)
    assert sorted(p.relative_to(volume) for p in volume.rglob("*")) == before
    assert answers.messages == ["KOReader is removed again; the reader is as it was."]
    assert not watcher.installer.fresh


def test_the_offer_is_keyboard_operable(
    window: tuple[MainWindow, FakeGitHub], tmp_path: Path
) -> None:
    main, _github = window
    watcher = main.watch_readers(list, list)
    reader = find_bare(bare_volume(tmp_path / LABEL), LABEL)
    assert reader is not None
    seen: list[list[str]] = []

    def answer() -> None:
        box = QApplication.activeModalWidget()
        assert isinstance(box, QMessageBox) and box.objectName() == "koreader-install-offer"
        seen.append([b.text() for b in box.buttons()])
        not_now = next(b for b in box.buttons() if b.text() == "&Not now")
        not_now.click()

    QTimer.singleShot(0, answer)
    assert watcher.installer.offer(reader) is False
    assert sorted(seen[0]) == ["&Install KOReader", "&Not now"]
    assert not watcher.installer.busy
