"""DESIGN rule "Possible with an extension" (B-23): the panel side of a locked Garage place.

One line "Needs an extension", one button per extension on kodesk's list ("Name → Store"), and
the neutral "You may find more for this in the Store". Details live in the tooltip, colours come
from the palette (the greyed control is simply disabled). Hidden once a listed extension is
installed: then the control works normally.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from kodesk.core.extensions import AREA_SEARCH, Area, Extension, for_area, installed_for

NEEDS = "Needs an extension"
MORE = "You may find more for this in the Store"


class ExtensionLock(QWidget):
    """The lock note for one Garage area; emits what the Store should search and select."""

    storeRequested = Signal(str, str)  # search text, entry name to select ("" = none)

    def __init__(self, area: Area, details: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.area = area
        self.setObjectName(f"lock-{area.name.lower()}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.needs = QLabel(NEEDS, self)
        self.needs.setObjectName("lock-needs")
        self.needs.setToolTip(details)
        layout.addWidget(self.needs)
        row = QHBoxLayout()
        self.buttons: list[QPushButton] = []
        for extension in for_area(area):
            button = QPushButton(f"{extension.title} → Store", self)
            button.setObjectName(f"lock-store-{extension.plugin}")
            button.setToolTip(f"{extension.unlocks}\n{extension.url}")
            button.clicked.connect(
                lambda _=False, e=extension: self.storeRequested.emit(e.plugin, e.plugin)
            )
            self.buttons.append(button)
            row.addWidget(button)
        row.addStretch(1)
        layout.addLayout(row)
        self.more = QPushButton(MORE, self)
        self.more.setObjectName("lock-more")
        self.more.setFlat(True)
        self.more.clicked.connect(lambda: self.storeRequested.emit(AREA_SEARCH[area], ""))
        more_row = QHBoxLayout()
        more_row.addWidget(self.more)
        more_row.addStretch(1)
        layout.addLayout(more_row)

    def update_for(self, plugins: tuple[str, ...]) -> Extension | None:
        """Show the lock unless a listed extension is installed; return that extension."""
        installed = installed_for(self.area, plugins)
        self.setVisible(installed is None)
        return installed
