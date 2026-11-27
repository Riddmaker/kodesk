"""The Garage's Library panel (B-21): list or mosaic, as Cover browser / Project: Title offer.

The mode lives in the plugin's own cache on the reader, not in settings.reader.lua, so this
panel has its own preview and write button (like the sleep screen): the change goes through
kodesk.core.librarymode.write_mode, inside a snapshot.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from kodesk.core.librarymode import LibraryLook, kind, label
from kodesk.core.snapshot import SnapshotCancelledError

# The rows to write → (report for the status line, the look as the reader has it now).
LibraryWriter = Callable[[dict[str, str | None]], tuple[str, LibraryLook]]

CLOSE_KOREADER = "Close KOReader on the reader first: it keeps the cache open while it runs."
UNIFIED_NOTE = (
    "Also sets History and Collections: “unified display mode” is on in {title} on your reader."
)


class LibraryPanel(QGroupBox):
    """Choose the library's look; the drawing follows at once, writing is a separate step."""

    modeChanged = Signal()
    written = Signal(str)  # the report

    def __init__(self, look: LibraryLook, parent: QWidget | None = None) -> None:
        super().__init__("Library", parent)
        self.setObjectName("library-box")
        self.look = look
        self.mode = look.mode
        self.writer: LibraryWriter | None = None
        self.hint = CLOSE_KOREADER
        layout = QVBoxLayout(self)
        self.about = QLabel(self)
        self.about.setObjectName("library-about")
        self.about.setWordWrap(True)
        layout.addWidget(self.about)
        form = QFormLayout()
        self.combo = QComboBox(self)
        self.combo.setObjectName("library-mode")
        self.combo.currentIndexChanged.connect(self._chosen)
        look_label = QLabel("&Look:", self)
        look_label.setBuddy(self.combo)
        form.addRow(look_label, self.combo)
        layout.addLayout(form)
        self.unified = QLabel(self)
        self.unified.setObjectName("library-unified")
        self.unified.setWordWrap(True)
        layout.addWidget(self.unified)
        self.write_button = QPushButton("&Write library look…", self)
        self.write_button.setObjectName("library-write")
        self.write_button.clicked.connect(self.write)
        row = QHBoxLayout()
        row.addWidget(self.write_button)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addStretch(1)
        self.set_look(look)

    def set_look(self, look: LibraryLook) -> None:
        """Show ``look`` as the reader has it (after reading or writing)."""
        self.look = look
        self.mode = look.mode
        drawer = look.drawer
        modes = list(drawer.modes)
        if look.mode not in dict(modes):  # e.g. Project: Title has no classic entry
            modes.insert(0, (look.mode, label(drawer, look.mode)))
        self.combo.blockSignals(True)
        self.combo.clear()
        for mode, text in modes:
            self.combo.addItem(text, mode)
        self.combo.setCurrentIndex([m for m, _ in modes].index(look.mode))
        self.combo.blockSignals(False)
        self.combo.setEnabled(len(modes) > 1)
        self.unified.setText(UNIFIED_NOTE.format(title=drawer.title))
        self.unified.setVisible(look.unified)
        self._update_about()
        self.enable_write()

    def _update_about(self) -> None:
        title = self.look.drawer.title
        if not self.look.drawer.cache:
            text = (
                "Cover browser is switched off on your reader, so KOReader shows its classic "
                "list and there is nothing to choose here (KOReader: Tools → Plugin management)."
            )
        elif kind(self.mode) == "classic":
            text = (
                f"Your book folder as {title} lists it in the classic list, measured from "
                "KOReader's own code (menu.lua)."
            )
        else:
            text = (
                f"Your book folder as {title} draws it. Lists with covers and the mosaic are "
                "drawn approximately, with plain frames instead of your covers."
            )
        self.about.setText(text)

    def set_writer(self, writer: LibraryWriter | None, hint: str | None = None) -> None:
        """``hint``: what the reader must do for the write (default: close KOReader)."""
        self.writer = writer
        self.hint = hint or CLOSE_KOREADER
        self.enable_write()

    def enable_write(self) -> None:
        """Writable: a reader is connected and the plugin's cache is there to write into."""
        possible = self.writer is not None and self.look.read is not None
        self.write_button.setEnabled(possible and self.mode != self.look.mode)
        if self.writer is None:
            reason = "Needs a connected reader"
        elif self.look.read is None:
            reason = (
                f"{self.look.drawer.title} has no cache on your reader yet (or it cannot be "
                "read): open the library on the reader once, close KOReader, reconnect."
            )
        else:
            reason = ""
        self.write_button.setToolTip(reason)

    def _chosen(self, index: int) -> None:
        self.mode = self.combo.itemData(index)
        self._update_about()
        self.enable_write()
        self.modeChanged.emit()

    def preview_lines(self) -> list[str]:
        """What the write changes: the cache file and each row, old → new."""
        old, new = label(self.look.drawer, self.look.mode), label(self.look.drawer, self.mode)
        return (
            [f"{self.look.drawer.cache} (table config):"]
            + [
                f"  {key}: {self.look.mode or 'classic'} → {value or 'classic'}"
                for key, value in self.look.rows(self.mode).items()
            ]
            + [f"Library: {old} → {new}"]
        )

    def confirm(self) -> bool:
        """Preview before write: old and new look, the rows, the KOReader-closed hint."""
        box = QMessageBox(self)
        box.setObjectName("library-preview")
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Write the library look")
        box.setText(
            f"Show the library as “{label(self.look.drawer, self.mode)}” instead of "
            f"“{label(self.look.drawer, self.look.mode)}”? kodesk changes only this setting "
            f"in {self.look.drawer.title}'s cache and takes a snapshot first."
        )
        box.setInformativeText(self.hint)
        box.setDetailedText("\n".join(self.preview_lines()))
        action = box.addButton("Write library look", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        return box.clickedButton() is action

    def write(self) -> None:
        """Preview, then write the chosen look through the writer."""
        if self.writer is None or self.mode == self.look.mode or not self.confirm():
            return
        try:
            report, look = self.writer(self.look.rows(self.mode))
        except SnapshotCancelledError:
            return  # the user stopped it before anything was written
        except Exception as error:  # pylint: disable=broad-exception-caught  # shown, not raised
            QMessageBox.warning(self, "Write the library look", str(error))
            return
        self.set_look(look)
        self.written.emit(report)
