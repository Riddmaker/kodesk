"""Install, update and remove from the GUI (B-08, B-09): fake reader, fake GitHub, offscreen."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QStandardPaths, Qt
from PySide6.QtWidgets import QMessageBox
from pytestqt.qtbot import QtBot

from kodesk.core import catalogue as cat
from kodesk.core.install import find_install
from kodesk.core.snapshot import SnapshotStore, file_sha256
from kodesk.ui.app import MainWindow, reader_context
from kodesk.ui.changes import ChangeRunner, Prepared, preview_text
from tests.conftest import (
    USER_PATCH,
    FakeGitHub,
    fake_catalogue,
    make_reader,
    plugin_files,
    plugin_zip,
)

PROTECTED = ("settings.reader.lua", "history.lua", "settings/vocabulary_builder.sqlite3")
CLOCK = ("bob", "readingclock.koplugin")


class Dialogs:
    """Stands in for the modal boxes: previews are confirmed, messages collected."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, confirm: bool = True) -> None:
        self.previews: list[tuple[str, str, str]] = []
        self.messages: list[str] = []

        def preview(_runner: ChangeRunner, prepared: Prepared) -> bool:
            self.previews.append(preview_text(prepared))
            return confirm

        monkeypatch.setattr(ChangeRunner, "confirm", preview)
        for name in ("information", "warning"):
            monkeypatch.setattr(QMessageBox, name, lambda _p, _t, text: self.messages.append(text))


@pytest.fixture(name="window")
def fixture_window(qtbot: QtBot, tmp_path: Path) -> tuple[MainWindow, Path, FakeGitHub]:
    """A main window on a fake reader, its snapshots in tmp, GitHub faked."""
    QStandardPaths.setTestModeEnabled(True)
    koreader = make_reader(tmp_path / "PB634K3", "pocketbook")
    (koreader / "version.log").write_text("2026-08-26, 08:01:22, v2026.07.1, PBVerseProColor\n")
    install = find_install(tmp_path / "PB634K3")
    main = MainWindow(reader_context(install), install, offline=True)  # store: no README fetch
    qtbot.addWidget(main)
    github = FakeGitHub()
    main.changes.get = github  # installs go to the fake GitHub
    store = SnapshotStore(tmp_path / "data" / "snapshots" / "reader")
    main.changes.store_for = lambda _install: store
    main.store.set_catalogue(cat.parse(*fake_catalogue()))
    return main, koreader, github


def _select(main: MainWindow, name: str) -> None:
    main.store.search.setText(name)
    main.store.enter_results()


def _key(main: MainWindow, name: str) -> tuple[cat.Kind, str]:
    keys = [k for k in main.installed.checkable() if k[1] == name]
    assert len(keys) == 1
    return keys[0]


def _select_installed(main: MainWindow, name: str) -> None:
    tree = main.installed.tree
    for i in range(tree.topLevelItemCount()):
        group = tree.topLevelItem(i)
        for j in range(group.childCount() if group else 0):
            item = group.child(j) if group else None
            if item is not None and item.text(0) == name:
                tree.setCurrentItem(item)
                return
    raise AssertionError(f"{name} is not listed")


def _cells(main: MainWindow, name: str) -> list[str]:
    _select_installed(main, name)
    item = main.installed.tree.currentItem()
    assert item is not None
    return [item.text(c) for c in range(5)]


def test_install_check_update_remove(
    window: tuple[MainWindow, Path, FakeGitHub], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    main, koreader, github = window
    dialogs = Dialogs(monkeypatch)
    before = {p: file_sha256(koreader / p) for p in PROTECTED}
    folder = koreader / "plugins" / "readingclock.koplugin"
    zip_v1 = plugin_zip(plugin_files("readingclock.koplugin", "1.0"))
    github.release(*CLOCK, "v1.0", {"readingclock.koplugin.zip": zip_v1})

    _select(main, "clock")
    assert main.store.details.install_button.text() == "Ins&tall…"
    main.store.details.install_button.click()
    qtbot.waitUntil(lambda: folder.is_dir() and bool(dialogs.messages))
    question, text, details = dialogs.previews[0]
    assert question == "Install readingclock v1.0?"
    assert "https://github.com/bob/readingclock.koplugin (GPL-3.0)" in text
    assert "SHA-256" in text and "Files: 3 added." in text
    assert "plugins/readingclock.koplugin/main.lua" in details
    assert dialogs.messages[-1].startswith("Installed readingclock.")
    assert _cells(main, "readingclock")[3] == "bob/readingclock.koplugin v1.0"
    assert main.store.details.install_button.text() == "&Update…"  # now marked as installed

    github.release(*CLOCK, "v2.0", {"readingclock.koplugin.zip": b"new"})
    main.installed.check_button.click()
    qtbot.waitUntil(lambda: _cells(main, "readingclock")[4] == "v2.0 available")
    assert _cells(main, "2-reader-footer-tweak.lua")[4] == "Not checked: " + (
        "https://raw.githubusercontent.com/erin/kopatches/main/2-reader-footer-tweak.lua: HTTP 404"
    )

    zip_v2 = plugin_zip(plugin_files("readingclock.koplugin", "2.0"))
    github.release(*CLOCK, "v2.0", {"readingclock.koplugin.zip": zip_v2})
    _select_installed(main, "readingclock")
    main.installed.update_button.click()
    qtbot.waitUntil(lambda: len(dialogs.previews) == 2 and len(dialogs.messages) == 2)
    assert dialogs.previews[1][0] == "Update readingclock v2.0?"
    assert (folder / "main.lua").read_bytes() == b"-- main 2.0\n"

    _select_installed(main, "readingclock")
    main.installed.remove_button.click()
    assert dialogs.previews[2][0] == "Remove readingclock?"
    assert not folder.exists() and dialogs.messages[-1].startswith("Removed readingclock.")
    assert {p: file_sha256(koreader / p) for p in PROTECTED} == before


def test_set_source_and_patch_update(
    window: tuple[MainWindow, Path, FakeGitHub], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    main, koreader, github = window
    dialogs = Dialogs(monkeypatch)
    assert _cells(main, USER_PATCH)[3] == "probably erin/kopatches:" + USER_PATCH
    asked: list[list[str]] = []

    def ask(_runner: ChangeRunner, _name: str, choices: list[str]) -> str:
        asked.append(choices)
        return choices[0]

    monkeypatch.setattr(ChangeRunner, "ask_source", ask)
    _select_installed(main, USER_PATCH)
    main.installed.source_button.click()
    assert asked[0][0] == f"erin/kopatches:{USER_PATCH}"  # the guess by name comes first
    assert _cells(main, USER_PATCH)[3] == "erin/kopatches"  # confirmed, no version yet

    assert main.store.catalogue is not None
    entry = next(e for e in main.store.catalogue.entries if e.name == USER_PATCH)
    assert entry.text_url is not None
    github.files[entry.text_url] = b"-- improved footer\n"
    _select_installed(main, USER_PATCH)
    main.installed.update_button.click()
    qtbot.waitUntil(lambda: bool(dialogs.messages))
    question, _text, details = dialogs.previews[0]
    assert question == f"Update patch {USER_PATCH}?"
    assert details.endswith("Code:\n-- improved footer\n")  # the code is part of the preview
    assert (koreader / "patches" / USER_PATCH).read_bytes() == b"-- improved footer\n"


def test_cancel_and_offline_change_nothing(
    window: tuple[MainWindow, Path, FakeGitHub], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    main, koreader, github = window
    dialogs = Dialogs(monkeypatch, confirm=False)
    github.release(*CLOCK, "v1.0", {"x.zip": plugin_zip(plugin_files("readingclock.koplugin"))})
    _select(main, "clock")
    main.store.details.install_button.click()
    qtbot.waitUntil(lambda: bool(dialogs.previews))
    qtbot.wait(50)
    assert not (koreader / "plugins" / "readingclock.koplugin").exists()
    assert main.install is not None
    assert not list(main.changes.store_for(main.install).base.glob("manifests/*"))

    main.changes.get = None  # kodesk --offline
    main.store.details.install_button.click()
    assert dialogs.messages == ["kodesk runs offline (--offline); installing needs the network."]


def test_buttons_have_keys(
    window: tuple[MainWindow, Path, FakeGitHub], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    main, koreader, _github = window
    dialogs = Dialogs(monkeypatch, confirm=False)  # the keys open the previews; nothing written
    with qtbot.waitActive(main):
        main.show()
        main.activateWindow()
    main.tabs.setCurrentWidget(main.installed)
    _select_installed(main, USER_PATCH)
    main.installed.tree.setFocus()
    with qtbot.waitSignal(main.installed.remove_requested) as removed:
        qtbot.keyClick(main.installed.tree, Qt.Key.Key_M, Qt.KeyboardModifier.AltModifier)
    assert removed.args == [(cat.Kind.PATCH, USER_PATCH)]
    assert (koreader / "patches" / USER_PATCH).is_file()
    main.tabs.setCurrentWidget(main.store)
    _select(main, "clock")
    with qtbot.waitSignal(main.store.install_requested):
        qtbot.keyClick(main.store.list, Qt.Key.Key_T, Qt.KeyboardModifier.AltModifier)
    # The install it starts fails in the background (no fake release); wait for its message,
    # or it would land in the next test once this test's dialog stand-ins are gone.
    qtbot.waitUntil(lambda: bool(dialogs.messages))


def test_preview_says_when_there_is_no_licence(tmp_path: Path) -> None:
    from kodesk.core.installer import plan_patch  # pylint: disable=import-outside-toplevel

    koreader = make_reader(tmp_path / "r", "pocketbook")
    entry = next(e for e in cat.parse(*fake_catalogue()).entries if e.name == USER_PATCH)
    change = plan_patch(koreader, entry, b"--\n", matches_index=None)
    question, text, _details = preview_text(Prepared(change, entry, guessed=True))
    assert question == f"Update patch {USER_PATCH}?"
    assert "(no licence given)" in text and "found by the name alone" in text
