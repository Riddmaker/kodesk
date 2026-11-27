"""The Wi-Fi window (B-11) with a fake reader and the local test SFTP server."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from PySide6.QtCore import QStandardPaths
from PySide6.QtWidgets import QSlider
from pytestqt.qtbot import QtBot

from kodesk import __version__
from kodesk.core import catalogue as cat
from kodesk.core import remote_garage as rg
from kodesk.core.install import find_install
from kodesk.core.luadump import SettingsDocument
from kodesk.core.mirror import WifiReader
from kodesk.core.remote_garage import PatchState
from kodesk.core.snapshot import SnapshotStore
from kodesk.core.wifi import AUTHORIZED_KEYS, KeyStore
from kodesk.ui.app import MainWindow, demo_context, reader_context
from kodesk.ui.garage import TuningGarage
from kodesk.ui.wifi import KEY_ONLY_TIP, NOT_PAIRED, UsbReader, WifiDialog
from kodesk.ui.wifi_garage import WifiGarage
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


@contextmanager
def _over_wifi(
    qtbot: QtBot, tmp_path: Path, store: SnapshotStore, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[MainWindow, Path, WifiReader]]:
    """Pair over "USB", connect over Wi-Fi to the local test server, show it in a main window."""
    QStandardPaths.setTestModeEnabled(True)
    monkeypatch.setattr(WifiDialog, "confirm", lambda _self, _title, _text: True)
    served = tmp_path / "served"
    koreader = make_reader(served / "mnt" / "ext1", "pocketbook")
    (koreader / "version.log").write_text("2026-08-26, 08:01:22, v2026.07.2, PBVerseProColor\n")
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
        yield main, koreader, shown[0]


def test_connect_then_install_a_plugin_over_wifi(
    qtbot: QtBot, tmp_path: Path, store: SnapshotStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """e2e: pair over "USB", connect over Wi-Fi, install from the Store; it lands on the reader."""
    dialogs = Dialogs(monkeypatch)
    with _over_wifi(qtbot, tmp_path, store, monkeypatch) as (main, koreader, _wifi):
        settings_before = (koreader / "settings.reader.lua").read_bytes()
        assert "over Wi-Fi (127.0.0.1)" in main.statusBar().currentMessage()
        assert main.koreader_action.isEnabled()  # sent for the launcher over Wi-Fi (B-27b)
        assert main.koupdate.send is not None
        assert main.garage.changes_box.writer is not None  # sent for the patch (B-26)
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


def test_garage_changes_over_wifi_wait_for_koreader(
    qtbot: QtBot, tmp_path: Path, store: SnapshotStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """e2e (B-26): a Garage change becomes the patch plus a change file on the reader; the
    settings stay untouched; the next connect shows what KOReader did and offers an undo."""
    dialogs = Dialogs(monkeypatch)
    asked: list[PatchState] = []
    monkeypatch.setattr(TuningGarage, "confirm_write", lambda _self, _count: True)

    def consent(_self: WifiGarage, state: PatchState) -> bool:
        asked.append(state)
        return True

    monkeypatch.setattr(WifiGarage, "consent", consent)
    with _over_wifi(qtbot, tmp_path, store, monkeypatch) as (main, koreader, wifi):
        settings_before = (koreader / "settings.reader.lua").read_bytes()
        size = main.garage.findChild(QSlider, "font-size")
        assert size is not None
        old = size.value()
        size.setValue(old + 4)
        main.garage.write_changes()
        assert "Restart KOReader" in main.garage.status.text(), dialogs.messages
        assert asked == ["missing"]
        assert (koreader / rg.PATCH).read_bytes() == rg.patch_source(__version__)
        sent = rg.read_pending((koreader / rg.PENDING).read_bytes())
        assert sent == [rg.Change("reader", ("copt_font_size",), old, old + 4)]
        assert (koreader / "settings.reader.lua").read_bytes() == settings_before

        size.setValue(old + 6)  # a second change merges into the waiting one, no new question
        main.garage.write_changes()
        assert asked == ["missing"]
        sent = rg.read_pending((koreader / rg.PENDING).read_bytes())
        assert sent == [rg.Change("reader", ("copt_font_size",), old, old + 6)]

        # KOReader restarts: the patch applied the change (as tests/test_remote_garage.py runs it).
        (koreader / rg.PENDING).unlink()
        result = {"format": 1, "id": "a-1", "applied": [sent[0].to_json()], "skipped": {}}
        (koreader / rg.RESULT).write_text(json.dumps(result))
        wifi.sync()
        assert main.show_wifi_reader(wifi) is None
        remote = main.remote_garage
        assert remote is not None
        box = remote.show_result()
        assert box is not None and "applied 1 setting" in box.text()
        assert "another value by now" in box.informativeText()  # the fake did not change it
        box.close()
        assert remote.show_result() is None  # shown once
        assert remote.undo(rg.read_result((koreader / rg.RESULT).read_bytes()))
        undone = rg.read_pending((koreader / rg.PENDING).read_bytes())
        assert undone == [rg.Change("reader", ("copt_font_size",), old + 6, old)]


def test_usb_connect_shows_the_result_and_drops_waiting_changes(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B-26 over USB: the result KOReader left is shown once, and changes still waiting for
    KOReader can be kept or dropped (inside a snapshot)."""
    QStandardPaths.setTestModeEnabled(True)
    dialogs = Dialogs(monkeypatch)
    koreader = make_reader(tmp_path / "PB634K3", "pocketbook")
    (koreader / "version.log").write_text("2026-08-26, 08:01:22, v2026.07.2, PBVerseProColor\n")
    install = find_install(koreader)
    main = MainWindow(reader_context(install), install, offline=True)
    qtbot.addWidget(main)
    applied = rg.Change("reader", ("copt_font_size",), 22, 26)
    waiting = rg.Change("reader", ("copt_line_spacing",), 100, 110)
    (koreader / rg.FOLDER).mkdir()
    result = {"format": 1, "id": "a-1", "applied": [applied.to_json()], "skipped": {}}
    (koreader / rg.RESULT).write_text(json.dumps(result))
    (koreader / rg.PENDING).write_bytes(rg.pending_document(rg.new_id(), [waiting]))
    shown, asked = main.check_wifi_changes()
    assert shown is not None and "applied 1 setting" in shown.text()
    assert asked is not None and "1 setting sent over Wi-Fi still waits" in asked.text()
    assert waiting.label() in asked.detailedText()
    assert main.check_wifi_changes()[0] is None  # the result is shown once
    drop = next(b for b in asked.buttons() if b.text() == "&Drop them")
    drop.click()
    qtbot.waitUntil(lambda: bool(dialogs.messages))
    assert dialogs.messages == ["Dropped: KOReader will not apply the changes sent over Wi-Fi."]
    assert not (koreader / rg.PENDING).exists()
    assert (koreader / rg.RESULT).is_file()
    assert main.check_wifi_changes() == (None, None)
