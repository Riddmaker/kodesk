"""Widget tests (pytest-qt, offscreen). Drive the mockup like a user would."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QListWidget, QSlider
from pytestqt.qtbot import QtBot

from kodesk.core.devices import Face
from kodesk.core.install import find_install
from kodesk.ui.app import MainWindow, demo_context, main, reader_context
from kodesk.ui.garage import TuningGarage, View
from tests.conftest import make_pocketbook

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


def test_smoke_test_flag_starts_and_quits(qapp: object) -> None:
    """What CI runs on every packaged build: the window opens and the app exits with 0."""
    del qapp
    assert main(["--demo", "--smoke-test"]) == 0
