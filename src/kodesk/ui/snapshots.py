"""The Snapshots window: the kept snapshots of the reader, restore in one click (B-04)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from kodesk.core.snapshot import RestorePlan, Snapshot, SnapshotError, SnapshotStore

MAX_LISTED = 30  # files named in the restore preview; the rest is counted


def snapshot_label(snapshot: Snapshot) -> str:
    when = snapshot.created.astimezone().strftime("%d %b %Y, %H:%M")
    return f"{when} · {len(snapshot.files)} files · {human_size(snapshot.size)}"


def human_size(size: int) -> str:
    if size < 2**20:
        return f"{max(1, round(size / 1024))} kB"
    return f"{size / 2**20:.0f} MB"


def plan_text(plan: RestorePlan) -> str:
    """The preview: what a restore writes back and removes (names, then a count)."""
    lines = []
    for title, paths in (("Written back", plan.write), ("Removed", plan.remove)):
        if not paths:
            continue
        lines.append(f"{title} ({len(paths)}):")
        lines += [f"  {p}" for p in paths[:MAX_LISTED]]
        if len(paths) > MAX_LISTED:
            lines.append(f"  … and {len(paths) - MAX_LISTED} more")
    return "\n".join(lines)


def _busy[T](action: Callable[[], T]) -> T:
    QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    try:
        return action()
    finally:
        QGuiApplication.restoreOverrideCursor()


class SnapshotsDialog(QDialog):
    """Lists the snapshots kept on this PC for one reader; take one now or restore one."""

    def __init__(self, store: SnapshotStore, koreader_dir: Path, parent: QWidget | None) -> None:
        super().__init__(parent)
        self.store = store
        self.koreader_dir = koreader_dir
        self.setWindowTitle("Snapshots of your reader")
        self.resize(560, 420)
        layout = QVBoxLayout(self)
        intro = QLabel(
            "kodesk takes a snapshot of the KOReader folder before every change and keeps the "
            f"last five on this PC ({store.base}). Books and their reading progress are never "
            "part of it and are never touched.",
            self,
        )
        intro.setWordWrap(True)
        intro.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(intro)
        self.list = QListWidget(self)
        self.list.setObjectName("snapshot-list")
        layout.addWidget(self.list, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        self.take_button = QPushButton("&Take snapshot now", self)
        self.restore_button = QPushButton("&Restore…", self)
        self.restore_button.setObjectName("restore")
        for button in (self.take_button, self.restore_button):
            button.setAutoDefault(False)
        buttons.addButton(self.take_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(self.restore_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.rejected.connect(self.reject)
        # No default button: the Return that opened the window from the menu must not act here.
        buttons.button(QDialogButtonBox.StandardButton.Close).setAutoDefault(False)
        self.take_button.clicked.connect(self.take)
        self.restore_button.clicked.connect(self.restore_selected)
        self.list.currentRowChanged.connect(lambda row: self.restore_button.setEnabled(row >= 0))
        layout.addWidget(buttons)
        self.refresh()

    def refresh(self) -> None:
        """Reload the list from the store, newest first."""
        self.list.clear()
        try:
            snapshots = self.store.snapshots()
        except SnapshotError as error:
            QMessageBox.warning(self, self.windowTitle(), str(error))
            snapshots = []
        for snapshot in snapshots:
            item = QListWidgetItem(snapshot_label(snapshot), self.list)
            item.setData(Qt.ItemDataRole.UserRole, snapshot.id)
        if snapshots:
            self.list.setCurrentRow(0)
        else:
            QListWidgetItem("No snapshot yet.", self.list).setFlags(Qt.ItemFlag.NoItemFlags)
        self.restore_button.setEnabled(bool(snapshots))

    def take(self) -> None:
        """Take a snapshot now (reads the reader, writes only to this PC)."""
        try:
            _busy(lambda: self.store.take(self.koreader_dir))
        except (SnapshotError, OSError) as error:
            QMessageBox.warning(self, self.windowTitle(), f"No snapshot taken: {error}")
        self.refresh()

    def selected(self) -> Snapshot | None:
        """The snapshot selected in the list, if any."""
        item = self.list.currentItem()
        snapshot_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        return self.store.load(snapshot_id) if isinstance(snapshot_id, str) else None

    def restore_selected(self) -> None:
        """Preview, confirm and restore the selected snapshot."""
        try:
            snapshot = self.selected()
            if snapshot is None:
                return
            plan = _busy(lambda: self.store.plan_restore(self.koreader_dir, snapshot))
        except (SnapshotError, OSError) as error:
            QMessageBox.warning(self, self.windowTitle(), str(error))
            return
        if not plan:
            QMessageBox.information(
                self, self.windowTitle(), "Your reader already matches this snapshot."
            )
            return
        if self.confirm(snapshot, plan):
            try:
                _busy(lambda: self.store.restore(self.koreader_dir, snapshot))
            except (SnapshotError, OSError) as error:
                QMessageBox.critical(self, self.windowTitle(), f"Restore failed: {error}")
                return
            QMessageBox.information(
                self, self.windowTitle(), "Restored. Your reader matches the snapshot again."
            )

    def confirm(self, snapshot: Snapshot, plan: RestorePlan) -> bool:
        """Preview before write: counts, the file list, and a button that names the action."""
        box = QMessageBox(self)
        box.setObjectName("restore-preview")
        box.setWindowTitle(self.windowTitle())
        box.setIcon(QMessageBox.Icon.Question)
        when = snapshot.created.astimezone().strftime("%d %b %Y, %H:%M")
        box.setText(
            f"Restore the snapshot of {when}? {len(plan.write)} files are written back and "
            f"{len(plan.remove)} files added since then are removed."
        )
        box.setDetailedText(plan_text(plan))
        restore = box.addButton(f"Restore snapshot of {when}", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        return box.clickedButton() is restore
