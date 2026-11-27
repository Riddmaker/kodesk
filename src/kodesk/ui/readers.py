"""Readers plugged in or out while kodesk runs (B-03).

Qt has no portable "volume mounted" signal, so the mounted volumes are polled every two seconds
(``QStorageInfo``: a list of mounts, cheap; only a new volume is searched for KOReader, by the
read-only :func:`kodesk.core.install.find_install`). Rules, in this order:

* A dialog or a write is open: nothing happens until it is closed.
* The shown reader is gone: back to the demo reader, unless the Tuning-Garage holds changes not
  written yet; those are kept, and the reader is used again once it is back at the same place.
* A reader appears and none is shown (or the shown one is gone): it is opened, unless the garage
  holds unwritten changes. Otherwise the status bar names it; Reader → Connected readers opens it.
* A reader kodesk knows appears without KOReader (B-28): kodesk offers to install it, once per
  plugging in; Reader → Connected readers offers it again.

Started only when kodesk looks for readers itself (no PATH argument, no ``--demo``), so a
reader given on the command line or a test tree is never swapped for a real one.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QStorageInfo, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QMenu

from kodesk.core.fresh_install import BareReader, find_bare
from kodesk.core.install import InstallError, KOReaderInstall, find_install, volume_root
from kodesk.ui.koinstall import KOReaderInstaller

if TYPE_CHECKING:
    from kodesk.ui.app import MainWindow

POLL_MS = 2000

Lister = Callable[[], list[KOReaderInstall]]
BareLister = Callable[[], list[BareReader]]


def _ready_volumes() -> list[QStorageInfo]:
    return [v for v in QStorageInfo.mountedVolumes() if v.isValid() and v.isReady()]


def mounted_readers() -> list[KOReaderInstall]:
    """Every mounted volume that holds a KOReader installation, in mount order."""
    found = []
    for volume in _ready_volumes():
        try:
            found.append(find_install(Path(volume.rootPath())))
        except (InstallError, OSError):
            continue
    return found


def bare_readers() -> list[BareReader]:
    """Every mounted volume of a reader kodesk knows (by its label) without KOReader."""
    found = []
    for volume in _ready_volumes():
        root = Path(volume.rootPath())
        reader = find_bare(root, volume.name() or root.name)
        if reader is not None:
            found.append(reader)
    return found


def reader_name(install: KOReaderInstall) -> str:
    """A reader in messages and the menu, e.g. "PocketBook Verse Pro Color (PB634K3)"."""
    model = install.device.name if install.device else (install.model or "Reader")
    return f"{model} ({volume_root(install).name})"


def connected(install: KOReaderInstall) -> bool:
    """Whether the reader's KOReader folder is still there (False once it was unplugged)."""
    try:
        return (install.root / "reader.lua").is_file()
    except OSError:
        return False


class ReaderWatcher(QObject):
    """Follows readers being plugged in and out and keeps the window and its menu in step."""

    def __init__(
        self,
        window: MainWindow,
        menu: QMenu,
        lister: Lister = mounted_readers,
        bare: BareLister = bare_readers,
    ) -> None:
        super().__init__(window)
        self.window = window
        self.menu = menu
        self.lister = lister
        self.bare_lister = bare
        self.readers: list[KOReaderInstall] = []
        self.bare: list[BareReader] = []
        self.known: set[Path] = set()
        self.known_bare: set[Path] = set()  # offered while plugged in: not offered again
        self.installer = KOReaderInstaller(window, window.koupdate.get, window.koupdate.save)
        self.gone = False
        self.timer = QTimer(self)
        self.timer.setInterval(POLL_MS)
        self.timer.timeout.connect(self.poll)

    def start(self) -> None:
        """Remember what is mounted now (already shown or ignored on purpose), then poll."""
        self.readers = self.lister()
        self.known = {r.root for r in self.readers}
        self._fill_menu()
        self.timer.start()

    def poll(self) -> None:
        """One look at the mounted volumes."""
        if QApplication.activeModalWidget() is not None:
            return  # a dialog or a write is running: never change the reader under it
        window = self.window
        pending = bool(window.garage.changed())
        current = window.install
        if current is not None and not connected(current):
            if not self.gone:
                self.gone = True
                if pending:
                    self._say(
                        "Reader disconnected. Your changes are kept: plug it in again to write "
                        "them."
                    )
                else:
                    window.show_reader(None)
                    self._say("Reader disconnected.")
        elif current is not None and self.gone:
            self.gone = False
            self._say(f"{reader_name(current)} is connected again.")
        self.readers = [r for r in self.lister() if not self._fresh(r)]
        roots = {r.root for r in self.readers}
        shown = window.install.root if window.install else None
        new = [r for r in self.readers if r.root not in self.known and r.root != shown]
        self.known = roots
        self.bare = self.bare_lister()
        new_bare = [r for r in self.bare if r.volume not in self.known_bare]
        self.known_bare = {r.volume for r in self.bare}
        self._fill_menu()
        if new_bare:
            self.offer_install(new_bare[0])
            return
        if not new:
            return
        reader = new[0]
        if (window.install is None or self.gone) and not pending:
            self.open(reader)
        else:
            self._say(f"{reader_name(reader)} is connected; Reader → Connected readers opens it.")

    def open(self, reader: KOReaderInstall) -> None:
        """Show ``reader`` (from the menu, or when it was plugged in); the status bar names it."""
        error = self.window.show_reader(reader)
        self.gone = False
        self._fill_menu()
        if error:
            self._say(f"{reader_name(reader)}: {error}")

    def _fresh(self, reader: KOReaderInstall) -> bool:
        """Just installed by kodesk and not started yet (no model): nothing to show so far."""
        return reader.model is None and volume_root(reader).resolve() in self.installer.fresh

    def offer_install(self, reader: BareReader) -> None:
        """Offer KOReader for a reader without it (from plugging in, or from the menu)."""
        installer = self.installer
        if not installer.online:
            self._say(
                f"{reader.name} has no KOReader yet; kodesk runs offline (--offline) and cannot "
                "install it."
            )
            return
        self._say(f"{reader.name} is connected, without KOReader.")
        installer.offer(reader)

    def _fill_menu(self) -> None:
        self.menu.clear()
        shown = self.window.install.root if self.window.install else None
        for number, reader in enumerate(self.readers, 1):
            action = QAction(f"&{number} {reader_name(reader)}", self.menu)
            action.setCheckable(True)
            action.setChecked(reader.root == shown)
            action.triggered.connect(lambda _checked=False, r=reader: self.open(r))
            self.menu.addAction(action)
        for bare in self.bare:
            action = QAction(f"&Install KOReader on {bare.name}…", self.menu)
            action.triggered.connect(lambda _checked=False, b=bare: self.offer_install(b))
            self.menu.addAction(action)
        if not self.readers and not self.bare:
            empty = self.menu.addAction("No reader connected")
            empty.setEnabled(False)

    def _say(self, text: str) -> None:
        self.window.statusBar().showMessage(text)  # stays until the next message
