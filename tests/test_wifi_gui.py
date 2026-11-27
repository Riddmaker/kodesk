"""The Wi-Fi window (B-11) with a fake reader and the local test SFTP server."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QStandardPaths
from pytestqt.qtbot import QtBot

from kodesk.core import catalogue as cat
from kodesk.core.luadump import SettingsDocument
from kodesk.core.mirror import WifiReader
from kodesk.core.snapshot import SnapshotStore
from kodesk.core.wifi import AUTHORIZED_KEYS, KeyStore
from kodesk.ui.app import MainWindow, demo_context
from kodesk.ui.wifi import KEY_ONLY_TIP, NOT_PAIRED, UsbReader, WifiDialog
from tests.conftest import FakeGitHub, fake_catalogue, make_reader, plugin_files, plugin_zip
from tests.sftp_server import FakeReader, new_host_key, serve
from tests.test_changes import CLOCK, Dialogs

ID = "PBVerseProColor-PB634K3"
KOREADER = "/mnt/ext1/applications/koreader"
SETTINGS = SettingsDocument.parse('return {\n    ["SSH_port"] = "2223",\n}\n')


@pytest.fixture(name="dialog")
def fixture_dialog(
    qtbot: QtBot,
    reader: Path,
    store: SnapshotStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> WifiDialog:
    """The window with the fake reader plugged in; every question answered with OK."""
    monkeypatch.setattr(WifiDialog, "confirm", lambda _self, _title, _text: True)
    usb = UsbReader(ID, "PocketBook Verse Pro", SETTINGS, reader, store)
    dialog = WifiDialog(KeyStore(tmp_path / "wifi"), usb, None)
    qtbot.addWidget(dialog)
    return dialog


def test_pair_then_test_then_forget(dialog: WifiDialog, qtbot: QtBot, tmp_path: Path) -> None:
    assert dialog.state.text().startswith(NOT_PAIRED) and KEY_ONLY_TIP in dialog.state.text()
    assert dialog.port.value() == 2223 and dialog.user.text() == "reader"
    assert not dialog.test_button.isEnabled() and not dialog.forget_button.isEnabled()
    dialog.pair_button.click()
    assert "Paired" in dialog.message.text()
    assert dialog.reader.currentText() == "PocketBook Verse Pro"
    assert dialog.test_button.isEnabled() and "Pair again" in dialog.pair_button.text()
    keys_file = dialog.usb.koreader_dir / AUTHORIZED_KEYS  # type: ignore[union-attr]
    assert keys_file.read_bytes().count(b"\n") == 1

    fake = FakeReader(tmp_path / "served", keys_file, new_host_key()[0])
    fake.root.mkdir()
    dialog.host.setText("8.8.8.8")
    dialog.test_button.click()
    assert "local network" in dialog.message.text()  # refused before any connection
    with serve(fake) as port:
        dialog.host.setText("127.0.0.1")
        dialog.port.setValue(port)
        dialog.test_button.click()
        qtbot.waitUntil(
            lambda: dialog.message.text().startswith(("Connected", "Not")), timeout=10_000
        )
    assert dialog.message.text().startswith("Connected"), dialog.message.text()
    assert "identity is now known" in dialog.message.text()
    saved = dialog.pairing()
    assert saved is not None and saved.port == port and saved.host_key is not None

    dialog.forget_button.click()
    assert keys_file.read_bytes() == b""
    assert dialog.pairing() is None and dialog.reader.currentText().endswith("(not paired)")


def test_without_usb_nothing_can_be_paired(qtbot: QtBot, tmp_path: Path) -> None:
    dialog = WifiDialog(KeyStore(tmp_path / "wifi"), None, None)
    qtbot.addWidget(dialog)
    assert not dialog.pair_button.isEnabled() and not dialog.test_button.isEnabled()
    assert "plug one in over USB" in dialog.state.text()


def test_every_control_has_a_keyboard_way(dialog: WifiDialog) -> None:
    for button in (dialog.pair_button, dialog.test_button, dialog.forget_button):
        assert "&" in button.text()


def test_connect_then_install_a_plugin_over_wifi(
    qtbot: QtBot, tmp_path: Path, store: SnapshotStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """e2e: pair over "USB", connect over Wi-Fi, install from the Store; it lands on the reader."""
    QStandardPaths.setTestModeEnabled(True)
    monkeypatch.setattr(WifiDialog, "confirm", lambda _self, _title, _text: True)
    dialogs = Dialogs(monkeypatch)
    served = tmp_path / "served"
    koreader = make_reader(served / "mnt" / "ext1", "pocketbook")
    (koreader / "version.log").write_text("2026-08-26, 08:01:22, v2026.07.2, PBVerseProColor\n")
    settings_before = (koreader / "settings.reader.lua").read_bytes()
    usb = UsbReader(ID, "PocketBook Verse Pro", SETTINGS, koreader, store, KOREADER)
    dialog = WifiDialog(KeyStore(tmp_path / "wifi"), usb, None, tmp_path / "mirrors")
    qtbot.addWidget(dialog)
    dialog.pair_button.click()
    assert dialog.connect_button.isEnabled()
    shown: list[WifiReader] = []
    dialog.connected.connect(shown.append)
    fake = FakeReader(served, koreader / AUTHORIZED_KEYS, new_host_key()[0], writable=True)
    with serve(fake) as port:
        dialog.host.setText("127.0.0.1")
        dialog.port.setValue(port)
        dialog.connect_button.click()
        assert shown, dialog.message.text()

        main = MainWindow(demo_context(), None, offline=True)
        qtbot.addWidget(main)
        assert main.show_wifi_reader(shown[0]) is None
        assert "over Wi-Fi (127.0.0.1)" in main.statusBar().currentMessage()
        assert not main.koreader_action.isEnabled()  # a KOReader update needs USB
        assert main.garage.changes_box.writer is None  # the Garage only shows (B-26)
        assert "Alice/" not in main.garage.context.library_entries
        assert "Alice" in main.garage.context.library_entries  # the book, read over Wi-Fi

        github = FakeGitHub()
        main.changes.get = github
        main.store.set_catalogue(cat.parse(*fake_catalogue()))
        zip_v1 = plugin_zip(plugin_files("readingclock.koplugin", "1.0"))
        github.release(*CLOCK, "v1.0", {"readingclock.koplugin.zip": zip_v1})
        main.store.search.setText("clock")
        main.store.enter_results()
        main.store.details.install_button.click()
        folder = koreader / "plugins" / "readingclock.koplugin"
        qtbot.waitUntil(lambda: bool(dialogs.messages), timeout=20_000)
        assert folder.is_dir(), dialogs.messages
        assert (koreader / "settings.reader.lua").read_bytes() == settings_before
