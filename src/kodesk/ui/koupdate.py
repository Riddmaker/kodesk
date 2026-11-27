"""Update KOReader itself from the GUI (B-06): check, download with progress, preview, write.

The download and unpacking run in the background behind a cancellable progress dialog; the
preview lists what changes and lets the user keep their own version of KOReader files they
edited; the write runs with the wait cursor, inside a snapshot (:mod:`kodesk.core.update`).
Over Wi-Fi (``send`` set, B-27b) the snapshot is taken of the mirror and the update is sent as
one archive that KOReader's launcher installs when KOReader restarts.
"""

from __future__ import annotations

import shutil
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from PySide6.QtCore import QObject, Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QProgressDialog,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from kodesk import APP_NAME
from kodesk.core import update
from kodesk.core.archive import ArchiveError
from kodesk.core.catalogue import Kind
from kodesk.core.install import KOReaderInstall
from kodesk.core.mirror import MirrorError
from kodesk.core.net import CancelledError, Get, NetError, Save
from kodesk.core.snapshot import Progress, SnapshotError, SnapshotStore
from kodesk.ui.snapshots import busy, path_lines, with_snapshot_progress
from kodesk.ui.store import Results, run_in_background

FAILURES = (update.UpdateError, NetError, ArchiveError, SnapshotError, OSError, MirrorError)
SENDING = "Sending the update to your reader…"
CHECKING = "Checking it on your reader…"
SOURCE = "github.com/koreader/koreader"


def preview_text(plan: update.UpdatePlan) -> tuple[str, str, str]:
    """(question, explanation, details) for the update preview."""
    tag = plan.release.tag
    question = f"Update KOReader from {plan.installed or 'an unknown version'} to {tag}?"
    lines = [
        f"From {SOURCE}, the official release {plan.release.name} ({plan.release.megabytes} MB), "
        "checked against the SHA-256 GitHub publishes for it."
    ]
    counts = [
        f"{len(paths)} {label}"
        for paths, label in (
            (plan.add, "added"),
            (plan.replace, "replaced"),
            (plan.remove, "removed (no longer part of KOReader)"),
        )
        if paths
    ]
    lines.append("Files: " + ", ".join(counts) + f"; {plan.unchanged} unchanged.")
    lines.append(
        "Your settings, reading history, style tweaks, gestures, the plugins and patches you "
        "added and your books stay exactly as they are."
    )
    if not plan.edits_known:
        lines.append(
            f"kodesk cannot tell whether you changed KOReader's own files: GitHub has no release "
            f"of {plan.installed or 'the installed version'} to compare with. The snapshot keeps "
            "the old files."
        )
    lines.append(
        "Close KOReader on the reader first: it runs from these files. kodesk takes a snapshot "
        "first (with the launcher beside the KOReader folder); "
        "Reader → Snapshots… brings everything back."
    )
    details = path_lines(
        (
            ("Added", plan.add),
            ("Replaced", plan.replace),
            ("Removed", plan.remove),
            ("Removed as outdated", plan.stale),
        )
    )
    return question, "\n\n".join(lines), "\n".join(details)


class UpdateDialog(QDialog):
    """The preview: what changes, the files the user edited (tick to keep), and the details."""

    def __init__(self, plan: update.UpdatePlan, parent: QWidget | None) -> None:
        super().__init__(parent)
        self.setObjectName("koreader-update-preview")
        self.setWindowTitle(APP_NAME)
        question, text, details = preview_text(plan)
        layout = QVBoxLayout(self)
        title = QLabel(question, self)
        font = title.font()
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)
        info = QLabel(text, self)
        info.setWordWrap(True)
        info.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(info)
        self.edited = QListWidget(self)
        self.edited.setObjectName("koreader-update-edited")
        if plan.edited:
            label = QLabel(
                "You changed these files of KOReader. &Ticked ones keep your version; the "
                "others get the new one:",
                self,
            )
            label.setWordWrap(True)
            label.setBuddy(self.edited)
            layout.addSpacing(label.fontMetrics().height() // 2)
            layout.addWidget(label)
            for path in plan.edited:
                item = QListWidgetItem(path, self.edited)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Unchecked)
            rows = min(len(plan.edited), 8)  # as tall as the list, up to 8 rows, then it scrolls
            frame = 2 * self.edited.frameWidth()
            self.edited.setFixedHeight(rows * self.edited.sizeHintForRow(0) + frame)
            layout.addWidget(self.edited)
        else:
            self.edited.hide()
        self.details = QPlainTextEdit(details, self)
        self.details.setObjectName("koreader-update-details")
        self.details.setReadOnly(True)
        self.details.hide()
        toggle = QPushButton("Show &details", self)
        toggle.setCheckable(True)
        toggle.toggled.connect(self.details.setVisible)
        layout.addWidget(toggle, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.details, 1)
        buttons = QDialogButtonBox(self)
        self.accept_button = buttons.addButton(
            f"&Update KOReader to {plan.release.tag}", QDialogButtonBox.ButtonRole.AcceptRole
        )
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.accept_button.setDefault(True)
        self.resize(720, self.sizeHint().height())

    def keep(self) -> frozenset[str]:
        """The edited files the user ticked to keep."""
        items = (self.edited.item(i) for i in range(self.edited.count()))
        return frozenset(
            item.text() for item in items if item and item.checkState() == Qt.CheckState.Checked
        )


class KOReaderUpdater(QObject):
    """Runs the KOReader update for the main window."""

    def __init__(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        parent: QWidget,
        get: Get | None,
        save: Save | None,
        store_for: Callable[[KOReaderInstall], SnapshotStore],
        changed: Callable[[], None],
        remove: Callable[[tuple[Kind, str]], None],
    ) -> None:
        super().__init__(parent)
        self.window = parent
        self.get = get
        self.save = save
        self.store_for = store_for
        self.changed = changed
        self.remove = remove
        self.install: KOReaderInstall | None = None
        self.send: Callable[[Path, Progress], None] | None = None  # set over Wi-Fi
        self.busy = False
        self.progress: QProgressDialog | None = None
        self._cancel = threading.Event()
        self._staging: Path | None = None
        self._checked: Callable[[str], None] | None = None
        self._results = Results(self)
        self._results.done.connect(self._done)

    def why_not(self) -> str | None:
        """Why KOReader cannot be updated right now, or None."""
        if self.install is None:
            return "Connect a reader first (File → Open reader…)."
        if self.get is None or self.save is None:
            return "kodesk runs offline (--offline); updating KOReader needs the network."
        if self.busy:
            return "kodesk is still preparing the KOReader update."
        try:
            update.layout_of(self.install)
        except update.UpdateError as error:
            return str(error)
        return None

    def start(self) -> None:
        """Find the latest release, download and unpack it on the PC, then show the preview."""
        reason = self.why_not()
        if reason is not None or self.install is None or self.get is None or self.save is None:
            self._warn(reason or "No reader.")
            return
        install, get, save, results = self.install, self.get, self.save, self._results
        self.busy = True
        self._cancel.clear()
        self._staging = staging = Path(tempfile.mkdtemp(prefix="kodesk-koreader-"))
        self.progress = QProgressDialog("Asking GitHub for the latest KOReader…", "Cancel", 0, 0)
        self.progress.setParent(self.window, self.progress.windowFlags())
        self.progress.setObjectName("koreader-update-progress")
        self.progress.setWindowTitle(APP_NAME)
        self.progress.setWindowModality(Qt.WindowModality.WindowModal)
        self.progress.setMinimumDuration(0)
        self.progress.canceled.connect(self._cancel.set)
        self.progress.show()

        def work() -> object:
            return update.prepare(
                install,
                get,
                save,
                staging,
                self._cancel.is_set,
                lambda text: results.done.emit("progress", text),
            )

        run_in_background(self._results, "prepare", work)

    def _done(self, job: str, result: object) -> None:
        if job == "progress":
            if self.progress is not None and isinstance(result, str):
                self.progress.setLabelText(result)
            return
        if job == "check":
            if self._checked is not None:
                self._checked(result if isinstance(result, str) else f"Not checked: {result}")
            return
        self.busy = False
        if self.progress is not None:
            self.progress.close()
            self.progress.deleteLater()
            self.progress = None
        try:
            if isinstance(result, update.UpToDate):
                self._info(f"KOReader on your reader is {result.text.lower()}.")
            elif isinstance(result, update.UpdatePlan):
                self.offer(result)
            elif not isinstance(result, CancelledError):
                self._warn(f"KOReader was not updated: {result}")
        finally:
            if self._staging is not None:
                shutil.rmtree(self._staging, ignore_errors=True)
                self._staging = None

    def offer(self, plan: update.UpdatePlan) -> bool:
        """Conflicts first, then the preview; write if confirmed. Returns whether it was written."""
        if plan.conflicts:
            self.conflict(plan)
            return False
        if not plan:
            self._info(f"KOReader on your reader already has every file of {plan.release.tag}.")
            return False
        keep = self.confirm(plan)
        if keep is None:
            return False
        return self.write(plan, keep)

    def conflict(self, plan: update.UpdatePlan) -> None:
        """A plugin the user added has the name of one the new KOReader brings: remove it first."""
        name = plan.conflicts[0]
        box = QMessageBox(self.window)
        box.setObjectName("koreader-update-conflict")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(APP_NAME)
        box.setText(
            f"KOReader {plan.release.tag} brings its own plugin {name}; the plugin {name} you "
            "added would clash with it."
        )
        box.setInformativeText(
            "Remove your plugin first (its settings stay on the reader), then update KOReader."
            + (f" Also clashing: {', '.join(plan.conflicts[1:])}." if plan.conflicts[1:] else "")
        )
        remove = box.addButton(f"Remove {name}…", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() is remove:
            self.remove((Kind.PLUGIN, name))

    def confirm(self, plan: update.UpdatePlan) -> frozenset[str] | None:
        """The preview; the files to keep, or None if cancelled."""
        dialog = UpdateDialog(plan, self.window)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.keep()

    def write(self, plan: update.UpdatePlan, keep: frozenset[str]) -> bool:
        """Snapshot, write, verify (restored on any problem)."""
        if self.install is None:
            return False
        if self.send is not None:
            return self._send(plan, keep, self.send)
        store, install = self.store_for(self.install), self.install
        try:
            with_snapshot_progress(
                self.window,
                lambda progress: update.apply_update(store, install, plan, keep, progress),
            )
        except FAILURES as error:
            self._warn(f"KOReader was not updated, your reader is as before: {error}")
            self.changed()
            return False
        self.changed()
        self._info(
            f"KOReader is now {plan.release.tag}. The snapshot taken before is kept (Reader → "
            "Snapshots…). Eject the reader and start KOReader once; on the next connect, "
            "kodesk checks that it loaded everything."
        )
        return True

    def _send(
        self, plan: update.UpdatePlan, keep: frozenset[str], send: Callable[[Path, Progress], None]
    ) -> bool:
        """Over Wi-Fi: snapshot (the mirror), pack the update, send it for KOReader's launcher."""
        if self.install is None:
            return False
        store, install = self.store_for(self.install), self.install
        launcher = update.layout_of(install).launcher
        outside = (PurePosixPath(launcher).name,) if launcher else ()
        staging = Path(tempfile.mkdtemp(prefix="kodesk-ota-"))
        try:
            archive = busy(lambda: update.ota_archive(install, plan, keep, staging))
            with_snapshot_progress(
                self.window, lambda progress: store.take(install.root, outside, progress), None
            )
            with_snapshot_progress(
                self.window, lambda progress: send(archive, progress), CHECKING, SENDING, "sent"
            )
        except FAILURES as error:
            self._warn(f"KOReader was not updated, your reader is as before: {error}")
            return False
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        self._info(
            f"KOReader {plan.release.tag} is on your reader and installs itself when KOReader "
            "starts again: in KOReader, Exit → Restart KOReader. The snapshot taken before is "
            "kept (Reader → Snapshots…); should KOReader not start any more, restore it over USB."
        )
        return True

    def check(self, done: Callable[[str], None]) -> None:
        """The KOReader line of "Check for updates" (in the background)."""
        if self.install is None or self.get is None:
            return
        install, get = self.install, self.get
        self._checked = done
        run_in_background(self._results, "check", lambda: update.check(install, get))

    def interrupted(self, restore: Callable[[str], object]) -> QMessageBox | None:
        """After an update that did not finish (cable pulled): offer the snapshot from before."""
        if self.install is None:
            return None
        base = self.store_for(self.install).base
        found = update.pending(base)
        if found is None:
            return None
        update.clear(base)
        box = QMessageBox(self.window)
        box.setObjectName("koreader-update-interrupted")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(APP_NAME)
        box.setText(
            f"The update of KOReader from {found.installed or 'an unknown version'} to "
            f"{found.target} did not finish."
        )
        box.setInformativeText(
            "The reader may have been unplugged while kodesk wrote to it. The snapshot taken "
            "before the update brings KOReader back to how it was."
        )
        restore_button = box.addButton("Restore snapshot…", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Keep as is", QMessageBox.ButtonRole.RejectRole)
        box.finished.connect(
            lambda _result: box.clickedButton() is restore_button and restore(found.snapshot_id)
        )
        box.open()
        return box

    def _warn(self, text: str) -> None:
        QMessageBox.warning(self.window, APP_NAME, text)

    def _info(self, text: str) -> None:
        QMessageBox.information(self.window, APP_NAME, text)
