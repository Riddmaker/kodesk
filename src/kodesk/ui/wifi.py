"""The Wi-Fi window (B-11): pair a reader over USB, then test the connection over Wi-Fi.

Pairing writes one line on the reader (preview, snapshot first); testing connects in the
background, so the window never freezes while the reader does not answer. Connect (B-27) brings
kodesk's copy of the reader in step and shows it in the main window.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from kodesk.core import wifi
from kodesk.core.install import InstallError
from kodesk.core.luadump import SettingsDocument
from kodesk.core.mirror import MirrorError, WifiReader
from kodesk.core.snapshot import SnapshotError, SnapshotStore
from kodesk.core.wifi import KeyStore, Pairing, WifiError
from kodesk.ui.snapshots import with_snapshot_progress
from kodesk.ui.store import Results, run_in_background

NOT_PAIRED = (
    "Not paired yet. Pairing puts kodesk's own key on the reader over USB (snapshot first); "
    "after that kodesk can reach it over Wi-Fi."
)
PAIRED = "Paired. "
PINNED = "The reader's identity (host key) is known; kodesk talks only to this reader."
PIN_LATER = "kodesk learns the reader's identity (host key) on the first connection."
KEY_ONLY_TIP = (
    "Tip: on the reader, switch on “Login with key only” (KOReader: Network → SSH server), "
    "so nobody can log in with a password."
)
READING = "Reading your reader over Wi-Fi (only what changed since last time)…"
START_SSH = (
    "Start the SSH server on the reader (KOReader: Network → SSH server), type the IP "
    "address it shows, then test the connection."
)


@dataclass(frozen=True)
class UsbReader:
    """The reader plugged in over USB, if any: the only one that can be paired."""

    reader_id: str
    name: str
    settings: SettingsDocument
    koreader_dir: Path
    store: SnapshotStore
    koreader_path: str = ""  # the KOReader folder as the reader names it (install.reader_path)


class WifiDialog(QDialog):  # pylint: disable=too-many-instance-attributes  # widgets
    """Paired readers, pairing, connection test, connecting and forgetting a reader."""

    connected = Signal(object)  # a WifiReader whose mirror is in step

    def __init__(
        self,
        keys: KeyStore,
        usb: UsbReader | None,
        parent: QWidget | None,
        mirrors: Path | None = None,
    ) -> None:
        super().__init__(parent)
        self.mirrors = mirrors  # where the copies of readers live; None: no Connect
        self.setObjectName("wifi-dialog")
        self.setWindowTitle("Wi-Fi")
        self.keys, self.usb = keys, usb
        self._results = Results(self)
        self._results.done.connect(self._tested)
        self.reader = QComboBox(self)
        self.reader.setObjectName("wifi-reader")
        self.state = QLabel(self)
        self.state.setObjectName("wifi-state")
        self.state.setWordWrap(True)
        self.host = QLineEdit(self)
        self.host.setObjectName("wifi-host")
        self.host.setPlaceholderText("192.168.1.23")
        self.port = QSpinBox(self)
        self.port.setObjectName("wifi-port")
        self.port.setRange(1, 65535)
        self.user = QLineEdit(self)
        self.user.setObjectName("wifi-user")
        self.user.setToolTip("reader on PocketBook, root on Kobo and Kindle")
        form = QFormLayout()
        for text, widget in (
            ("&Reader:", self.reader),
            ("", self.state),
            ("IP &address:", self.host),
            ("P&ort:", self.port),
            ("&User name:", self.user),
        ):
            form.addRow(text, widget)
        self.pair_button = self._button("&Pair over USB…", "wifi-pair", self.pair)
        self.test_button = self._button("&Test connection", "wifi-test", self.test)
        self.connect_button = self._button("Co&nnect", "wifi-connect", self.connect_reader)
        self.connect_button.setToolTip(
            "Read the reader over Wi-Fi and show it; plugins and patches can then be changed"
        )
        self.forget_button = self._button("&Forget this reader…", "wifi-forget", self.forget)
        row = QHBoxLayout()
        buttons = (self.pair_button, self.test_button, self.connect_button, self.forget_button)
        for button in buttons:
            row.addWidget(button)
        row.addStretch(1)
        self.message = QLabel(self)
        self.message.setObjectName("wifi-result")
        self.message.setWordWrap(True)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        close.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(row)
        layout.addWidget(self.message)
        layout.addStretch(1)
        layout.addWidget(close)
        self.reader.currentIndexChanged.connect(lambda _i: self.refresh())
        self.fill()
        self.resize(560, 320)

    def _button(self, text: str, name: str, slot: object) -> QPushButton:
        button = QPushButton(text, self)
        button.setObjectName(name)
        button.clicked.connect(slot)
        return button

    # -- state ----------------------------------------------------------------------------

    def fill(self, select: str | None = None) -> None:
        """The readers: every paired one, plus the USB reader if it is not paired yet."""
        names = {p.reader_id: p.name for p in self.keys.pairings()}
        if self.usb is not None:
            names.setdefault(self.usb.reader_id, f"{self.usb.name} (not paired)")
            select = select or self.usb.reader_id
        self.reader.blockSignals(True)
        self.reader.clear()
        for reader_id, name in names.items():
            self.reader.addItem(name, reader_id)
        index = self.reader.findData(select) if select else 0
        self.reader.setCurrentIndex(max(index, 0))
        self.reader.blockSignals(False)
        self.refresh()

    def reader_id(self) -> str | None:
        value = self.reader.currentData()
        return value if isinstance(value, str) else None

    def pairing(self) -> Pairing | None:
        reader_id = self.reader_id()
        return self.keys.load(reader_id) if reader_id else None

    def _on_usb(self) -> bool:
        return self.usb is not None and self.usb.reader_id == self.reader_id()

    def refresh(self) -> None:
        """Fields and buttons for the selected reader."""
        pairing = self.pairing()
        on_usb = self._on_usb()
        if pairing is None:
            text = NOT_PAIRED if on_usb else "No reader paired yet: plug one in over USB."
            self.port.setValue(wifi.ssh_port(self.usb.settings if on_usb and self.usb else None))
            self.user.setText(wifi.DEFAULT_USER)
            self.host.clear()
        else:
            text = PAIRED + (PINNED if pairing.host_key else PIN_LATER)
            self.port.setValue(pairing.port)
            self.user.setText(pairing.user)
            self.host.setText(pairing.host)
        if on_usb and self.usb is not None and not wifi.key_only(self.usb.settings):
            text += "\n" + KEY_ONLY_TIP
        self.state.setText(text)
        self.pair_button.setEnabled(on_usb)
        self.pair_button.setText("&Pair again over USB…" if pairing else "&Pair over USB…")
        self.test_button.setEnabled(pairing is not None)
        self.connect_button.setEnabled(pairing is not None and self.mirrors is not None)
        leftover = (
            on_usb and self.reader_id() is not None and self.keys.leftover(self.reader_id() or "")
        )
        self.forget_button.setEnabled(pairing is not None or bool(leftover))

    # -- actions --------------------------------------------------------------------------

    def confirm(self, title: str, text: str) -> bool:
        """Ask before a change (tests replace this)."""
        box = QMessageBox(QMessageBox.Icon.Question, title, text, parent=self)
        box.setObjectName("wifi-confirm")
        box.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
        return box.exec() == QMessageBox.StandardButton.Ok

    def pair(self) -> None:
        """Put kodesk's key on the USB reader, inside a snapshot."""
        usb = self.usb
        if usb is None or not self._on_usb():
            return
        lines = "\n".join(wifi.pair_lines(usb.koreader_dir))
        text = f"kodesk will change on your reader:\n{lines}\n\nA snapshot is taken first."
        if not self.confirm("Pair over USB", text):
            return
        try:
            user = wifi.check_user(self.user.text().strip())
            start = Pairing(usb.reader_id, usb.name, user=user, koreader_path=usb.koreader_path)
            paired, _snapshot = with_snapshot_progress(
                self,
                lambda progress: wifi.pair(
                    usb.store, usb.koreader_dir, self.keys, start, usb.settings, progress
                ),
            )
        except (WifiError, SnapshotError, OSError) as error:
            self.message.setText(f"Not paired: {error}")
            return
        self.fill(paired.reader_id)
        self.message.setText("Paired. " + START_SSH)

    def test(self) -> None:
        """Connect in the background and look around once."""
        pairing = self.pairing()
        if pairing is None:
            return
        try:
            host = wifi.check_host(self.host.text())
            user = wifi.check_user(self.user.text().strip())
        except WifiError as error:
            self.message.setText(str(error))
            return
        trying = replace(pairing, port=self.port.value(), user=user)
        self.test_button.setEnabled(False)
        self.message.setText(f"Connecting to {host}…")

        def work() -> str:
            with wifi.connect(self.keys, trying, host) as connection:
                folder = connection.sftp.normalize(".")
                first = " The reader's identity is now known." if connection.pinned else ""
                return f"Connected to {pairing.name} at {host} (folder {folder}).{first}"

        run_in_background(self._results, pairing.reader_id, work)

    def connect_reader(self) -> None:
        """Bring kodesk's copy of the reader in step over Wi-Fi, then show it."""
        pairing = self.pairing()
        if pairing is None or self.mirrors is None:
            return
        try:
            user = wifi.check_user(self.user.text().strip())
            trying = replace(pairing, port=self.port.value(), user=user)
            reader = WifiReader.open(self.keys, trying, self.host.text(), self.mirrors)
            with_snapshot_progress(self, reader.sync, None, READING)
        except (WifiError, MirrorError, SnapshotError, InstallError, OSError) as error:
            self.message.setText(f"Not connected: {error}")
            return
        self.connected.emit(reader)
        self.accept()

    def _tested(self, _token: str, result: object) -> None:
        self.test_button.setEnabled(True)
        if isinstance(result, Exception):  # WifiError, or a network failure paramiko raised
            self.message.setText(f"Not connected: {result}")
        else:
            self.message.setText(str(result))
            self.refresh()

    def forget(self) -> None:
        """Delete key and pin; take kodesk's line off the reader when it is plugged in."""
        reader_id = self.reader_id()
        if reader_id is None:
            return
        on_usb = self._on_usb()
        text = (
            "kodesk deletes its key for this reader and removes its line from "
            f"{wifi.AUTHORIZED_KEYS} on the reader (snapshot first)."
            if on_usb
            else "kodesk deletes its key for this reader. Its line stays on the reader until "
            "you plug the reader in and choose Forget again (or remove it yourself)."
        )
        if not self.confirm("Forget this reader", text):
            return
        usb = self.usb
        try:
            if on_usb and usb is not None:
                with_snapshot_progress(
                    self,
                    lambda progress: wifi.unpair(
                        usb.store, usb.koreader_dir, self.keys, reader_id, progress
                    ),
                )
            else:
                self.keys.forget(reader_id, keep_line=True)
        except (SnapshotError, OSError) as error:
            self.message.setText(f"Not forgotten: {error}")
            return
        self.message.setText("Forgotten.")
        self.fill()
