"""The Garage's Sleep screen panel (B-14): drop or choose a picture for the PocketBook."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QImage
from PySide6.QtWidgets import (
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

SleepWriter = Callable[[bytes], str]  # PNG bytes → where on the reader they went


class SleepDropPanel(QGroupBox):
    """Drop zone for a sleep-screen picture, stating the exact target size."""

    imageDropped = Signal(QImage)

    def __init__(self, width: int, height: int, parent: QWidget | None = None) -> None:
        super().__init__("Sleep screen", parent)
        self.setAcceptDrops(True)
        self.image: QImage | None = None
        self.writer: SleepWriter | None = None
        layout = QVBoxLayout(self)
        self.hint = QLabel(
            f"<p>Drop a picture here or choose one.</p><p><b>Best size: {width} × {height} px"
            "</b> (portrait). Other sizes are scaled to fit, on white.</p>"
            "<p>On PocketBook the firmware draws the sleep screen, not KOReader: kodesk "
            "adds the picture to the reader's <code>Photo</code> folder; you then choose it "
            "on the reader under Settings → Personalize → Logos.</p>",
            self,
        )
        self.hint.setWordWrap(True)
        self.hint.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.hint)
        choose = QPushButton("&Choose picture…", self)
        choose.setObjectName("sleep-choose")
        choose.clicked.connect(self.choose)
        self.write_button = QPushButton("&Put on reader…", self)
        self.write_button.setObjectName("sleep-write")
        self.write_button.setToolTip("Needs a connected PocketBook")
        self.write_button.setEnabled(False)
        row = QHBoxLayout()
        row.addWidget(choose)
        row.addWidget(self.write_button)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addStretch(1)

    def enable_write(self) -> None:
        self.write_button.setEnabled(self.writer is not None and self.image is not None)

    def choose(self) -> None:
        """The keyboard way to the drop zone: a file dialog for one picture."""
        path, _filter = QFileDialog.getOpenFileName(
            self, "Choose a picture", "", "Pictures (*.png *.jpg *.jpeg *.bmp *.gif *.webp)"
        )
        image = QImage(path) if path else QImage()
        if not image.isNull():
            self.imageDropped.emit(image)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls() or event.mimeData().hasImage():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        data = event.mimeData()
        image = QImage()
        if data.hasImage():
            image = QImage(data.imageData())
        elif data.hasUrls():
            image = QImage(data.urls()[0].toLocalFile())
        if not image.isNull():
            self.imageDropped.emit(image)
            event.acceptProposedAction()
