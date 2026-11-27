"""Application entry point: ``kodesk [PATH]`` where PATH is a reader mount or KOReader folder."""

from __future__ import annotations

import argparse
import contextlib
import sys
from collections.abc import Iterable, Sequence
from datetime import datetime
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
from kodesk.core import catalogue, healthcheck, icons, net
from kodesk.core.devices import DEVICES
from kodesk.core.hotcorners import GESTURES_FILE
from kodesk.core.icons import icon_file
from kodesk.core.install import (
    InstallError,
    KOReaderInstall,
    find_install,
    home_folder,
    read_settings_file,
    reader_path,
    volume_root,
)
from kodesk.core.librarymode import LibraryLook, read_look, write_mode
from kodesk.core.luadump import SettingsDocument
from kodesk.core.mirror import WifiReader
from kodesk.core.settings_write import Changes, write_documents
from kodesk.core.sleepscreen import PHOTO_DIR, PLATFORMS, SLEEP_LOGO, place_picture
from kodesk.core.snapshot import SnapshotStore, device_id, guarded_write
from kodesk.core.volume import volume_serial
from kodesk.core.wifi import KeyStore
from kodesk.ui.changes import ChangeRunner
from kodesk.ui.garage import ReaderContext, TuningGarage
from kodesk.ui.installed import InstalledPage
from kodesk.ui.koupdate import KOReaderUpdater
from kodesk.ui.readers import Lister, ReaderWatcher, mounted_readers
from kodesk.ui.screen import load_device_fonts
from kodesk.ui.snapshots import SnapshotsDialog, with_snapshot_progress
from kodesk.ui.store import StorePage
from kodesk.ui.wifi import UsbReader, WifiDialog

DEMO_DEVICE = "pocketbook-verse-pro-color"
BOOK_SUFFIXES = {".epub", ".pdf", ".fb2", ".mobi", ".azw3", ".djvu", ".cbz", ".txt"}
MAX_LIBRARY_ENTRIES = 200


def demo_context() -> ReaderContext:
    return ReaderContext(device=DEVICES[DEMO_DEVICE], settings=SettingsDocument.parse("return {}"))


def library_names(entries: Iterable[tuple[str, bool]]) -> tuple[str, ...]:
    """Folder and book names as the library shows them, from (name, is a folder) pairs."""
    shown = sorted(
        (
            (name, folder)
            for name, folder in entries
            if (folder and not name.endswith(".sdr"))
            or (not folder and Path(name).suffix.lower() in BOOK_SUFFIXES)
        ),
        key=lambda entry: (not entry[1], entry[0].lower()),
    )
    return tuple(
        f"{name}/" if folder else Path(name).stem for name, folder in shown[:MAX_LIBRARY_ENTRIES]
    )


def _library_entries(home: Path) -> tuple[str, ...]:
    """Folder and book names in the reader's home folder (names only, read-only)."""
    if not home.is_dir():
        return ()
    return library_names((p.name, p.is_dir()) for p in home.iterdir())


def menu_icons(koreader_root: Path) -> dict[str, Path]:
    """The menu tab icons as KOReader finds them on the reader (read-only)."""
    found = {name: icon_file(koreader_root, name) for name in icons.MENU_TABS}
    return {name: path for name, path in found.items() if path is not None}


def _gestures(koreader_root: Path) -> SettingsDocument | None:
    """settings/gestures.lua (read-only); None until KOReader has saved it once."""
    path = koreader_root / GESTURES_FILE
    return read_settings_file(path) if path.is_file() else None


def reader_context(
    install: KOReaderInstall,
    library: tuple[str, ...] | None = None,
    source: str | None = None,
) -> ReaderContext:
    """Read everything the garage shows from a reader (over Wi-Fi: its mirror, B-27).

    ``library`` replaces the book names read from the volume (a mirror has no books);
    ``source`` replaces the line saying where the reader is.
    """
    if install.device is None:
        raise InstallError(
            f"Reader model {install.model or 'unknown'} has no device definition yet."
        )
    volume = volume_root(install)
    settings = install.read_settings()
    if library is None:
        library = _library_entries(volume / home_folder(install.platform, settings))
    sleep = volume / SLEEP_LOGO
    return ReaderContext(
        device=install.device,
        settings=settings,
        koreader_version=install.version,
        font_families=tuple(load_device_fonts(install.fonts_dir)),
        library_entries=library,
        sleep_image=sleep if sleep.is_file() else None,
        plugins=install.plugins(),
        icons=menu_icons(install.root),
        gestures=_gestures(install.root),
        library=read_look(install.root, settings, install.plugins()),
        source_label=source or f"KOReader {install.version or '?'} on {volume}",
    )


def _app_data() -> Path:
    return Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation))


def key_store() -> KeyStore:
    """Wi-Fi pairings and kodesk's keys (B-11), in the app data folder."""
    return KeyStore(_app_data() / "wifi")


def snapshot_store(install: KOReaderInstall) -> SnapshotStore:
    """This reader's snapshots in the platform's app data folder (never on the reader)."""
    data = _app_data()
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
        self.wifi: WifiReader | None = None  # the reader shown is reached over Wi-Fi (B-27)
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
            self.store_for,
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
            self.store_for,
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
        self._file_menu(open_action)
        self.snapshots_action = QAction("&Snapshots…", self)
        self.snapshots_action.triggered.connect(self.show_snapshots)
        self.koreader_action = QAction("Update &KOReader…", self)
        self.koreader_action.triggered.connect(self.koupdate.start)
        self.wifi_action = QAction("&Wi-Fi…", self)
        self.wifi_action.triggered.connect(self.show_wifi)
        reader_menu = self.menuBar().addMenu("&Reader")
        reader_menu.addAction(self.snapshots_action)
        reader_menu.addAction(self.koreader_action)
        reader_menu.addAction(self.wifi_action)
        self.reader_menu = reader_menu
        self.watcher: ReaderWatcher | None = None
        self.snapshots_button = QPushButton("Snapshots…", self)
        self.snapshots_button.setObjectName("snapshots")
        self.snapshots_button.setToolTip("Snapshots of your reader, restore in one click")
        self.snapshots_button.clicked.connect(self.show_snapshots)
        self.statusBar().addPermanentWidget(self.snapshots_button)
        self.set_context(context, install)

    def _file_menu(self, open_action: QAction) -> None:
        quit_action = QAction("&Quit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu = self.menuBar().addMenu("&File")
        file_menu.addAction(open_action)
        file_menu.addSeparator()
        file_menu.addAction(quit_action)

    def set_context(self, context: ReaderContext, install: KOReaderInstall | None = None) -> None:
        """Show a reader (or the demo); snapshots only exist for a real reader."""
        self.install = install
        wifi = self._over_wifi(install)
        self.snapshots_action.setEnabled(install is not None)
        self.koreader_action.setEnabled(install is not None)
        self.snapshots_button.setVisible(install is not None)
        old: QWidget | None = getattr(self, "garage", None)
        self.garage = TuningGarage(context, self)
        if old is not None:
            self.tabs.removeTab(self.tabs.indexOf(old))
            old.deleteLater()
        self.tabs.insertTab(0, self.garage, "Tuning-&Garage")
        self.garage.storeRequested.connect(self.show_in_store)
        self.tabs.setCurrentIndex(0)
        self.changes.install = install
        self.koupdate.install = install
        self.koupdate.send = self.wifi.send_update if wifi and self.wifi else None  # B-27b
        self.reread()
        self.statusBar().showMessage(context.source_label)
        if install is not None and not wifi:  # over Wi-Fi the Garage shows only (B-26)
            self.garage.set_writer(lambda changes: self._write_settings(install, changes))
            if install.platform in PLATFORMS:
                self.garage.set_sleep_writer(lambda data: self._write_sleep(install, data))
            self.garage.set_library_writer(lambda rows: self._write_library(install, rows))
            QTimer.singleShot(0, self.check_health)

    def show_in_store(self, search: str, select: str) -> None:
        """The Store with ``search`` filled in (from a locked place in the Garage)."""
        self.tabs.setCurrentWidget(self.store)
        self.store.search_for(search, select)

    def show_reader(self, install: KOReaderInstall | None) -> str | None:
        """Show ``install`` (None: the demo reader); an error message if it cannot be shown."""
        self.wifi = None
        if install is None:
            self.set_context(demo_context(), None)
            return None
        try:
            self.set_context(reader_context(install), install)
        except (InstallError, OSError) as error:
            return str(error)
        return None

    def show_wifi_reader(self, wifi: WifiReader) -> str | None:
        """Show a reader reached over Wi-Fi: its mirror, freshly brought in step (B-27)."""
        try:
            install = wifi.install()
            source = (
                f"KOReader {install.version or '?'} over Wi-Fi ({wifi.host}) · "
                "the Garage only shows your settings here; change them over USB"
            )
            context = reader_context(install, library_names(wifi.books), source)
        except (InstallError, OSError) as error:
            return str(error)
        self.wifi = wifi
        self.set_context(context, install)
        return None

    def _over_wifi(self, install: KOReaderInstall | None) -> bool:
        return (
            install is not None
            and self.wifi is not None
            and install.root == self.wifi.mirror.koreader_dir
        )

    def store_for(self, install: KOReaderInstall) -> SnapshotStore:
        """The reader's snapshots; over Wi-Fi each finished change is sent to the reader."""
        if self.wifi is None or not self._over_wifi(install):
            return snapshot_store(install)
        store = SnapshotStore(_app_data() / "snapshots" / self.wifi.pairing.reader_id)
        store.publish = self.wifi.publish
        return store

    def watch_readers(self, lister: Lister = mounted_readers) -> ReaderWatcher:
        """Follow readers being plugged in and out (Reader → Connected readers)."""
        self.reader_menu.addSeparator()
        menu = self.reader_menu.addMenu("&Connected readers")
        self.watcher = ReaderWatcher(self, menu, lister)
        self.watcher.start()
        return self.watcher

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

    def _write_settings(self, install: KOReaderInstall, changes: Changes) -> str:
        store = self.store_for(install)
        snapshot = with_snapshot_progress(
            self, lambda progress: write_documents(store, install.root, changes, progress)
        )
        taken = snapshot.created.astimezone().strftime("%H:%M")
        return f"Written to your reader. Snapshot from {taken} kept (Reader → Snapshots…)."

    def _write_library(
        self, install: KOReaderInstall, rows: dict[str, str | None]
    ) -> tuple[str, LibraryLook]:
        """Write the library look into the plugin's cache, inside a snapshot."""
        store = self.store_for(install)
        look = self.garage.context.library
        snapshot = with_snapshot_progress(
            self,
            lambda progress: write_mode(
                store, install.root, look.drawer, rows, look.read, progress
            ),
        )
        taken = snapshot.created.astimezone().strftime("%H:%M")
        now = read_look(install.root, install.read_settings(), install.plugins())
        self.garage.context.library = now
        return f"Library look written. Snapshot from {taken} kept (Reader → Snapshots…).", now

    def _write_sleep(self, install: KOReaderInstall, data: bytes) -> str:
        """Add a sleep-screen picture to the reader's Photo folder, inside a snapshot."""
        store = self.store_for(install)
        placed: list[Path] = []

        def write() -> None:
            placed.append(place_picture(volume_root(install), data, datetime.now()))

        with_snapshot_progress(
            self, lambda progress: guarded_write(store, install.root, write, (), (), progress)
        )
        return f"{PHOTO_DIR}/{placed[0].name}"

    def check_health(self) -> QMessageBox | None:
        """After a write: did it finish, and did KOReader load every plugin and patch since?
        Offer the way back."""
        if self.install is None:
            return None
        # First: a KOReader update that did not finish (the log check would blame its plugins).
        return self.koupdate.interrupted(self.offer_restore) or self._check_log(self.install)

    def _check_log(self, install: KOReaderInstall) -> QMessageBox | None:
        store = self.store_for(install)
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
            error = self.show_reader(find_install(Path(folder)))
        except (InstallError, OSError) as failure:
            error = str(failure)
        if error:
            QMessageBox.warning(self, APP_NAME, error)

    def show_wifi(self) -> WifiDialog:
        """Pair the reader over USB and test Wi-Fi (B-11)."""
        usb = None
        if self.install is not None and not self._over_wifi(self.install):
            store = snapshot_store(self.install)
            device = self.install.device
            name = device.name if device else f"Reader {self.install.model or ''}".strip()
            try:
                where = reader_path(self.install)
            except InstallError:
                where = ""  # pairing works; Wi-Fi then says the platform is not supported
            usb = UsbReader(
                store.base.name,
                name,
                self.install.read_settings(),
                self.install.root,
                store,
                where,
            )
        dialog = WifiDialog(key_store(), usb, self, _app_data() / "mirror")
        dialog.connected.connect(self._wifi_connected)
        dialog.open()
        return dialog

    def _wifi_connected(self, wifi: WifiReader) -> None:
        error = self.show_wifi_reader(wifi)
        if error:
            QMessageBox.warning(self, APP_NAME, error)

    def show_snapshots(self) -> SnapshotsDialog | None:
        """Open the Snapshots window for the connected reader."""
        if self.install is None:
            return None
        dialog = SnapshotsDialog(self.store_for(self.install), self.install.root, self)
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
            found = find_install(args.path) if args.path else next(iter(mounted_readers()), None)
            if found is not None:
                context, install = reader_context(found), found
        except (InstallError, OSError) as error:
            print(f"{APP_NAME}: {error}; showing the demo reader", file=sys.stderr)
    window = MainWindow(context, install, offline=args.offline)
    window.show()
    if not (args.path or args.demo or args.smoke_test):
        window.watch_readers()  # only when kodesk looks for readers itself
    if args.smoke_test:
        QTimer.singleShot(0, app.quit)
    return app.exec()
