"""Installed tab (B-03): what is on the reader, read-only, on a fake reader tree."""

from __future__ import annotations

from pathlib import Path

from pytestqt.qtbot import QtBot

from kodesk.core import catalogue as cat
from kodesk.core.install import find_install
from kodesk.core.inventory import Origin, Patch, Plugin
from kodesk.ui.app import MainWindow, demo_context, reader_context
from kodesk.ui.installed import NO_READER, InstalledPage, patch_row, summary
from tests.conftest import (
    OWNER_LIKE_SETTINGS,
    THIRD_PARTY_PLUGIN,
    USER_PATCH,
    fake_catalogue,
    make_reader,
)


def _rows(page: InstalledPage) -> dict[str, list[tuple[str, ...]]]:
    groups: dict[str, list[tuple[str, ...]]] = {}
    for index in range(page.tree.topLevelItemCount()):
        group = page.tree.topLevelItem(index)
        assert group is not None
        children = [group.child(i) for i in range(group.childCount())]
        groups[group.text(0).split(" (")[0]] = [
            tuple(child.text(c) for c in range(3)) for child in children if child is not None
        ]
    return groups


def test_lists_what_the_user_added(qtbot: QtBot, tmp_path: Path) -> None:
    settings = {**OWNER_LIKE_SETTINGS}
    settings["plugins_disabled"] = {THIRD_PARTY_PLUGIN: True}
    koreader = make_reader(tmp_path / "PB634K3", "pocketbook", settings)
    (koreader / "patches" / "5-reserved.lua").write_text("-- never run\n")
    before = {p: p.read_bytes() for p in koreader.rglob("*") if p.is_file()}
    page = InstalledPage(None)
    qtbot.addWidget(page)
    page.set_install(find_install(tmp_path / "PB634K3"))
    rows = _rows(page)
    assert rows["Plugins"] == [(THIRD_PARTY_PLUGIN, "Added by you", "Off")]
    assert rows["User patches"] == [
        (USER_PATCH, "Added by you", "Runs at start"),
        ("5-reserved.lua", "Added by you", "Not run: priority 5 is reserved"),
    ]
    assert "plugins (1 added by you, " in page.summary.text()
    assert page.summary.text().endswith("built in), 2 user patches.")
    page.show_built_in.setChecked(True)
    plugins = _rows(page)["Plugins"]
    assert len(plugins) > 1 and ("Built in", "On") in {row[1:] for row in plugins}
    assert {p: p.read_bytes() for p in koreader.rglob("*") if p.is_file()} == before  # read-only


def test_summary_never_guesses_the_origin() -> None:
    plugins = (Plugin("a", Origin.UNKNOWN, True), Plugin("b", Origin.UNKNOWN, False))
    assert summary(plugins, ()) == "2 plugins (2 of unknown origin), 0 user patches."
    assert summary((), (Patch("2-x.lua", "2", True),)) == "0 plugins, 1 user patch."


def test_patch_states() -> None:
    assert patch_row(Patch("0-x.lua", "0", True))[2] == "Runs after the next update"
    assert patch_row(Patch("2-x.lua", "2", False))[2] == "Not run: all patches are switched off"
    assert patch_row(Patch("x.lua", None, False))[2].startswith("Not run: name does not")


def test_demo_shows_the_hint_and_the_tab_has_a_key(qtbot: QtBot, tmp_path: Path) -> None:
    window = MainWindow(demo_context())
    qtbot.addWidget(window)
    assert window.installed.summary.text() == NO_READER
    assert not window.installed.tree.isVisibleTo(window.installed)
    titles = [window.tabs.tabText(i) for i in range(window.tabs.count())]
    assert titles == ["Tuning-&Garage", "&Installed", "&Store"]
    koreader = make_reader(tmp_path / "PB634K3", "pocketbook")
    (koreader / "version.log").write_text("2026-08-26, 08:01:22, v2026.07.1, PBVerseProColor\n")
    install = find_install(tmp_path / "PB634K3")
    window.set_context(reader_context(install), install)
    assert window.installed.install is install
    entry = cat.parse(*fake_catalogue()).entries[0]
    assert entry.name == THIRD_PARTY_PLUGIN and window.store.installed(entry)  # same names
