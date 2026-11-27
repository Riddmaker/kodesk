"""The Wi-Fi window (B-11) with a fake reader and the local test SFTP server."""

from __future__ import annotations

from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

from kodesk.core.luadump import SettingsDocument
from kodesk.core.snapshot import SnapshotStore
from kodesk.core.wifi import AUTHORIZED_KEYS, KeyStore
from kodesk.ui.wifi import KEY_ONLY_TIP, NOT_PAIRED, UsbReader, WifiDialog
from tests.sftp_server import FakeReader, new_host_key, serve

ID = "PBVerseProColor-PB634K3"
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
