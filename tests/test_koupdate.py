"""KOReader update from the GUI (B-06): fake reader, fake GitHub, offscreen."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QStandardPaths, Qt
from PySide6.QtWidgets import QMessageBox
from pytestqt.qtbot import QtBot

from kodesk.core import release, update
from kodesk.core.install import find_install
from kodesk.core.snapshot import SnapshotStore
from kodesk.ui.app import MainWindow, reader_context
from kodesk.ui.koupdate import KOReaderUpdater, UpdateDialog, preview_text
from tests.conftest import RELEASE, FakeGitHub, make_reader
from tests.test_update import EDITED, LAUNCHER, LAUNCHER_STEPS, NEW, koreader_github


class Dialogs:
    """Stands in for the modal boxes: the preview keeps ``keep``, messages are collected."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, keep: frozenset[str] | None) -> None:
        self.previews: list[tuple[str, str, str]] = []
        self.messages: list[str] = []
        self.conflicts: list[tuple[str, ...]] = []

        def preview(_runner: KOReaderUpdater, plan: update.UpdatePlan) -> frozenset[str] | None:
            self.previews.append(preview_text(plan))
            return keep

        monkeypatch.setattr(KOReaderUpdater, "confirm", preview)
        monkeypatch.setattr(
            KOReaderUpdater, "conflict", lambda _r, plan: self.conflicts.append(plan.conflicts)
        )
        for name in ("information", "warning"):
            monkeypatch.setattr(QMessageBox, name, lambda _p, _t, text: self.messages.append(text))


@pytest.fixture(name="window")
def fixture_window(
    qtbot: QtBot, tmp_path: Path
) -> tuple[MainWindow, Path, FakeGitHub, SnapshotStore]:
    """A main window on a fake reader (v2026.07.1), its snapshots in tmp, GitHub faked."""
    QStandardPaths.setTestModeEnabled(True)
    koreader = make_reader(tmp_path / "PB634K3", "pocketbook")
    (tmp_path / "PB634K3" / EDITED).write_text("/* mine */\n")
    (koreader / "version.log").write_text(f"2026-08-26, 08:01:22, {RELEASE}, PBVerseProColor\n")
    install = find_install(tmp_path / "PB634K3")
    main = MainWindow(reader_context(install), install, offline=True)
    qtbot.addWidget(main)
    github = koreader_github(tmp_path)
    main.koupdate.get, main.koupdate.save = github, github.save
    store = SnapshotStore(tmp_path / "data" / "snapshots" / "reader")
    main.koupdate.store_for = lambda _install: store
    return main, koreader, github, store


def _run(main: MainWindow, qtbot: QtBot) -> None:
    main.tabs.setCurrentWidget(main.installed)
    main.installed.koreader_button.click()
    qtbot.waitUntil(lambda: not main.koupdate.busy, timeout=10000)


def test_update_from_the_installed_tab(
    window: tuple[MainWindow, Path, FakeGitHub, SnapshotStore],
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main, koreader, _github, store = window
    assert main.installed.koreader.text() == f"KOReader {RELEASE}"
    dialogs = Dialogs(monkeypatch, keep=frozenset({EDITED}))
    _run(main, qtbot)
    question, text, details = dialogs.previews[0]
    assert question == f"Update KOReader from {RELEASE} to {NEW}?"
    assert "github.com/koreader/koreader" in text and "SHA-256" in text
    assert "Added (1):" in details
    assert (koreader / "git-rev").read_text() == f"{NEW}\n"
    assert (koreader.parents[1] / EDITED).read_text() == "/* mine */\n"
    assert dialogs.messages[-1].startswith(f"KOReader is now {NEW}.")
    assert main.installed.koreader.text() == f"KOReader {NEW}"  # read again
    assert len(store.snapshots()) == 1
    assert main.koupdate.progress is None


def test_cancel_in_the_preview_writes_nothing(
    window: tuple[MainWindow, Path, FakeGitHub, SnapshotStore],
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main, koreader, _github, store = window
    Dialogs(monkeypatch, keep=None)
    _run(main, qtbot)
    assert (koreader / "git-rev").read_text() == f"{RELEASE}\n"
    assert not store.snapshots()


def test_up_to_date_and_offline(
    window: tuple[MainWindow, Path, FakeGitHub, SnapshotStore],
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main, _koreader, github, _store = window
    dialogs = Dialogs(monkeypatch, keep=frozenset())
    github.files[f"{release.API}/latest"] = github.files[f"{release.API}/tags/{RELEASE}"]
    _run(main, qtbot)
    assert dialogs.messages == ["KOReader on your reader is up to date."]
    main.koupdate.get = None
    _run(main, qtbot)
    assert "offline" in dialogs.messages[-1]
    assert not dialogs.previews
    assert not list(tmp_path.glob("kodesk-koreader-*"))


def test_a_clashing_plugin_stops_the_update(
    window: tuple[MainWindow, Path, FakeGitHub, SnapshotStore],
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main, koreader, _github, _store = window
    clash = "applications/koreader/plugins/projecttitle.koplugin/main.lua"
    (tmp_path / "gh").mkdir()
    github = koreader_github(tmp_path / "gh", changed={clash: b"-- built in now\n"})
    main.koupdate.get, main.koupdate.save = github, github.save
    dialogs = Dialogs(monkeypatch, keep=frozenset())
    _run(main, qtbot)
    assert dialogs.conflicts == [("projecttitle",)] and not dialogs.previews
    assert (koreader / "git-rev").read_text() == f"{RELEASE}\n"


def test_check_for_updates_names_the_new_koreader(
    window: tuple[MainWindow, Path, FakeGitHub, SnapshotStore],
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main, _koreader, _github, _store = window
    Dialogs(monkeypatch, keep=None)
    main.changes.get = main.koupdate.get  # plugins: no catalogue, so only KOReader is checked
    main.koupdate.check(main.installed.set_koreader_status)
    qtbot.waitUntil(lambda: "available" in main.installed.koreader.text())
    assert main.installed.koreader.text() == f"KOReader {RELEASE} · {NEW} available"


def test_an_interrupted_update_offers_the_snapshot(
    window: tuple[MainWindow, Path, FakeGitHub, SnapshotStore],
) -> None:
    main, _koreader, _github, store = window
    update.remember(store.base, RELEASE, NEW, "20261126T130000Z")
    box = main.check_health()
    assert box is not None and box.objectName() == "koreader-update-interrupted"
    assert NEW in box.text()
    assert update.pending(store.base) is None  # asked once
    box.close()


def test_the_preview_lets_the_user_keep_edited_files(
    window: tuple[MainWindow, Path, FakeGitHub, SnapshotStore], tmp_path: Path
) -> None:
    main, koreader, github, _store = window
    install = find_install(koreader)
    plan = update.prepare(install, github, github.save, tmp_path / "s", lambda: False)
    assert isinstance(plan, update.UpdatePlan)
    dialog = UpdateDialog(plan, main)
    assert dialog.keep() == frozenset()
    item = dialog.edited.item(0)
    assert item is not None and item.text() == EDITED
    item.setCheckState(Qt.CheckState.Checked)
    assert dialog.keep() == frozenset({EDITED})
    assert dialog.accept_button.text() == f"&Update KOReader to {NEW}"


def test_update_koreader_has_keys(
    window: tuple[MainWindow, Path, FakeGitHub, SnapshotStore], qtbot: QtBot
) -> None:
    main, _koreader, _github, _store = window
    with qtbot.waitActive(main):
        main.show()
        main.activateWindow()
    main.tabs.setCurrentWidget(main.installed)
    assert main.installed.koreader_button.text() == "Update &KOReader…"
    assert main.koreader_action.text() == "Update &KOReader…" and main.koreader_action.isEnabled()
    with qtbot.waitSignal(main.installed.koreader_update_requested):
        qtbot.keyClick(main.installed.tree, Qt.Key.Key_K, Qt.KeyboardModifier.AltModifier)


def test_over_wifi_the_update_is_sent_for_the_launcher(
    window: tuple[MainWindow, Path, FakeGitHub, SnapshotStore],
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main, koreader, _github, store = window
    (koreader.parents[1] / LAUNCHER).write_text(LAUNCHER_STEPS)
    sent: list[tuple[str, bytes]] = []
    main.koupdate.send = lambda path, progress: sent.append((path.name, path.read_bytes()))
    dialogs = Dialogs(monkeypatch, keep=frozenset())
    _run(main, qtbot)
    assert [name for name, _data in sent] == ["koreader.updated.tar"]
    assert (koreader / "git-rev").read_text() != f"{NEW}\n"  # the launcher installs it
    assert len(store.snapshots()) == 1
    assert "Restart KOReader" in dialogs.messages[-1]
