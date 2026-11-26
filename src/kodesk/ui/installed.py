"""The Installed tab: plugins and user patches on the connected reader (B-03). Read-only."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QLabel, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from kodesk.core.install import InstallError, KOReaderInstall
from kodesk.core.inventory import RUN_PRIORITIES, Origin, Patch, Plugin, list_patches, list_plugins

COLUMNS = ("Name", "Origin", "State")
UNKNOWN_ORIGIN = "Origin unknown"
UNKNOWN_HINT = "The reader has no ota/package.index, the file list of the installed KOReader."
NO_READER = "Connect a reader (File → Open reader…) to see what is installed on it."


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


class InstalledPage(QWidget):
    """Two groups, plugins and patches; built-in plugins only on request."""

    def __init__(self, parent: QWidget | None) -> None:
        super().__init__(parent)
        self.install: KOReaderInstall | None = None
        self.plugins: tuple[Plugin, ...] = ()
        self.patches: tuple[Patch, ...] = ()
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
        layout = QVBoxLayout(self)
        layout.addWidget(self.summary)
        layout.addWidget(self.show_built_in)
        layout.addWidget(self.tree, 1)
        self.set_install(None)

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
        self.show_built_in.setVisible(install is not None)
        self.tree.setVisible(install is not None)
        if install is None:
            self.summary.setText(NO_READER)
        else:
            self.summary.setText(summary(self.plugins, self.patches) + problem)
        self.refresh()

    def refresh(self) -> None:
        """Rebuild the tree from the last read inventory and the built-in switch."""
        self.tree.clear()
        if self.install is None:
            return
        shown = [
            p
            for p in self.plugins
            if self.show_built_in.isChecked() or p.origin is not Origin.BUILT_IN
        ]
        groups = (
            (f"Plugins ({len(shown)} shown)", [plugin_row(p) for p in shown]),
            (f"User patches ({len(self.patches)})", [patch_row(p) for p in self.patches]),
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
            for row in rows:
                item = QTreeWidgetItem(group, list(row))
                if row[1] == UNKNOWN_ORIGIN:
                    item.setToolTip(1, UNKNOWN_HINT)
            group.setExpanded(True)
        minimum = self.fontMetrics().horizontalAdvance("M" * 14)  # readable when empty
        for column in range(len(COLUMNS) - 1):
            self.tree.resizeColumnToContents(column)
            self.tree.setColumnWidth(column, max(minimum, self.tree.columnWidth(column)))
