"""The Installed tab: plugins and user patches on the connected reader (B-03, B-08, B-09).

The list is read-only; the buttons below it ask the main window to update, remove or set the
source of the selected item, or to check for updates (only on click, never in the background).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from kodesk.core.catalogue import Entry, Kind
from kodesk.core.install import InstallError, KOReaderInstall
from kodesk.core.inventory import RUN_PRIORITIES, Origin, Patch, Plugin, list_patches, list_plugins
from kodesk.core.records import Record
from kodesk.core.sources import probable_sources

COLUMNS = ("Name", "Origin", "State", "Source", "Update")
UNKNOWN_ORIGIN = "Origin unknown"
UNKNOWN_HINT = "The reader has no ota/package.index, the file list of the installed KOReader."
NO_READER = "Connect a reader (File → Open reader…) to see what is installed on it."
UNKNOWN_SOURCE = "Unknown"
Key = tuple[Kind, str]


def plugin_row(plugin: Plugin) -> tuple[str, str, str]:
    origin = {
        Origin.BUILT_IN: "Built in",
        Origin.THIRD_PARTY: "Added by you",
        Origin.UNKNOWN: UNKNOWN_ORIGIN,
    }[plugin.origin]
    return plugin.name, origin, "On" if plugin.enabled else "Off"


def patch_row(patch: Patch) -> tuple[str, str, str]:
    """Patches are always added by the user; the state says whether KOReader runs it, and why."""
    if patch.runs:
        state = "Runs after the next update" if patch.priority == "0" else "Runs at start"
    elif patch.priority in RUN_PRIORITIES:
        state = "Not run: all patches are switched off"
    elif patch.priority is None:
        state = "Not run: name does not start with a priority digit and a dash"
    else:
        state = f"Not run: priority {patch.priority} is reserved"
    return patch.name, "Added by you", state


def source_text(record: Record | None, guesses: tuple[Entry, ...]) -> str:
    """Where an item came from: kodesk's record, else a guess by name (said as such)."""
    if record is not None:
        return f"{record.source} {record.version}".strip()
    if len(guesses) == 1:
        return f"probably {guesses[0].id}"
    if guesses:
        return f"{len(guesses)} possible sources"
    return UNKNOWN_SOURCE


def _count(number: int, one: str, many: str) -> str:
    return f"{number} {one if number == 1 else many}"


def summary(plugins: tuple[Plugin, ...], patches: tuple[Patch, ...]) -> str:
    """One line: how many plugins by origin, how many patches."""
    parts = []
    for origin, label in (
        (Origin.THIRD_PARTY, "added by you"),
        (Origin.BUILT_IN, "built in"),
        (Origin.UNKNOWN, "of unknown origin"),
    ):
        if number := sum(p.origin is origin for p in plugins):
            parts.append(f"{number} {label}")
    detail = f" ({', '.join(parts)})" if parts else ""
    plugin_text = _count(len(plugins), "plugin", "plugins")
    return f"{plugin_text}{detail}, {_count(len(patches), 'user patch', 'user patches')}."


class InstalledPage(QWidget):  # pylint: disable=too-many-instance-attributes  # widgets
    """Two groups, plugins and patches; built-in plugins only on request; actions below."""

    update_requested = Signal(object)  # Key
    remove_requested = Signal(object)  # Key
    source_requested = Signal(object)  # Key
    check_requested = Signal()

    def __init__(self, parent: QWidget | None) -> None:
        super().__init__(parent)
        self.install: KOReaderInstall | None = None
        self.plugins: tuple[Plugin, ...] = ()
        self.patches: tuple[Patch, ...] = ()
        self.records: Mapping[Key, Record] = {}
        self.entries: tuple[Entry, ...] = ()
        self.updates: dict[Key, str] = {}
        self.summary = QLabel(self)
        self.summary.setObjectName("installed-summary")
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.TextFormat.PlainText)  # names come from the device
        self.show_built_in = QCheckBox("Show &built-in plugins", self)
        self.show_built_in.setObjectName("installed-built-in")
        self.show_built_in.toggled.connect(self.refresh)
        self.tree = QTreeWidget(self)
        self.tree.setObjectName("installed-list")
        self.tree.setHeaderLabels(list(COLUMNS))
        self.tree.setRootIsDecorated(False)
        self.tree.currentItemChanged.connect(lambda *_args: self._enable())
        layout = QVBoxLayout(self)
        layout.addWidget(self.summary)
        layout.addWidget(self.show_built_in)
        layout.addWidget(self.tree, 1)
        layout.addLayout(self._buttons())
        self.set_install(None)

    def _buttons(self) -> QHBoxLayout:
        self.update_button = QPushButton("&Update…", self)
        self.remove_button = QPushButton("Re&move…", self)
        self.source_button = QPushButton("Set s&ource…", self)
        self.check_button = QPushButton("&Check for updates", self)
        row = QHBoxLayout()
        for button, name, signal in (
            (self.update_button, "installed-update", self.update_requested),
            (self.remove_button, "installed-remove", self.remove_requested),
            (self.source_button, "installed-source", self.source_requested),
        ):
            button.setObjectName(name)
            button.clicked.connect(lambda _c=False, s=signal: self._emit(s))
            row.addWidget(button)
        self.check_button.setObjectName("installed-check")
        self.check_button.setToolTip("Asks GitHub for the latest version of each item")
        self.check_button.clicked.connect(self.check_requested.emit)
        row.addStretch(1)
        row.addWidget(self.check_button)
        return row

    def _emit(self, signal: Signal) -> None:
        key = self.selected()
        if key is not None:
            signal.emit(key)  # type: ignore[attr-defined]

    def set_install(self, install: KOReaderInstall | None) -> None:
        """Read the inventory of ``install`` (read-only), or show the hint for the demo."""
        self.install = install
        self.plugins, self.patches, problem = (), (), ""
        if install is not None:
            try:
                self.plugins = list_plugins(install.root, install.read_settings())
            except (InstallError, OSError) as error:
                problem = f" On/off could not be read: {error}"
                self.plugins = ()
            self.patches = list_patches(install.root)
        self.updates = {}
        for widget in (self.show_built_in, self.tree, self.check_button):
            widget.setVisible(install is not None)
        for button in (self.update_button, self.remove_button, self.source_button):
            button.setVisible(install is not None)
        if install is None:
            self.summary.setText(NO_READER)
        else:
            self.summary.setText(summary(self.plugins, self.patches) + problem)
        self.refresh()

    def set_sources(self, records: Iterable[Record], entries: Iterable[Entry]) -> None:
        """kodesk's install records and the catalogue (for guesses by name)."""
        self.records = {(r.kind, r.name): r for r in records}
        self.entries = tuple(entries)
        self.refresh()

    def set_updates(self, updates: Mapping[Key, str]) -> None:
        """Results of "Check for updates", per item."""
        self.updates = dict(updates)
        self.refresh()

    def guesses(self, key: Key) -> tuple[Entry, ...]:
        """Catalogue entries matching an item by name."""
        return probable_sources(self.entries, *key)

    def checkable(self) -> list[Key]:
        """Items that are not built in (what "Check for updates" looks at)."""
        plugins = [(Kind.PLUGIN, p.name) for p in self.plugins if p.origin is not Origin.BUILT_IN]
        return plugins + [(Kind.PATCH, p.name) for p in self.patches]

    def selected(self) -> Key | None:
        """The selected plugin or patch, None for a group row or a built-in plugin."""
        item = self.tree.currentItem()
        key = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        return key if isinstance(key, tuple) else None

    def _enable(self) -> None:
        on = self.selected() is not None
        for button in (self.update_button, self.remove_button, self.source_button):
            button.setEnabled(on)

    def _row(self, key: Key, cells: tuple[str, str, str]) -> list[str]:
        source = source_text(self.records.get(key), self.guesses(key))
        return [*cells, source, self.updates.get(key, "")]

    def refresh(self) -> None:
        """Rebuild the tree from the last read inventory and the built-in switch."""
        selected = self.selected()
        self.tree.clear()
        self._enable()
        if self.install is None:
            return
        shown = [
            p
            for p in self.plugins
            if self.show_built_in.isChecked() or p.origin is not Origin.BUILT_IN
        ]
        groups = (
            (
                f"Plugins ({len(shown)} shown)",
                [((Kind.PLUGIN, p.name), plugin_row(p), p.origin) for p in shown],
            ),
            (
                f"User patches ({len(self.patches)})",
                [((Kind.PATCH, p.name), patch_row(p), None) for p in self.patches],
            ),
        )
        for title, rows in groups:
            group = QTreeWidgetItem(self.tree, [title])
            group.setFirstColumnSpanned(True)
            group.setFlags(Qt.ItemFlag.ItemIsEnabled)
            font = group.font(0)
            font.setBold(True)
            group.setFont(0, font)
            if not rows:
                QTreeWidgetItem(group, ["None"]).setFlags(Qt.ItemFlag.ItemIsEnabled)
            for key, cells, origin in rows:
                if origin is Origin.BUILT_IN:
                    item = QTreeWidgetItem(group, [*cells, "KOReader", ""])
                else:
                    item = QTreeWidgetItem(group, self._row(key, cells))
                    item.setData(0, Qt.ItemDataRole.UserRole, key)
                if cells[1] == UNKNOWN_ORIGIN:
                    item.setToolTip(1, UNKNOWN_HINT)
                if key == selected:
                    self.tree.setCurrentItem(item)
            group.setExpanded(True)
        minimum = self.fontMetrics().horizontalAdvance("M" * 14)  # readable when empty
        for column in range(len(COLUMNS) - 1):
            self.tree.resizeColumnToContents(column)
            self.tree.setColumnWidth(column, max(minimum, self.tree.columnWidth(column)))
