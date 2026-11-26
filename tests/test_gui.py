"""Widget tests (pytest-qt, offscreen). Drive the mockup like a user would."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QStandardPaths, Qt
from PySide6.QtWidgets import QCheckBox, QListWidget, QMessageBox, QSlider
from pytestqt.qtbot import QtBot

from kodesk.core import healthcheck
from kodesk.core.devices import Face
from kodesk.core.install import find_install
from kodesk.core.snapshot import RestorePlan, Snapshot, SnapshotStore
from kodesk.ui.app import MainWindow, demo_context, reader_context, snapshot_store
from kodesk.ui.garage import TuningGarage, View
from kodesk.ui.snapshots import SnapshotsDialog, human_size, plan_text
from tests.conftest import make_pocketbook, make_reader

pytestmark = pytest.mark.gui


@pytest.fixture(name="garage")
def fixture_garage(qtbot: QtBot, pocketbook: Path) -> Iterator[TuningGarage]:
    window = MainWindow(reader_context(find_install(pocketbook)))
    qtbot.addWidget(window)  # pytest-qt keeps only a weak reference: hold the window here
    window.resize(1200, 800)
    window.show()
    yield window.garage


def current_view(garage: TuningGarage) -> View:
    """Read the view through a call, so mypy does not narrow it across key presses."""
    return garage.view


def click_key(qtbot: QtBot, garage: TuningGarage, key: str) -> None:
    qtbot.mouseClick(garage.device_view.keys[key], Qt.MouseButton.LeftButton)


def test_every_front_key_is_a_visible_pressable_button(garage: TuningGarage) -> None:
    keys = garage.device_view.keys
    assert set(keys) == {"home", "prev", "next", "menu"}
    for button in keys.values():
        assert button.isVisible() and button.width() > 10 and button.accessibleName()


def test_page_keys_turn_pages_and_can_be_swapped(qtbot: QtBot, garage: TuningGarage) -> None:
    click_key(qtbot, garage, "next")
    click_key(qtbot, garage, "next")
    assert garage.page == 2
    click_key(qtbot, garage, "prev")
    assert garage.page == 1
    swap = garage.findChild(QCheckBox, "invert-page-turn")
    assert swap is not None and swap.isVisible()
    swap.setChecked(True)
    click_key(qtbot, garage, "next")
    assert garage.page == 0
    assert ("input_invert_page_turn_keys",) in garage.changed()


def test_home_and_menu_switch_views(qtbot: QtBot, garage: TuningGarage) -> None:
    click_key(qtbot, garage, "menu")
    assert current_view(garage) is View.MENU
    click_key(qtbot, garage, "menu")
    assert current_view(garage) is View.BOOK
    click_key(qtbot, garage, "home")
    assert current_view(garage) is View.LIBRARY
    assert "Locked" in garage.key_panel.info.text()


def test_home_and_menu_are_locked_without_a_plugin(garage: TuningGarage) -> None:
    for key in ("home", "menu"):
        garage.select_key(key)
        assert "Locked" in garage.key_panel.info.text()
        assert not garage.key_panel.unlock.isVisibleTo(garage)
    garage.select_key("home")
    assert "pbfixes" in garage.key_panel.info.text()


def test_pbfixes_plugin_unlocks_home(qtbot: QtBot, tmp_path: Path) -> None:
    reader = make_pocketbook(tmp_path / "PB634K3")
    (reader / "applications/koreader/plugins/pbfixes.koplugin").mkdir(parents=True)
    window = MainWindow(reader_context(find_install(reader)))
    qtbot.addWidget(window)
    window.show()
    garage = window.garage
    garage.select_key("home")
    unlock = garage.key_panel.unlock
    assert "Unlocked" in garage.key_panel.info.text()
    assert unlock.isVisible() and unlock.isChecked()  # pbfixes: absent key = on
    garage.press_key("menu")
    garage.press_key("home")
    assert current_view(garage) is View.BOOK  # Back closes the menu instead of the library
    assert not garage.changed()  # showing the state writes nothing
    unlock.setChecked(False)
    assert garage.changed() == [("pbfixes_home_back",)]
    assert garage.edited.get(("pbfixes_home_back",)) is False
    garage.press_key("home")
    assert current_view(garage) is View.LIBRARY  # option off: Home is Home again
    unlock.setChecked(True)
    assert not garage.changed()  # back to the plugin's default: key stays absent
    garage.select_key("menu")
    assert not unlock.isVisible()


def test_night_pictures_option_follows_night_mode(garage: TuningGarage) -> None:
    night = garage.findChild(QCheckBox, "night-mode")
    pictures = garage.findChild(QCheckBox, "night-pictures")
    assert night is not None and pictures is not None
    assert night.isChecked() and pictures.isEnabled() and pictures.isChecked()
    before = garage.screen_image()
    pictures.setChecked(False)
    assert garage.changed() == [("copt_nightmode_images",)]
    assert garage.screen_image() != before  # the picture turns into a negative
    pictures.setChecked(True)
    assert not garage.changed()
    night.setChecked(False)
    assert not pictures.isEnabled()


def test_long_press_menu_is_firmware_power(garage: TuningGarage) -> None:
    garage.long_press_key("menu")
    assert "powers the reader off" in garage.status.text()


def test_slider_changes_exactly_one_key(garage: TuningGarage) -> None:
    slider = garage.findChild(QSlider, "font-size")
    assert slider is not None
    slider.setValue(26)
    assert garage.changed() == [("copt_font_size",)]
    original_lines = garage.original.text.splitlines()
    edited_lines = garage.edited.text.splitlines()
    assert sum(a != b for a, b in zip(original_lines, edited_lines, strict=True)) == 1
    changes = garage.findChild(QListWidget, "changes")
    assert changes is not None and changes.item(0).text() == "copt_font_size: 22 → 26"
    slider.setValue(22)
    assert not garage.changed()


def test_screen_is_rendered_at_device_pixels(garage: TuningGarage) -> None:
    for view in View:
        garage.set_view(view)
        image = garage.screen_image()
        assert (image.width(), image.height()) == (1072, 1448)


def test_bottom_edge_face_hides_front_keys(garage: TuningGarage) -> None:
    garage.device_view.set_face(Face.BOTTOM)
    assert not any(button.isVisible() for button in garage.device_view.keys.values())


def test_demo_starts_without_reader(qtbot: QtBot) -> None:
    window = MainWindow(demo_context())
    qtbot.addWidget(window)
    assert window.garage.context.library_entries == ()


def test_hardware_keys_work_with_keyboard_only(qtbot: QtBot, garage: TuningGarage) -> None:
    """Tab reaches every reader key in physical order; Space presses the focused one."""
    keys = garage.device_view.keys
    keys["home"].setFocus()
    order = ["home"]
    for _ in range(3):
        qtbot.keyClick(garage.focusWidget(), Qt.Key.Key_Tab)
        focused = garage.focusWidget()
        order.append(next(cid for cid, button in keys.items() if button is focused))
    assert order == ["home", "prev", "next", "menu"]
    qtbot.keyClick(keys["menu"], Qt.Key.Key_Space)
    assert current_view(garage) is View.MENU


def test_smoke_test_flag_starts_and_quits(tmp_path: Path) -> None:
    """What CI runs on every packaged build: the window opens and the app exits with 0.

    In its own process: once ``app.quit()`` ran, Qt ends every later event loop at once,
    which would starve the tests after this one.
    """
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "HOME": str(tmp_path)}
    env["XDG_CACHE_HOME"] = str(tmp_path / "cache")
    command = [sys.executable, "-m", "kodesk", "--demo", "--offline", "--smoke-test"]
    done = subprocess.run(command, env=env, capture_output=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr.decode(errors="replace")


def test_snapshots_take_and_restore(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # Qt's test-mode folder lives in HOME
    QStandardPaths.setTestModeEnabled(True)  # never the user's real app data folder
    koreader = make_reader(tmp_path / "PB634K3", "pocketbook")
    (koreader / "version.log").write_text("2026-08-26, 08:01:22, v2026.07.1, PBVerseProColor\n")
    install = find_install(tmp_path / "PB634K3")
    window = MainWindow(reader_context(install), install)
    qtbot.addWidget(window)
    store = snapshot_store(install)
    assert store.base.is_relative_to(tmp_path)
    dialog = window.show_snapshots()
    assert dialog is not None and not dialog.restore_button.isEnabled()
    qtbot.mouseClick(dialog.take_button, Qt.MouseButton.LeftButton)
    assert dialog.list.count() == 1 and dialog.restore_button.isEnabled()

    settings = koreader / "settings.reader.lua"
    original = settings.read_bytes()
    settings.write_text("return {}\n")
    shown: list[str] = []
    monkeypatch.setattr(SnapshotsDialog, "confirm", lambda _self, _snap, plan: bool(plan))
    monkeypatch.setattr(QMessageBox, "information", lambda _p, _t, text: shown.append(text))
    qtbot.mouseClick(dialog.restore_button, Qt.MouseButton.LeftButton)
    assert settings.read_bytes() == original
    assert shown == ["Restored. Your reader matches the snapshot again."]


def test_return_does_not_take_a_snapshot(qtbot: QtBot, tmp_path: Path) -> None:
    QStandardPaths.setTestModeEnabled(True)
    koreader = make_reader(tmp_path / "PB634K3", "pocketbook")
    store = SnapshotStore(tmp_path / "store")
    dialog = SnapshotsDialog(store, koreader, None)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.keyClick(dialog.list, Qt.Key.Key_Return)
    assert not store.snapshots() and dialog.isVisible()


def test_sizes_are_readable() -> None:
    assert (human_size(300), human_size(5 * 2**20)) == ("1 kB", "5 MB")


def test_restore_preview_names_files_then_counts() -> None:
    plan = RestorePlan(write=tuple(f"settings/{n}.lua" for n in range(32)), remove=("x.lua",))
    text = plan_text(plan)
    assert text.startswith("Written back (32):\n  settings/0.lua")
    assert "… and 2 more" in text and text.endswith("Removed (1):\n  x.lua")


def test_demo_reader_has_no_snapshots(qtbot: QtBot) -> None:
    window = MainWindow(demo_context())
    qtbot.addWidget(window)
    assert not window.snapshots_action.isEnabled() and window.show_snapshots() is None


def _real_reader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[MainWindow, Path]:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    QStandardPaths.setTestModeEnabled(True)
    koreader = make_reader(tmp_path / "PB634K3", "pocketbook")
    (koreader / "version.log").write_text("2026-08-26, 08:01:22, v2026.07.1, PBVerseProColor\n")
    install = find_install(tmp_path / "PB634K3")
    return MainWindow(reader_context(install), install), koreader


def test_write_to_reader_writes_one_line_after_a_snapshot(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    window, koreader = _real_reader(tmp_path, monkeypatch)
    qtbot.addWidget(window)
    garage = window.garage
    assert not garage.changes_box.write_button.isEnabled()  # nothing changed yet
    slider = garage.findChild(QSlider, "font-size")
    assert slider is not None
    slider.setValue(26)
    assert garage.changes_box.write_button.isEnabled()
    monkeypatch.setattr(TuningGarage, "confirm_write", lambda _self, count: count == 1)
    qtbot.mouseClick(garage.changes_box.write_button, Qt.MouseButton.LeftButton)
    assert '["copt_font_size"] = 26,' in (koreader / "settings.reader.lua").read_text()
    assert garage.status.text().startswith("Written to your reader.")
    assert not garage.changed() and not garage.changes_box.write_button.isEnabled()
    assert install_snapshots(window) == 1


def install_snapshots(window: MainWindow) -> int:
    assert window.install is not None
    return len(snapshot_store(window.install).snapshots())


def test_write_refused_when_koreader_saved_in_between(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    window, koreader = _real_reader(tmp_path, monkeypatch)
    qtbot.addWidget(window)
    slider = window.garage.findChild(QSlider, "font-size")
    assert slider is not None
    slider.setValue(26)
    settings = koreader / "settings.reader.lua"
    settings.write_text(settings.read_text().replace("= 22,", "= 24,"))
    warnings: list[str] = []
    monkeypatch.setattr(TuningGarage, "confirm_write", lambda _self, _count: True)
    monkeypatch.setattr(QMessageBox, "warning", lambda _p, _t, text: warnings.append(text))
    qtbot.mouseClick(window.garage.changes_box.write_button, Qt.MouseButton.LeftButton)
    assert "KOReader still open" in warnings[0]
    assert '["copt_font_size"] = 24,' in settings.read_text()
    assert window.garage.changed()  # the change is kept, nothing was written


def test_garage_without_a_writer_cannot_write(garage: TuningGarage) -> None:
    slider = garage.findChild(QSlider, "font-size")
    assert slider is not None
    slider.setValue(slider.value() + 2)
    assert garage.changes_box.writer is None and not garage.changes_box.write_button.isEnabled()


def _write_font_size(window: MainWindow, monkeypatch: pytest.MonkeyPatch) -> None:
    slider = window.garage.findChild(QSlider, "font-size")
    assert slider is not None
    slider.setValue(26)
    monkeypatch.setattr(TuningGarage, "confirm_write", lambda _self, _count: True)
    window.garage.write_changes()


def test_health_check_waits_until_koreader_ran(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    window, koreader = _real_reader(tmp_path, monkeypatch)
    qtbot.addWidget(window)
    _write_font_size(window, monkeypatch)
    assert window.check_health() is None  # KOReader not started since: stays pending
    (koreader / "crash.log").write_text("11/26/26-14:00:00 INFO  initializing for device X\n")
    assert window.check_health() is None  # clean start: nothing to say, check done
    assert window.install is not None
    assert healthcheck.load_pending(snapshot_store(window.install).base) is None


def test_failed_plugin_after_a_write_offers_the_snapshot(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    window, koreader = _real_reader(tmp_path, monkeypatch)
    qtbot.addWidget(window)
    _write_font_size(window, monkeypatch)
    (koreader / "crash.log").write_text(
        "11/26/26-14:00:01 ERROR Failed to initialize projecttitle plugin: boom\n"
    )
    box = window.check_health()
    assert box is not None and "Plugin projecttitle" in box.text()
    assert "projecttitle: boom" in box.detailedText()
    previews: list[str] = []

    def confirm(_self: SnapshotsDialog, snapshot: Snapshot, _plan: RestorePlan) -> bool:
        previews.append(snapshot.id)
        return True

    monkeypatch.setattr(SnapshotsDialog, "confirm", confirm)
    monkeypatch.setattr(QMessageBox, "information", lambda *_args: None)
    restore = next(b for b in box.buttons() if b.text() == "Restore snapshot…")
    qtbot.mouseClick(restore, Qt.MouseButton.LeftButton)
    assert len(previews) == 1  # preview before write, then restored
    assert '["copt_font_size"] = 22,' in (koreader / "settings.reader.lua").read_text()


def test_snapshot_folder_keeps_older_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    QStandardPaths.setTestModeEnabled(True)
    make_reader(tmp_path / "PB634K3", "pocketbook")
    install = find_install(tmp_path / "PB634K3")
    monkeypatch.setattr("kodesk.ui.app.volume_serial", lambda _device: "1234-ABCD")
    fresh = snapshot_store(install).base
    assert fresh.name == "Fake-PB634K3-1234-ABCD"
    older = fresh.with_name("Fake-PB634K3")  # made before kodesk knew the serial
    older.mkdir(parents=True)
    assert snapshot_store(install).base == older
    fresh.mkdir(parents=True)
    assert snapshot_store(install).base == fresh
