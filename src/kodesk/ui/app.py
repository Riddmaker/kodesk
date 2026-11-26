"""Application entry point: ``kodesk [PATH]`` where PATH is a reader mount or KOReader folder."""

from __future__ import annotations

import argparse
import contextlib
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
    QTabWidget,
    QWidget,
)

from kodesk import APP_ID, APP_NAME, __version__
from kodesk.core import catalogue, healthcheck, net
from kodesk.core.devices import DEVICES
from kodesk.core.install import InstallError, KOReaderInstall, find_install, volume_root
from kodesk.core.luadump import SettingsDocument
from kodesk.core.settings_write import write_settings
from kodesk.core.snapshot import SnapshotStore, device_id
from kodesk.core.volume import volume_serial
from kodesk.ui.changes import ChangeRunner
from kodesk.ui.garage import ReaderContext, TuningGarage
from kodesk.ui.installed import InstalledPage
from kodesk.ui.koupdate import KOReaderUpdater
from kodesk.ui.screen import load_device_fonts
from kodesk.ui.snapshots import SnapshotsDialog
from kodesk.ui.store import StorePage

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


def reader_context(install: KOReaderInstall) -> ReaderContext:
    """Read everything the garage shows from a connected reader."""
    if install.device is None:
        raise InstallError(
            f"Reader model {install.model or 'unknown'} has no device definition yet."
        )
    volume = volume_root(install)
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
    volume = volume_root(install)
    serial = volume_serial(
        bytes(QStorageInfo(str(volume)).device().data()).decode(errors="replace")
    )
    base = data / "snapshots"
    unique = base / device_id(install.model, volume.name, serial)
    older = base / device_id(install.model, volume.name)  # before 2026-11-26: no serial
    return SnapshotStore(older if older.is_dir() and not unique.is_dir() else unique)


def store_page(offline: bool, parent: QWidget) -> StorePage:
    """The Store tab; ``offline`` never contacts the network (cached catalogue only)."""
    cache = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation)
    cache_dir = Path(cache) / "catalogue"
    get = None if offline else catalogue.https_get

    def fetch(entry: catalogue.Entry) -> str:
        if offline:
            raise catalogue.CatalogueError("kodesk runs offline")
        return catalogue.fetch_text(entry)

    return StorePage(lambda: catalogue.load(cache_dir, get), fetch, parent)


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
    """Main window: menu bar, the Tuning-Garage, Installed and Store tabs, the reader below."""

    def __init__(
        self,
        context: ReaderContext,
        install: KOReaderInstall | None = None,
        offline: bool = False,
    ) -> None:
        super().__init__()
        self.install: KOReaderInstall | None = None
        self.tabs = QTabWidget(self)
        self.tabs.setDocumentMode(True)
        self.setCentralWidget(self.tabs)
        self.installed = InstalledPage(self)
        self.tabs.addTab(self.installed, "&Installed")
        self.store = store_page(offline, self)
        self.tabs.addTab(self.store, "&Store")
        self.changes = ChangeRunner(
            self,
            None if offline else catalogue.https_get,
            self._catalogue,
            snapshot_store,
            self.reread,
        )
        self.store.install_requested.connect(self.changes.install_entry)
        self.store.loaded.connect(self._show_sources)
        self.installed.update_requested.connect(self.changes.update)
        self.installed.remove_requested.connect(self.changes.remove)
        self.installed.source_requested.connect(self.changes.set_source)
        self.koupdate = KOReaderUpdater(
            self,
            None if offline else net.https_get,
            None if offline else net.https_save,
            snapshot_store,
            self.reread_install,
            self.changes.remove,
        )
        self.installed.koreader_update_requested.connect(self.koupdate.start)
        self.installed.check_requested.connect(self.check_updates)
        self.tabs.currentChanged.connect(self._tab_changed)
        self.setWindowTitle(APP_NAME)
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
        self.koreader_action = QAction("Update &KOReader…", self)
        self.koreader_action.triggered.connect(self.koupdate.start)
        reader_menu = self.menuBar().addMenu("&Reader")
        reader_menu.addAction(self.snapshots_action)
        reader_menu.addAction(self.koreader_action)
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
        self.koreader_action.setEnabled(install is not None)
        self.snapshots_button.setVisible(install is not None)
        old: QWidget | None = getattr(self, "garage", None)
        self.garage = TuningGarage(context, self)
        if old is not None:
            self.tabs.removeTab(self.tabs.indexOf(old))
            old.deleteLater()
        self.tabs.insertTab(0, self.garage, "Tuning-&Garage")
        self.tabs.setCurrentIndex(0)
        self.changes.install = install
        self.koupdate.install = install
        self.reread()
        self.statusBar().showMessage(context.source_label)
        if install is not None:
            self.garage.set_writer(
                lambda before, after: self._write_settings(install, before, after)
            )
            QTimer.singleShot(0, self.check_health)

    def reread(self) -> None:
        """Read what is installed again (after a change) and where it came from."""
        install = self.install
        self.installed.set_install(install)
        self.store.set_installed(
            frozenset(install.plugins()) if install else frozenset(),
            frozenset(p.name for p in self.installed.patches),
        )
        self._show_sources()

    def reread_install(self) -> None:
        """After a KOReader update: its version (and so on) as the reader has it now."""
        if self.install is not None:
            with contextlib.suppress(InstallError, OSError):  # reader gone: show what was there
                self.install = find_install(self.install.root)
            self.changes.install = self.koupdate.install = self.install
        self.reread()

    def check_updates(self) -> None:
        """Ask GitHub about the plugins and patches, and about KOReader itself."""
        self.changes.check(self.installed.checkable(), self.installed.set_updates)
        self.koupdate.check(self.installed.set_koreader_status)

    def _show_sources(self) -> None:
        records = self.changes.records() if self.install is not None else None
        entries = self.store.catalogue.entries if self.store.catalogue else ()
        self.installed.set_sources(records.all() if records else (), entries)

    def _catalogue(self) -> catalogue.Catalogue | None:
        """The Store's catalogue; starts loading it if it is not there yet."""
        if self.store.catalogue is None:
            self.store.ensure_loaded()
        return self.store.catalogue

    def _tab_changed(self, index: int) -> None:
        if self.tabs.widget(index) is self.store:
            self.store.ensure_loaded()  # no network before the user opens the Store
            self.store.search.setFocus()

    @staticmethod
    def _write_settings(
        install: KOReaderInstall, before: SettingsDocument, after: SettingsDocument
    ) -> str:
        snapshot = write_settings(snapshot_store(install), install.root, before, after)
        taken = snapshot.created.astimezone().strftime("%H:%M")
        return f"Written to your reader. Snapshot from {taken} kept (Reader → Snapshots…)."

    def check_health(self) -> QMessageBox | None:
        """After a write: did it finish, and did KOReader load every plugin and patch since?
        Offer the way back."""
        if self.install is None:
            return None
        # First: a KOReader update that did not finish (the log check would blame its plugins).
        return self.koupdate.interrupted(self.offer_restore) or self._check_log(self.install)

    def _check_log(self, install: KOReaderInstall) -> QMessageBox | None:
        store = snapshot_store(install)
        pending = healthcheck.load_pending(store.base)
        if pending is None:
            return None
        try:
            report = healthcheck.check(install.root, pending)
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
    parser.add_argument(
        "--offline", action="store_true", help="never go online (store: last cached catalogue)"
    )
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
    window = MainWindow(context, install, offline=args.offline)
    window.show()
    if args.smoke_test:
        QTimer.singleShot(0, app.quit)
    return app.exec()
