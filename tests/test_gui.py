"""Widget tests (pytest-qt, offscreen). Drive the mockup like a user would."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QStandardPaths, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QLabel,
    QListWidget,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSlider,
    QWidget,
)
from pytestqt.qtbot import QtBot

from kodesk.core import healthcheck
from kodesk.core.devices import Face
from kodesk.core.hotcorners import GESTURES_FILE
from kodesk.core.install import find_install
from kodesk.core.librarymode import COVER_BROWSER, read_config
from kodesk.core.snapshot import (
    Progress,
    RestorePlan,
    Snapshot,
    SnapshotCancelledError,
    SnapshotStore,
)
from kodesk.ui.app import MainWindow, demo_context, reader_context, snapshot_store
from kodesk.ui.garage import TuningGarage, View
from kodesk.ui.library_panel import LibraryPanel
from kodesk.ui.snapshots import (
    WRITING,
    SnapshotsDialog,
    human_size,
    plan_text,
    with_snapshot_progress,
)
from tests.conftest import make_pocketbook, make_reader
from tests.test_extensions import SVG
from tests.test_librarymode import make_cache

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
    garage.select_key("menu")
    assert not garage.key_panel.unlock.isVisibleTo(garage)
    assert not garage.key_panel.lock.isVisibleTo(garage)  # no extension can change Menu
    garage.select_key("home")
    panel = garage.key_panel
    # DESIGN rule "Possible with an extension": greyed option, lock note, Store buttons.
    assert panel.unlock.isVisibleTo(garage) and not panel.unlock.isEnabled()
    assert panel.lock.isVisibleTo(garage)
    assert [b.text() for b in panel.lock.buttons] == ["pbfixes → Store"]
    assert garage.device_view.keys["home"].locked
    assert not garage.device_view.keys["menu"].locked
    assert "extension" in garage.device_view.keys["home"].accessibleName()


def test_lock_buttons_open_the_store_prefilled(qtbot: QtBot, pocketbook: Path) -> None:
    window = MainWindow(reader_context(find_install(pocketbook)), offline=True)
    qtbot.addWidget(window)
    window.show()
    window.garage.select_key("home")
    qtbot.mouseClick(window.garage.key_panel.lock.buttons[0], Qt.MouseButton.LeftButton)
    assert window.tabs.currentWidget() is window.store
    assert window.store.search.text() == "pbfixes"
    window.tabs.setCurrentWidget(window.garage)
    qtbot.mouseClick(window.garage.key_panel.lock.more, Qt.MouseButton.LeftButton)
    assert window.store.search.text() == "button"


def test_menu_view_draws_the_readers_icons_and_locks_icon_packs(
    qtbot: QtBot, tmp_path: Path
) -> None:
    reader = make_pocketbook(tmp_path / "PB634K3")
    stock = reader / "applications/koreader/resources/icons/mdlight"
    stock.mkdir(parents=True)
    (stock / "appbar.menu.svg").write_bytes(SVG)
    context = reader_context(find_install(reader))
    assert set(context.icons) == {"appbar.menu"}
    window = MainWindow(context)
    qtbot.addWidget(window)
    window.show()
    garage = window.garage
    garage.set_view(View.MENU)
    with_icon = garage.screen_image()
    spot = garage.device_view.spots["icons"]
    assert spot.isVisible() and spot.spot.locked  # no icon extension installed
    assert garage.icons_lock.isVisibleTo(garage)
    qtbot.mouseClick(spot, Qt.MouseButton.LeftButton)
    assert spot.selected and "needs an extension" in garage.status.text()
    garage.context.icons.clear()
    assert garage.screen_image() != with_icon  # the real icon, not the plain sign
    garage.set_view(View.BOOK)
    assert "icons" not in garage.device_view.spots  # the Book view has the corners


def test_icon_extension_unlocks_the_icons(qtbot: QtBot, tmp_path: Path) -> None:
    reader = make_pocketbook(tmp_path / "PB634K3")
    (reader / "applications/koreader/plugins/iconschanger.koplugin").mkdir(parents=True)
    window = MainWindow(reader_context(find_install(reader)))
    qtbot.addWidget(window)
    window.show()
    garage = window.garage
    garage.set_view(View.MENU)
    assert not garage.device_view.spots["icons"].spot.locked
    assert not garage.icons_lock.isVisibleTo(garage)


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
    assert unlock.isVisible() and unlock.isEnabled() and unlock.isChecked()  # absent = on
    assert not garage.key_panel.lock.isVisible()
    assert not garage.device_view.keys["home"].locked
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


def test_footer_item_inside_the_readers_table_changes_one_key(garage: TuningGarage) -> None:
    title = garage.findChild(QCheckBox, "footer-book_title")
    assert title is not None and not title.isChecked()
    before = garage.screen_image()
    title.setChecked(True)
    assert garage.changed() == [("footer", "book_title")]
    assert garage.screen_image() != before  # the title shows in the drawn status bar
    title.setChecked(False)
    assert not garage.changed()  # a key the reader never had stays unset


def test_footer_without_a_table_gets_koreaders_default_table(qtbot: QtBot, tmp_path: Path) -> None:
    volume = make_pocketbook(tmp_path / "PB", settings={"copt_font_size": 22})
    window = MainWindow(reader_context(find_install(volume)))
    qtbot.addWidget(window)
    garage = window.garage
    prefix = garage.findChild(QComboBox, "footer-item_prefix")
    assert prefix is not None and prefix.currentData() == "icons"
    prefix.setCurrentIndex(prefix.findData("letters"))
    assert garage.changed() == [("footer",)]
    table = garage.edited.get(("footer",))
    assert isinstance(table, dict) and table["item_prefix"] == "letters"
    assert table["page_progress"] is True  # KOReader's defaults, not nil
    prefix.setCurrentIndex(prefix.findData("icons"))
    assert not garage.changed()


def test_hiding_and_showing_the_footer(garage: TuningGarage) -> None:
    shown = garage.findChild(QCheckBox, "footer-shown")
    bold = garage.findChild(QCheckBox, "footer-text_font_bold")
    assert shown is not None and bold is not None and shown.isChecked()
    shown.setChecked(False)
    assert garage.edited.get(("reader_footer_mode",)) == 0 and not bold.isEnabled()
    shown.setChecked(True)
    assert not garage.changed() and bold.isEnabled()


def test_top_bar_items_follow_the_alternative_status_bar(garage: TuningGarage) -> None:
    shown = garage.findChild(QCheckBox, "alt-status-bar")
    percent = garage.findChild(QCheckBox, "header-reading_percent")
    assert shown is not None and percent is not None
    assert shown.isChecked() and not percent.isChecked()  # crengine's default is 0
    percent.setChecked(True)
    assert garage.changed() == [("cre_header_reading_percent",)]
    shown.setChecked(False)
    assert not percent.isEnabled()


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


def _progress_dialog(parent: QWidget) -> QProgressDialog:
    dialog = parent.findChild(QProgressDialog, "snapshot-progress")
    assert dialog is not None
    return dialog


def test_snapshot_progress_can_be_cancelled_before_the_write(qtbot: QtBot) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    written: list[bool] = []

    def action(progress: Progress) -> None:
        progress(0, 300 * 2**20)
        _progress_dialog(parent).cancel()
        progress(100 * 2**20, 300 * 2**20)
        written.append(True)

    with pytest.raises(SnapshotCancelledError):
        with_snapshot_progress(parent, action)
    assert not written


def test_snapshot_progress_then_shows_the_write_without_cancel(qtbot: QtBot) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    seen: list[str] = []

    def action(progress: Progress) -> str:
        progress(0, 2**20)
        assert "0 MB of 1 MB" in _progress_dialog(parent).labelText() or "kB of" in (
            _progress_dialog(parent).labelText()
        )
        progress(2**20, 2**20)
        dialog = _progress_dialog(parent)
        seen.append(dialog.labelText())
        assert dialog.value() == dialog.maximum()
        assert dialog.findChild(QPushButton) is None  # a write is never stopped halfway
        return "done"

    assert with_snapshot_progress(parent, action) == "done"
    assert seen == [WRITING]
    assert not _progress_dialog(parent).isVisible()


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


def _real_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prepare: Callable[[Path], object] = lambda _root: None,
) -> tuple[MainWindow, Path]:
    """A fake PocketBook Verse Pro Color; ``prepare`` gets its KOReader folder first."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    QStandardPaths.setTestModeEnabled(True)
    koreader = make_reader(tmp_path / "PB634K3", "pocketbook")
    (koreader / "version.log").write_text("2026-08-26, 08:01:22, v2026.07.1, PBVerseProColor\n")
    prepare(koreader)
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


def test_sleep_picture_is_chosen_previewed_and_put_on_the_reader(
    qtbot: QtBot, pocketbook: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    QStandardPaths.setTestModeEnabled(True)  # snapshots into a test folder
    install = find_install(pocketbook)
    window = MainWindow(reader_context(install), install, offline=True)
    qtbot.addWidget(window)
    garage = window.garage
    write = garage.findChild(QPushButton, "sleep-write")
    assert write is not None and not write.isEnabled()  # nothing chosen yet
    picture = QImage(300, 200, QImage.Format.Format_RGB32)
    picture.fill(QColor("red"))
    source = tmp_path / "cat.png"
    picture.save(str(source))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_args: (str(source), ""))
    choose = garage.findChild(QPushButton, "sleep-choose")
    assert choose is not None
    choose.click()  # keyboard users get here with Alt+C, no drag and drop needed
    assert garage.view is View.SLEEP and write.isEnabled()
    shown = garage.screen_image()
    assert (shown.width(), shown.height()) == garage.context.device.screen_px
    assert shown.pixelColor(0, 0) == QColor("white")  # letterboxed on white
    monkeypatch.setattr(TuningGarage, "confirm_sleep_write", lambda _self: True)
    guides: list[str] = []
    monkeypatch.setattr(QMessageBox, "information", lambda _p, _t, text: guides.append(text))
    monkeypatch.setattr(QMessageBox, "warning", lambda _p, _t, text: guides.append(text))
    write.click()
    placed = list((pocketbook / "Photo").glob("kodesk-sleep-*.png"))
    assert len(placed) == 1
    assert QImage(str(placed[0])).size() == shown.size()
    assert "Settings → Personalize → Logos" in guides[0]
    assert snapshot_store(find_install(pocketbook)).snapshots()  # snapshot first (hard rule)


def test_library_look_is_previewed_without_a_reader(qtbot: QtBot) -> None:
    window = MainWindow(demo_context(), offline=True)
    qtbot.addWidget(window)
    garage = window.garage
    combo = garage.findChild(QComboBox, "library-mode")
    write = garage.findChild(QPushButton, "library-write")
    assert combo is not None and write is not None
    garage.set_view(View.LIBRARY)
    classic = garage.screen_image()
    combo.setCurrentIndex(combo.findData("mosaic_image"))  # keyboard: Alt+L, then arrows
    assert garage.view is View.LIBRARY
    assert garage.screen_image() != classic
    assert not garage.changed()  # not a settings.reader.lua change
    assert not write.isEnabled()  # demo: nothing to write to
    assert garage.status.text() == "Library · Mosaic with cover images"


def test_library_look_needs_the_cache_to_write(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    window, _koreader = _real_reader(tmp_path, monkeypatch)  # KOReader never drew covers
    qtbot.addWidget(window)
    panel = window.garage.library_panel
    panel.combo.setCurrentIndex(panel.combo.findData("mosaic_image"))
    assert not panel.write_button.isEnabled()
    assert "open the library on the reader once" in panel.write_button.toolTip()


def _reader_with_cover_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[MainWindow, Path]:
    window, koreader = _real_reader(
        tmp_path,
        monkeypatch,
        lambda root: make_cache(
            root / COVER_BROWSER.cache, {"filemanager_display_mode": "list_image_meta"}
        ),
    )
    return window, koreader / COVER_BROWSER.cache


def test_library_look_is_written_into_the_cache_after_a_snapshot(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    window, cache = _reader_with_cover_browser(tmp_path, monkeypatch)
    qtbot.addWidget(window)
    panel = window.garage.library_panel
    assert panel.combo.currentData() == "list_image_meta"
    assert not panel.write_button.isEnabled()
    panel.combo.setCurrentIndex(panel.combo.findData("mosaic_image"))
    assert panel.write_button.isEnabled()
    assert "  filemanager_display_mode: list_image_meta → mosaic_image" in panel.preview_lines()
    monkeypatch.setattr(LibraryPanel, "confirm", lambda _self: True)
    panel.write_button.click()
    assert read_config(cache)["filemanager_display_mode"] == "mosaic_image"
    assert window.garage.status.text().startswith("Library look written.")
    assert not panel.write_button.isEnabled()  # the reader has it now
    assert install_snapshots(window) == 1


def test_library_look_refused_while_koreader_has_the_cache_open(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    window, cache = _reader_with_cover_browser(tmp_path, monkeypatch)
    qtbot.addWidget(window)
    panel = window.garage.library_panel
    panel.combo.setCurrentIndex(panel.combo.findData("mosaic_text"))
    cache.with_name(cache.name + "-wal").write_bytes(b"")  # KOReader started meanwhile
    warnings: list[str] = []
    monkeypatch.setattr(LibraryPanel, "confirm", lambda _self: True)
    monkeypatch.setattr(QMessageBox, "warning", lambda _p, _t, text: warnings.append(text))
    panel.write_button.click()
    assert "Close KOReader" in warnings[0]
    assert read_config(cache)["filemanager_display_mode"] == "list_image_meta"
    assert install_snapshots(window) == 0


def test_corners_show_the_readers_gestures_and_keep_unknown_ones(garage: TuningGarage) -> None:
    corners = garage.corners
    assert corners.combos["tap_top_left_corner"].currentText() == "Toggle page flipping"
    assert corners.combos["tap_right_bottom_corner"].currentText() == "Pass through"
    kept = corners.combos["hold_bottom_right_corner"]
    assert kept.currentText() == "screenshot + toggle_wifi"
    assert "keeps it exactly" in kept.itemData(0, Qt.ItemDataRole.ToolTipRole)
    assert set(garage.device_view.spots) == {
        "corner-top-left",
        "corner-top-right",
        "corner-bottom-left",
        "corner-bottom-right",
    }
    garage.select_spot("corner-top-right")
    assert corners.combos["tap_top_right_corner"].hasFocus()
    assert garage.status.text() == (
        "Top right corner: tap → Toggle bookmark · long press → Pass through"
    )
    assert not corners.changed()


def test_corner_change_is_written_with_one_entry_changed(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    window, koreader = _real_reader(tmp_path, monkeypatch)
    qtbot.addWidget(window)
    garage = window.garage
    gestures = koreader / GESTURES_FILE
    old = gestures.read_text()
    combo = garage.corners.combos["hold_bottom_right_corner"]
    combo.setCurrentIndex(combo.findData("toc"))
    combo.setCurrentIndex(0)  # back to the reader's own two actions: nothing pending
    assert not garage.changes_box.write_button.isEnabled()
    tap = garage.corners.combos["tap_right_bottom_corner"]
    tap.setCurrentIndex(tap.findData("night_mode"))  # keyboard: Tab to it, arrow keys
    assert garage.changes_box.lines() == [
        "gestures.lua: Bottom right corner, tap: Pass through → Toggle night mode"
    ]
    monkeypatch.setattr(TuningGarage, "confirm_write", lambda _self, count: count == 1)
    garage.changes_box.write_button.click()
    new = gestures.read_text()
    added = [line for line in new.splitlines() if line not in old.splitlines()]
    assert added == [
        '        ["tap_right_bottom_corner"] = {',
        '            ["night_mode"] = true,',
    ]
    assert garage.status.text().startswith("Written to your reader.")
    assert not garage.corners.changed()
    assert install_snapshots(window) == 1


def test_corners_without_a_gestures_file_are_read_only(qtbot: QtBot) -> None:
    window = MainWindow(demo_context(), offline=True)
    qtbot.addWidget(window)
    corners = window.garage.corners
    assert corners.combos["tap_top_right_corner"].currentText() == "Toggle bookmark"  # default
    assert not corners.combos["tap_top_right_corner"].isEnabled()
    assert corners.findChild(QLabel, "corners-no-file") is not None
