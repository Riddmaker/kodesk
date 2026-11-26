"""Application entry point: ``kodesk [PATH]`` where PATH is a reader mount or KOReader folder."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import QStandardPaths, QStorageInfo, QTimer
from PySide6.QtGui import QAction, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QWidget,
)

from kodesk import APP_ID, APP_NAME, __version__
from kodesk.core import healthcheck
from kodesk.core.devices import DEVICES
from kodesk.core.install import InstallError, KOReaderInstall, find_install
from kodesk.core.luadump import SettingsDocument
from kodesk.core.settings_write import write_settings
from kodesk.core.snapshot import SnapshotStore, device_id
from kodesk.ui.garage import ReaderContext, TuningGarage
from kodesk.ui.screen import load_device_fonts
from kodesk.ui.snapshots import SnapshotsDialog

DEMO_DEVICE = "pocketbook-verse-pro-color"
BOOK_SUFFIXES = {".epub", ".pdf", ".fb2", ".mobi", ".azw3", ".djvu", ".cbz", ".txt"}
MAX_LIBRARY_ENTRIES = 200


def demo_context() -> ReaderContext:
    return ReaderContext(device=DEVICES[DEMO_DEVICE], settings=SettingsDocument.parse("return {}"))


def _library_entries(home: Path) -> tuple[str, ...]:
    """Folder and book names in the reader's home folder (names only, read-only)."""
    if not home.is_dir():
        return ()
    entries = sorted(
        (
            p
            for p in home.iterdir()
            if (p.is_dir() and not p.name.endswith(".sdr")) or p.suffix.lower() in BOOK_SUFFIXES
        ),
        key=lambda p: (not p.is_dir(), p.name.lower()),
    )
    return tuple(f"{p.name}/" if p.is_dir() else p.stem for p in entries[:MAX_LIBRARY_ENTRIES])


def _volume_root(install: KOReaderInstall) -> Path:
    relative = {"pocketbook": 2, "kobo": 2, "kindle": 1}.get(install.platform, 1)
    return install.root.parents[relative - 1]


def reader_context(install: KOReaderInstall) -> ReaderContext:
    """Read everything the garage shows from a connected reader."""
    if install.device is None:
        raise InstallError(
            f"Reader model {install.model or 'unknown'} has no device definition yet."
        )
    volume = _volume_root(install)
    settings = install.read_settings()
    home = settings.get(("home_dir",))
    home_path = volume / "Books"
    if isinstance(home, str) and home.startswith("/mnt/ext1/"):  # PocketBook internal storage
        home_path = volume / home.removeprefix("/mnt/ext1/")
    sleep = volume / "system/logo/sleeplogo/pb_sleeplogo.bmp"
    return ReaderContext(
        device=install.device,
        settings=settings,
        koreader_version=install.version,
        font_families=tuple(load_device_fonts(install.fonts_dir)),
        library_entries=_library_entries(home_path),
        sleep_image=sleep if sleep.is_file() else None,
        plugins=install.plugins(),
        source_label=f"KOReader {install.version or '?'} on {volume}",
    )


def snapshot_store(install: KOReaderInstall) -> SnapshotStore:
    """This reader's snapshots in the platform's app data folder (never on the reader)."""
    data = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation))
    return SnapshotStore(data / "snapshots" / device_id(install.model, _volume_root(install).name))


def detect_reader() -> KOReaderInstall | None:
    """First mounted volume that holds a KOReader installation."""
    for volume in QStorageInfo.mountedVolumes():
        if not volume.isValid() or not volume.isReady():
            continue
        try:
            return find_install(Path(volume.rootPath()))
        except (InstallError, OSError):
            continue
    return None


class MainWindow(QMainWindow):
    """Main window: menu bar, the Tuning-Garage, and the reader source in the status bar."""

    def __init__(self, context: ReaderContext, install: KOReaderInstall | None = None) -> None:
        super().__init__()
        self.install: KOReaderInstall | None = None
        self.setWindowTitle(f"{APP_NAME} — Tuning-Garage")
        self.resize(1280, 860)
        open_action = QAction("&Open reader…", self)
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.triggered.connect(self.choose_reader)
        quit_action = QAction("&Quit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu = self.menuBar().addMenu("&File")
        file_menu.addAction(open_action)
        file_menu.addSeparator()
        file_menu.addAction(quit_action)
        self.snapshots_action = QAction("&Snapshots…", self)
        self.snapshots_action.triggered.connect(self.show_snapshots)
        self.menuBar().addMenu("&Reader").addAction(self.snapshots_action)
        self.snapshots_button = QPushButton("Snapshots…", self)
        self.snapshots_button.setObjectName("snapshots")
        self.snapshots_button.setToolTip("Snapshots of your reader, restore in one click")
        self.snapshots_button.clicked.connect(self.show_snapshots)
        self.statusBar().addPermanentWidget(self.snapshots_button)
        self.set_context(context, install)

    def set_context(self, context: ReaderContext, install: KOReaderInstall | None = None) -> None:
        """Show a reader (or the demo); snapshots only exist for a real reader."""
        self.install = install
        self.snapshots_action.setEnabled(install is not None)
        self.snapshots_button.setVisible(install is not None)
        self.garage = TuningGarage(context, self)
        old: QWidget | None = self.centralWidget()
        self.setCentralWidget(self.garage)
        if old is not None:
            old.deleteLater()
        self.statusBar().showMessage(context.source_label)
        if install is not None:
            self.garage.set_writer(
                lambda before, after: self._write_settings(install, before, after)
            )
            QTimer.singleShot(0, self.check_health)

    @staticmethod
    def _write_settings(
        install: KOReaderInstall, before: SettingsDocument, after: SettingsDocument
    ) -> str:
        snapshot = write_settings(snapshot_store(install), install.root, before, after)
        taken = snapshot.created.astimezone().strftime("%H:%M")
        return f"Written to your reader. Snapshot from {taken} kept (Reader → Snapshots…)."

    def check_health(self) -> QMessageBox | None:
        """After a write: did KOReader load every plugin and patch since? Offer the way back."""
        if self.install is None:
            return None
        store = snapshot_store(self.install)
        pending = healthcheck.load_pending(store.base)
        if pending is None:
            return None
        try:
            report = healthcheck.check(self.install.root, pending)
        except OSError:
            return None  # reader gone; the check stays pending
        if not report.started:
            return None
        healthcheck.clear_pending(store.base)
        if not report.failures:
            return None
        box = QMessageBox(self)
        box.setObjectName("health")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(APP_NAME)
        box.setText(
            "Since the last change, KOReader could not load:\n"
            + "\n".join(f"• {f.kind.capitalize()} {f.name}" for f in report.failures)
        )
        box.setInformativeText(
            "The change may have caused this. The snapshot taken before it brings your reader "
            "back to how it was."
        )
        box.setDetailedText("\n\n".join(f"{f.name}: {f.message}" for f in report.failures))
        restore = box.addButton("Restore snapshot…", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Keep as is", QMessageBox.ButtonRole.RejectRole)
        snapshot_id = pending.snapshot_id
        box.finished.connect(
            lambda _result: box.clickedButton() is restore and self.offer_restore(snapshot_id)
        )
        box.open()
        return box

    def offer_restore(self, snapshot_id: str) -> SnapshotsDialog | None:
        """The Snapshots window with ``snapshot_id`` selected, then its restore preview."""
        dialog = self.show_snapshots()
        if dialog is not None and dialog.select(snapshot_id):
            dialog.restore_selected()
        return dialog

    def choose_reader(self) -> None:
        """Let the user pick a reader mount or KOReader folder."""
        folder = QFileDialog.getExistingDirectory(self, "Choose your reader or its KOReader folder")
        if not folder:
            return
        try:
            install = find_install(Path(folder))
            self.set_context(reader_context(install), install)
        except (InstallError, OSError) as error:
            QMessageBox.warning(self, APP_NAME, str(error))

    def show_snapshots(self) -> SnapshotsDialog | None:
        """Open the Snapshots window for the connected reader."""
        if self.install is None:
            return None
        dialog = SnapshotsDialog(snapshot_store(self.install), self.install.root, self)
        dialog.open()
        return dialog


def main(argv: Sequence[str] | None = None) -> int:
    """Start the GUI with the given reader, the first one found, or the demo reader."""
    parser = argparse.ArgumentParser(prog=APP_NAME)
    parser.add_argument("path", nargs="?", type=Path, help="reader mount point or KOReader folder")
    parser.add_argument("--demo", action="store_true", help="ignore connected readers")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="open the window, run the event loop once and quit (packaging checks in CI)",
    )
    args, qt_args = parser.parse_known_args(sys.argv[1:] if argv is None else argv)

    existing = QApplication.instance()  # set when embedded, e.g. by pytest-qt
    app = existing if isinstance(existing, QApplication) else QApplication([sys.argv[0], *qt_args])
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setDesktopFileName(APP_ID)
    app.setWindowIcon(QIcon.fromTheme(APP_ID))

    context, install = demo_context(), None
    if not args.demo:
        try:
            found = find_install(args.path) if args.path else detect_reader()
            if found is not None:
                context, install = reader_context(found), found
        except (InstallError, OSError) as error:
            print(f"{APP_NAME}: {error}; showing the demo reader", file=sys.stderr)
    window = MainWindow(context, install)
    window.show()
    if args.smoke_test:
        QTimer.singleShot(0, app.quit)
    return app.exec()
