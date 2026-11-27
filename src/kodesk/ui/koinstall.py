"""Install KOReader on a known reader that has none yet (B-28): offer, download, preview, write.

:class:`kodesk.ui.readers.ReaderWatcher` calls :meth:`KOReaderInstaller.offer` when such a reader
is plugged in (and Reader → Connected readers lists it). The download and unpacking run in the
background behind a cancellable progress dialog, as for an update (:mod:`kodesk.ui.koupdate`);
the preview names the release and what is added; the write shows its progress and can be
cancelled (what was written is removed again); the result offers to remove it all again
(:mod:`kodesk.core.fresh_install`).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QMessageBox, QWidget

from kodesk import APP_NAME
from kodesk.core import fresh_install
from kodesk.core.fresh_install import BareReader, FreshPlan, Installed
from kodesk.core.net import CancelledError, Get, Save
from kodesk.core.snapshot import SnapshotCancelledError
from kodesk.ui.koupdate import FAILURES, SOURCE, ReleaseDownload
from kodesk.ui.snapshots import human_size, with_snapshot_progress

INSTALLING = "Installing KOReader on your reader…"


def offer_text(reader: BareReader) -> tuple[str, str]:
    """(question, explanation) when a reader without KOReader is plugged in."""
    return (
        f"KOReader is not installed on this {reader.name} yet.",
        f"kodesk can install the latest stable release from {SOURCE}. It adds KOReader beside "
        "your books and apps and changes nothing else on the reader.",
    )


def preview_text(plan: FreshPlan) -> tuple[str, str, str]:
    """(question, explanation, details) for the install preview."""
    question = f"Install KOReader {plan.release.tag} on {plan.reader.name}?"
    lines = [
        f"From {SOURCE}, the official release {plan.release.name} ({plan.release.megabytes} MB), "
        "checked against the SHA-256 GitHub publishes for it.",
        f"Adds {len(plan.write)} files ({human_size(plan.bytes)}): the folder "
        "applications/koreader, its starter applications/koreader.app and "
        "system/bin/koreader.app, exactly as KOReader's own install instructions say. Nothing "
        "on the reader is replaced or removed.",
    ]
    if plan.present:
        lines.append(f"Already there and left as they are: {', '.join(plan.present)}.")
    details = "\n".join(f"+ {path}" for path in plan.write)
    return question, "\n\n".join(lines), details


class KOReaderInstaller(QObject):
    """Runs the first install of KOReader for the main window."""

    def __init__(self, parent: QWidget, get: Get | None, save: Save | None) -> None:
        super().__init__(parent)
        self.window = parent
        self.get = get
        self.save = save
        self.fresh: set[Path] = set()  # volumes KOReader was put on (not started there yet)
        self.download = ReleaseDownload(parent, "koreader-install-progress", self._prepared)

    @property
    def busy(self) -> bool:
        return self.download.busy

    @property
    def online(self) -> bool:
        return self.get is not None and self.save is not None

    def offer(self, reader: BareReader) -> bool:
        """Ask whether to install; start it if yes. False if the user said not now."""
        question, text = offer_text(reader)
        box = QMessageBox(self.window)
        box.setObjectName("koreader-install-offer")
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(APP_NAME)
        box.setText(question)
        box.setInformativeText(text)
        install = box.addButton("&Install KOReader", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("&Not now", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(install)
        box.exec()
        if box.clickedButton() is not install:
            return False
        self.start(reader)
        return True

    def start(self, reader: BareReader) -> None:
        """Download and unpack the latest release on the PC in the background, then preview."""
        if self.get is None or self.save is None:
            self._warn("kodesk runs offline (--offline); installing KOReader needs the network.")
            return
        if self.busy:
            return
        get, save = self.get, self.save
        self.download.start(
            lambda staging, cancelled, say: fresh_install.prepare(
                reader, get, save, staging, cancelled, say
            )
        )

    def _prepared(self, result: object) -> None:
        if isinstance(result, FreshPlan):
            if self.confirm(result):
                self.write(result)
        elif not isinstance(result, CancelledError):
            self._warn(f"KOReader was not installed: {result}")

    def confirm(self, plan: FreshPlan) -> bool:
        """The preview; True if the user chose to install."""
        question, text, details = preview_text(plan)
        box = QMessageBox(self.window)
        box.setObjectName("koreader-install-preview")
        box.setWindowTitle(APP_NAME)
        box.setText(question)
        box.setInformativeText(text)
        box.setDetailedText(details)
        install = box.addButton(
            f"&Install KOReader {plan.release.tag}", QMessageBox.ButtonRole.AcceptRole
        )
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(install)
        box.exec()
        return box.clickedButton() is install

    def write(self, plan: FreshPlan) -> Installed | None:
        """Write the files behind a progress dialog; then the result with "remove it again"."""
        try:
            done = with_snapshot_progress(
                self.window,
                lambda progress: fresh_install.install(plan, progress),
                then=None,
                label=INSTALLING,
                verb="written",
            )
        except SnapshotCancelledError:
            self._info("KOReader was not installed; what was written is removed again.")
            return None
        except (*FAILURES, fresh_install.FreshInstallError) as error:
            self._warn(f"KOReader was not installed; the reader is as before: {error}")
            return None
        self.fresh.add(plan.reader.volume.resolve())
        self.result(plan, done)
        return done

    def result(self, plan: FreshPlan, done: Installed) -> None:
        """Say what to do next; offer to take it away again."""
        box = QMessageBox(self.window)
        box.setObjectName("koreader-install-done")
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle(APP_NAME)
        box.setText(f"KOReader {plan.release.tag} is installed on {plan.reader.name}.")
        box.setInformativeText(
            "Eject the reader safely and start KOReader once from the reader's applications. "
            "Then plug it in again: kodesk shows it, with the Tuning-Garage, Store and "
            "snapshots."
        )
        box.addButton("&OK", QMessageBox.ButtonRole.AcceptRole)
        undo = box.addButton("&Remove it again", QMessageBox.ButtonRole.DestructiveRole)
        box.exec()
        if box.clickedButton() is undo:
            self.undo(plan.reader, done)

    def undo(self, reader: BareReader, done: Installed) -> None:
        try:
            fresh_install.remove(reader.volume, done)
        except OSError as error:
            self._warn(f"KOReader could not be removed completely: {error}")
            return
        self.fresh.discard(reader.volume.resolve())
        self._info("KOReader is removed again; the reader is as it was.")

    def _warn(self, text: str) -> None:
        QMessageBox.warning(self.window, APP_NAME, text)

    def _info(self, text: str) -> None:
        QMessageBox.information(self.window, APP_NAME, text)
