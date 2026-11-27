"""The Garage's hot corners (B-22): tap and long press on each corner of an open book.

Two combo boxes per corner. A corner whose entry kodesk does not offer (several actions, an
action not on the list) shows that entry as its first choice and keeps it untouched until the
user picks something else. Changes join the pending settings changes; the Garage writes them
together with settings.reader.lua, inside one snapshot.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QComboBox, QGridLayout, QGroupBox, QLabel, QVBoxLayout, QWidget

from kodesk.core import hotcorners as hc
from kodesk.core.luadump import SettingsDocument, changed_paths

KEEP = "\0keep"  # combo data of "the reader's own entry, as it is"
NO_FILE = (
    "KOReader has not saved its gestures on this reader yet (settings/gestures.lua), so the "
    "corners show KOReader's defaults and cannot be changed here. Open a book on the reader "
    "once, close KOReader, then reconnect."
)
KEPT_TIP = "As on your reader: kodesk keeps it exactly as it is unless you pick another action."
HOLD_OFF = (
    "Long press on corners is switched off on your reader (“Ignore long-press on corners”); "
    "the long-press actions only work after switching it back on there."
)


class CornersPanel(QGroupBox):
    """Hot corners in an open book; ``original`` is the reader's gestures.lua (None: none)."""

    cornersChanged = Signal()

    def __init__(
        self, original: SettingsDocument | None, hold_off: bool, parent: QWidget | None = None
    ) -> None:
        super().__init__("Corners (book open)", parent)
        self.setObjectName("corners-box")
        self.original = original
        self.edited = original
        self.combos: dict[str, QComboBox] = {}  # KOReader gesture name → its combo box
        layout = QVBoxLayout(self)
        grid = QGridLayout()
        grid.addWidget(QLabel("Tap", self), 0, 1)
        grid.addWidget(QLabel("Long press", self), 0, 2)
        for row, corner in enumerate(hc.CORNERS, start=1):
            grid.addWidget(QLabel(f"{corner.label}:", self), row, 0)
            for column, (gesture, how) in enumerate(
                ((corner.tap, "tap"), (corner.hold, "long press")), start=1
            ):
                combo = self._combo(gesture, f"{corner.label} corner, {how}")
                combo.setObjectName(f"corner-{corner.id}-{'tap' if column == 1 else 'hold'}")
                grid.addWidget(combo, row, column)
        layout.addLayout(grid)
        for text, shown, name in (
            (NO_FILE, original is None, "corners-no-file"),
            (HOLD_OFF, hold_off, "corners-hold-off"),
        ):
            if shown:
                note = QLabel(text, self)
                note.setObjectName(name)
                note.setWordWrap(True)
                layout.addWidget(note)

    def _combo(self, gesture: str, name: str) -> QComboBox:
        combo = QComboBox(self)
        combo.setAccessibleName(name)
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        combo.setMinimumContentsLength(14)  # the panel never scrolls sideways
        value = hc.current(self.original, gesture)
        try:
            current: str | None = hc.action_of(value)
        except KeyError:
            combo.addItem(hc.describe(value), KEEP)
            combo.setItemData(0, KEPT_TIP, Qt.ItemDataRole.ToolTipRole)
            current = KEEP
        combo.addItem(hc.NOTHING, None)
        for action, title in hc.ACTIONS:
            combo.addItem(title, action)
        combo.setCurrentIndex(combo.findData(current))
        combo.setEnabled(self.original is not None)
        combo.currentIndexChanged.connect(lambda _i: self._chosen())
        self.combos[gesture] = combo
        return combo

    def _chosen(self) -> None:
        """Rebuild the edited file from the reader's: only corners whose choice differs from
        the reader's entry are replaced, so every other entry keeps its exact bytes."""
        if self.original is None:
            return
        edited = self.original
        for gesture, combo in self.combos.items():
            chosen = combo.currentData()
            try:
                own: str | None = hc.action_of(self.original.get(hc.path(gesture)))
            except KeyError:
                own = KEEP
            if chosen != own:
                edited = hc.with_action(edited, gesture, chosen)
        self.edited = edited
        self.cornersChanged.emit()

    def changed(self) -> list[str]:
        """The gestures whose entry differs from the reader's, in KOReader's order."""
        if self.original is None or self.edited is None:
            return []
        paths = changed_paths(self.original.data, self.edited.data)
        names = {str(p[1]) for p in paths if len(p) > 1 and p[0] == hc.SECTION}
        return [g for c in hc.CORNERS for g in (c.tap, c.hold) if g in names]

    def lines(self) -> list[str]:
        """One preview line per changed corner gesture."""
        result = []
        for gesture in self.changed():
            corner = next(c for c in hc.CORNERS if gesture in (c.tap, c.hold))
            how = "tap" if gesture == corner.tap else "long press"
            old = hc.describe(hc.current(self.original, gesture))
            new = hc.describe(hc.current(self.edited, gesture))
            result.append(f"gestures.lua: {corner.label} corner, {how}: {old} → {new}")
        return result

    def written(self) -> None:
        """The edits are on the reader now: they become the baseline."""
        self.original = self.edited

    def summary(self, corner: hc.Corner) -> str:
        """What a corner does, for the status line."""
        tap = hc.describe(hc.current(self.edited, corner.tap))
        hold = hc.describe(hc.current(self.edited, corner.hold))
        return f"{corner.label} corner: tap → {tap} · long press → {hold}"
