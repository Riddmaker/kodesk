"""Tuning-Garage spike (B-02): the reader in the middle, its settings around it.

Read-only in this spike: edits change an in-memory copy of settings.reader.lua and are listed as
a diff; writing to the reader waits for snapshot/restore (B-04, hard rule "snapshot before every
write").
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from functools import partial
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QSplitter,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from kodesk.core.devices import Control, DeviceModel, Face
from kodesk.core.extensions import Area, Extension, installed_for
from kodesk.core.layout import (
    DEFAULT_B_MARGIN,
    DEFAULT_H_MARGINS,
    DEFAULT_NIGHTMODE_IMAGES,
    DEFAULT_STATUS_LINE,
    DEFAULT_T_MARGIN,
    book_layout,
)
from kodesk.core.librarymode import COVER_BROWSER, LibraryLook
from kodesk.core.librarymode import label as mode_label
from kodesk.core.luadump import KeyPath, LuaValue, SettingsDocument, changed_paths
from kodesk.core.statusbar import (
    ALIGNMENTS,
    BAR_POSITIONS,
    DEFAULT_FOOTER_MODE,
    FONT_SIZE_RANGE,
    FOOTER_DEFAULTS,
    FOOTER_ITEMS,
    FOOTER_KEY,
    FOOTER_MODE_KEY,
    HEADER_ITEMS,
    ITEM_PREFIXES,
    SEPARATORS,
    footer_int,
    footer_table,
    footer_value,
    footer_with,
    header_key,
)
from kodesk.ui.device_view import DeviceView, Spot
from kodesk.ui.extensions import ExtensionLock
from kodesk.ui.library_panel import LibraryPanel, LibraryWriter
from kodesk.ui.screen import (
    BookRenderer,
    Shelf,
    fit_picture,
    render_library,
    render_menu,
    render_sleep,
    tab_bar_rect,
)
from kodesk.ui.sleep_panel import SleepDropPanel, SleepWriter


class View(StrEnum):
    """What the reader shows."""

    BOOK = "Book open"
    MENU = "Menu"
    LIBRARY = "Library"
    SLEEP = "Sleep screen"


FACE_LABELS = {
    Face.FRONT: "Front",
    Face.BOTTOM: "Bottom edge",
    Face.BACK: "Back",
    Face.LEFT: "Left edge",
    Face.RIGHT: "Right edge",
    Face.TOP: "Top edge",
}


@dataclass
class ReaderContext:
    """Everything the garage reads from the reader (all read-only)."""

    device: DeviceModel
    settings: SettingsDocument
    koreader_version: str | None = None
    font_families: tuple[str, ...] = ()
    library_entries: tuple[str, ...] = ()
    sleep_image: Path | None = None
    source_label: str = "demo (no reader connected)"
    plugins: tuple[str, ...] = ()  # installed plugin names, e.g. "pbfixes"
    icons: dict[str, Path] = field(default_factory=dict)  # icon name → file on the reader
    library: LibraryLook = field(default_factory=lambda: LibraryLook(COVER_BROWSER, None))


def _segmented(labels: list[str], parent: QWidget) -> tuple[QWidget, QButtonGroup]:
    box = QWidget(parent)
    layout = QHBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    group = QButtonGroup(box)
    for index, label in enumerate(labels):
        button = QToolButton(box)
        button.setText(label)
        button.setCheckable(True)
        button.setAutoRaise(False)
        button.setObjectName(f"seg-{label}")
        group.addButton(button, index)
        layout.addWidget(button)
    group.button(0).setChecked(True)
    return box, group


INVERT_KEY: KeyPath = ("input_invert_page_turn_keys",)
NIGHT_PICTURES_KEY: KeyPath = ("copt_nightmode_images",)
PAGE_KEYS = ("LPgFwd", "LPgBack")


@dataclass(frozen=True)
class SliderSpec:
    """One numeric setting shown as a slider."""

    name: str  # objectName, for tests and the accessibility tree
    label: str
    path: KeyPath
    low: int
    high: int
    default: int  # KOReader's default, shown while the reader has no value


@dataclass(frozen=True)
class KeyOption:
    """A hardware-key option that an extension on kodesk's list adds (B-23)."""

    area: Area
    label: str
    path: KeyPath  # one of the extension's keys (kodesk.core.extensions)
    default: bool  # the extension's value while the key is absent


# pbfixes reads its key with G_reader_settings:nilOrTrue (absent = on).
KEY_OPTIONS = {
    "Home": KeyOption(Area.HOME_KEY, "Home button acts as Back", ("pbfixes_home_back",), True),
}
HOME_LOCK_DETAILS = (
    "KOReader has no setting for the Home button on PocketBook (its Hotkeys plugin needs a "
    "keyboard). An extension can make Home act as Back; once one is installed on the reader, "
    "this option works here. The reader's own settings must leave a single press of Home to "
    "KOReader."
)
ICONS_LOCK_DETAILS = (
    "KOReader draws its own icon set. An extension can put other icon packs on the reader; "
    "kodesk then draws the icons your reader really shows."
)


def key_description(control: Control, plugins: tuple[str, ...] = ()) -> str:
    """What KOReader does with a hardware control on this device, and what can be changed."""
    if control.koreader_key in PAGE_KEYS:
        return (
            "KOReader can swap the two page-turn buttons. Other actions cannot be "
            "assigned to them in KOReader on this device."
        )
    if control.koreader_key == "Home":
        extension = installed_for(Area.HOME_KEY, plugins)
        if extension is not None:
            return (
                "In KOReader: opens the library (file browser). <b>Unlocked</b> by "
                f"{extension.title} on your reader: {extension.unlocks}."
            )
        return (
            "In KOReader: opens the library (file browser). <b>Locked:</b> KOReader has no "
            "setting for this button on PocketBook."
        )
    if control.koreader_key == "Menu":
        return (
            "In KOReader: opens and closes the main menu. <b>Locked:</b> no KOReader setting "
            "or known plugin changes it on PocketBook. Holding it powers the reader off "
            "(firmware)."
        )
    return "KOReader does not see this control."


class KeyPanel(QGroupBox):
    """Details of the selected hardware button and the settings that change it."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Button", parent)
        self.setObjectName("key-box")
        layout = QVBoxLayout(self)
        self.info = QLabel("Click a button on the reader to see and change what it does.", self)
        self.info.setWordWrap(True)
        self.info.setObjectName("key-info")
        layout.addWidget(self.info)
        self.invert = QCheckBox("Swap page-turn buttons (forward ↔ backward)", self)
        self.invert.setObjectName("invert-page-turn")
        self.invert.setVisible(False)
        layout.addWidget(self.invert)
        self.unlock = QCheckBox(self)  # the option an extension adds to a locked key
        self.unlock.setObjectName("key-unlock")
        self.unlock.setVisible(False)
        layout.addWidget(self.unlock)
        self.lock = ExtensionLock(Area.HOME_KEY, HOME_LOCK_DETAILS, self)
        self.lock.setVisible(False)
        layout.addWidget(self.lock)
        self.current_unlock: KeyOption | None = None

    def show_control(
        self,
        control: Control,
        plugins: tuple[str, ...] = (),
        value_of: Callable[[KeyPath], LuaValue | None] = lambda _path: None,
    ) -> None:
        """Describe one control; options appear only where KOReader or a plugin offers one."""
        self.setTitle(f"Button: {control.label}")
        self.info.setText(
            f"<b>Reader firmware:</b> {control.firmware_role}<br>"
            f"<b>KOReader key:</b> {control.koreader_key or '—'}<br>"
            f"{key_description(control, plugins)}"
        )
        self.invert.setVisible(control.koreader_key in PAGE_KEYS)
        option = KEY_OPTIONS.get(control.koreader_key or "")
        installed = installed_for(option.area, plugins) if option else None
        self.current_unlock = option if installed is not None else None
        # Possible with an extension: the option is shown greyed, with the lock note.
        self.unlock.setVisible(option is not None)
        self.unlock.setEnabled(installed is not None)
        self.lock.setVisible(option is not None and installed is None)
        if option is not None:
            value = value_of(option.path) if installed is not None else None
            self.unlock.blockSignals(True)  # showing the state is not a change
            self.unlock.setText(option.label)
            self.unlock.setToolTip("" if installed else HOME_LOCK_DETAILS)
            self.unlock.setChecked(
                installed is not None and (option.default if value is None else value is True)
            )
            self.unlock.blockSignals(False)


# Writes the edited settings to the reader; returns a short report for the status line.
SettingsWriter = Callable[[SettingsDocument, SettingsDocument], str]


class ChangesBox(QGroupBox):
    """The pending changes and the button that writes them (only with a connected reader)."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__("Changed settings (not written yet)", parent)
        self.writer: SettingsWriter | None = None
        self.list = QListWidget(self)
        self.list.setObjectName("changes")
        self.write_button = QPushButton("Write to reader…", self)
        self.write_button.setObjectName("write")
        self.write_button.setEnabled(False)
        self.write_button.setToolTip(
            "Writes only the settings listed above, after a snapshot (Reader → Snapshots…)."
        )
        layout = QVBoxLayout(self)
        layout.addWidget(self.list)
        layout.addWidget(self.write_button)

    def lines(self) -> list[str]:
        return [self.list.item(i).text() for i in range(self.list.count())]


class TuningGarage(QWidget):  # pylint: disable=too-many-instance-attributes  # widgets
    """The Tuning-Garage: the drawn reader in the middle, its settings around it."""

    settingsChanged = Signal()
    storeRequested = Signal(str, str)  # Store search text, entry name to select

    def __init__(self, context: ReaderContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.original = context.settings
        self.edited = context.settings
        self.view = View.BOOK
        self.previous_view = View.BOOK
        self.page = 0
        self.library_page = 0
        width, height = context.device.screen_px
        self.sleep_panel = SleepDropPanel(width, height, self)
        self.library_panel = LibraryPanel(context.library, self)
        self.library_panel.modeChanged.connect(self._library_mode_changed)
        self._renderer: BookRenderer | None = None

        self.device_view = DeviceView(context.device, self)
        self.device_view.keyPressed.connect(self.press_key)
        self.device_view.keyLongPressed.connect(self.long_press_key)
        self.status = QLabel(self)
        self.status.setObjectName("garage-status")
        self.status.setWordWrap(True)
        self.library_panel.written.connect(self.status.setText)
        view_switch, self.view_group = _segmented([v.value for v in View], self)
        self.view_group.idClicked.connect(lambda index: self.set_view(list(View)[index]))

        self.panels = QStackedWidget(self)
        self.key_panel = KeyPanel(self)
        self.key_panel.invert.setChecked(self._value(INVERT_KEY) is True)
        self.key_panel.invert.toggled.connect(self._set_invert)
        self.key_panel.unlock.toggled.connect(self._set_unlocked_option)
        self.key_panel.lock.storeRequested.connect(self.storeRequested)
        self.device_view.spotClicked.connect(self.select_spot)
        self.device_view.set_locked_keys(
            {"home"} if installed_for(Area.HOME_KEY, context.plugins) is None else set()
        )
        self.changes_box = ChangesBox(self)
        self.icons_lock = ExtensionLock(Area.ICONS, ICONS_LOCK_DETAILS, self)
        self.sleep_panel.imageDropped.connect(self._sleep_dropped)
        self.sleep_panel.write_button.clicked.connect(self.write_sleep_picture)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.addWidget(self._build_center(view_switch))
        splitter.addWidget(self._build_right())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)
        self.refresh()

    def _build_center(self, view_switch: QWidget) -> QWidget:
        """View switch and side switch above the drawn reader, status line below it."""
        device = self.context.device
        face_switch, face_group = _segmented([FACE_LABELS[f] for f in device.faces], self)
        face_group.idClicked.connect(lambda index: self.device_view.set_face(device.faces[index]))
        center = QWidget(self)
        center_layout = QVBoxLayout(center)
        top_row = QHBoxLayout()
        top_row.addWidget(view_switch)
        top_row.addStretch(1)
        top_row.addWidget(QLabel("Side:", self))
        top_row.addWidget(face_switch)
        center_layout.addLayout(top_row)
        center_layout.addWidget(self.device_view, 1)
        center_layout.addWidget(self.status)
        return center

    def _build_right(self) -> QWidget:
        """Settings panel of the current view, the selected button, and the pending diff."""
        book = QWidget(self)
        book_layout_ = QVBoxLayout(book)
        book_layout_.setContentsMargins(0, 0, 0, 0)
        book_layout_.addWidget(self._book_panel())
        book_layout_.addWidget(self._header_panel())
        book_layout_.addWidget(self._footer_panel())
        book_layout_.addStretch(1)
        scroll = QScrollArea(self)
        scroll.setObjectName("book-settings")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(book)
        self.panels.addWidget(scroll)
        self.panels.addWidget(self._menu_panel())
        self.panels.addWidget(self.library_panel)
        self.panels.addWidget(self.sleep_panel)
        self.changes_box.write_button.clicked.connect(self.write_changes)
        right = QWidget(self)
        right_layout = QVBoxLayout(right)
        right_layout.addWidget(self.panels, 1)
        right_layout.addWidget(self.key_panel)
        right_layout.addWidget(self.changes_box)
        return right

    # ----------------------------------------------------------------- settings model

    def _value(self, path: KeyPath) -> LuaValue | None:
        return self.edited.get(path)

    def _set(self, path: KeyPath, value: LuaValue | None) -> None:
        """Change one key in the in-memory copy; None restores the reader's own value."""
        if value is None:
            original = self.original.get(path)
            self.edited = (
                self.edited.delete(path) if original is None else self.edited.set(path, original)
            )
        else:
            self.edited = self.edited.set(path, value)
        self._renderer = None
        self.refresh()
        self.settingsChanged.emit()

    def _set_or_default(self, path: KeyPath, value: LuaValue, default: LuaValue) -> None:
        """Like _set, but going back to KOReader's default on a key the reader never had
        leaves the key unset: no stray line in the diff (only visibly changed keys)."""
        unset = self.original.get(path) is None and value == default
        self._set(path, None if unset else value)

    def _set_footer(self, key: str, value: LuaValue) -> None:
        """One status-bar setting (see kodesk.core.statusbar: KOReader reads the ``footer``
        table whole). Inside the reader's own table only that key changes; a reader without a
        table gets KOReader's default table with the change, as KOReader would save it."""
        if footer_table(self.original.data) is not None:
            default = FOOTER_DEFAULTS.get(key, False)
            self._set_or_default((FOOTER_KEY, key), value, False if default is True else default)
        else:
            self._set((FOOTER_KEY,), footer_with(self.edited.data, key, value))

    def _set_footer_shown(self, on: bool) -> None:
        """Off is mode 0; on brings back the reader's own mode (or KOReader's default)."""
        original = self.original.get((FOOTER_MODE_KEY,))
        if not on:
            self._set((FOOTER_MODE_KEY,), 0)
        else:
            self._set((FOOTER_MODE_KEY,), None if original != 0 else DEFAULT_FOOTER_MODE)
            if footer_value(self.edited.data, "disabled") is True:
                self._set_footer("disabled", False)

    def _set_invert(self, on: bool) -> None:
        self._set_or_default(INVERT_KEY, on, False)

    def _set_unlocked_option(self, on: bool) -> None:
        unlock = self.key_panel.current_unlock
        if unlock is not None:
            self._set_or_default(unlock.path, on, unlock.default)

    def changed(self) -> list[KeyPath]:
        """Key paths whose value differs from the reader's own settings."""
        return changed_paths(self.original.data, self.edited.data)

    def set_writer(self, writer: SettingsWriter | None) -> None:
        """Allow writing (a connected reader); without a writer the garage only previews."""
        self.changes_box.writer = writer
        self.refresh()

    def confirm_write(self, count: int) -> bool:
        """Preview before write: the changes, the KOReader-closed hint, a named action."""
        box = QMessageBox(self)
        box.setObjectName("write-preview")
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Write to your reader")
        box.setText(
            f"Write {count} changed setting{'s' if count != 1 else ''} to your reader? "
            "kodesk takes a snapshot first; every other setting stays exactly as it is."
        )
        box.setInformativeText(
            "Close KOReader on the reader first: it saves its own settings when it exits and "
            "would overwrite these changes."
        )
        box.setDetailedText("\n".join(self.changes_box.lines()))
        action = box.addButton(
            f"Write {count} setting{'s' if count != 1 else ''}", QMessageBox.ButtonRole.AcceptRole
        )
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        return box.clickedButton() is action

    def write_changes(self) -> None:
        """Write the pending changes through the writer; on success they become the baseline."""
        count = len(self.changed())
        writer = self.changes_box.writer
        if writer is None or not count or not self.confirm_write(count):
            return
        try:
            report = writer(self.original, self.edited)
        except Exception as error:  # pylint: disable=broad-exception-caught  # shown, not raised
            QMessageBox.warning(self, "Write to your reader", str(error))
            return
        self.original = self.edited
        self.refresh()
        self.status.setText(report)

    # ----------------------------------------------------------------- panels

    def _slider(self, form: QFormLayout, spec: SliderSpec) -> QSlider:
        slider = QSlider(Qt.Orientation.Horizontal, self)
        slider.setObjectName(spec.name)
        slider.setRange(spec.low, spec.high)
        current = self._value(spec.path)
        slider.setValue(int(current) if isinstance(current, (int, float)) else spec.default)
        value_label = QLabel(str(slider.value()), self)
        value_label.setMinimumWidth(32)
        slider.valueChanged.connect(lambda v: value_label.setText(str(v)))
        slider.valueChanged.connect(lambda v: self._set(spec.path, v))
        row = QHBoxLayout()
        row.addWidget(slider, 1)
        row.addWidget(value_label)
        form.addRow(spec.label, row)
        return slider

    def _book_panel(self) -> QWidget:
        panel = QGroupBox("Book open — defaults for books without own settings", self)
        form = QFormLayout(panel)
        layout = book_layout(self.context.device, self.edited.data)
        font_combo = QComboBox(self)
        font_combo.setObjectName("font-face")
        families = list(self.context.font_families) or [layout.font_face]
        if layout.font_face not in families:
            families.insert(0, layout.font_face)
        font_combo.addItems(families)
        font_combo.setCurrentText(layout.font_face)
        font_combo.currentTextChanged.connect(lambda face: self._set(("cre_font",), face))
        form.addRow("Font", font_combo)
        left, right = DEFAULT_H_MARGINS
        for spec in (
            SliderSpec(
                "font-size", "Font size", ("copt_font_size",), 10, 60, round(layout.font_size)
            ),
            SliderSpec(
                "line-spacing",
                "Line spacing %",
                ("copt_line_spacing",),
                70,
                160,
                layout.line_spacing_pct,
            ),
            SliderSpec("margin-left", "Left margin", ("copt_h_page_margins", 1), 0, 100, left),
            SliderSpec("margin-right", "Right margin", ("copt_h_page_margins", 2), 0, 100, right),
            SliderSpec(
                "margin-top", "Top margin", ("copt_t_page_margin",), 0, 140, DEFAULT_T_MARGIN
            ),
            SliderSpec(
                "margin-bottom", "Bottom margin", ("copt_b_page_margin",), 0, 140, DEFAULT_B_MARGIN
            ),
        ):
            self._slider(form, spec)
        night = QCheckBox("Night mode", self)
        night.setObjectName("night-mode")
        night.setChecked(layout.night_mode)
        night.toggled.connect(lambda on: self._set(("night_mode",), on))
        form.addRow(night)
        pictures = QCheckBox("Pictures in original colours", self)
        pictures.setObjectName("night-pictures")
        pictures.setToolTip(
            "In night mode, show pictures as they are instead of as negatives "
            "(KOReader: Invert Images, EPUB). Only matters while night mode is on."
        )
        pictures.setChecked(layout.night_pictures_original)
        pictures.setEnabled(layout.night_mode)
        night.toggled.connect(pictures.setEnabled)
        pictures.toggled.connect(
            lambda on: self._set_or_default(
                NIGHT_PICTURES_KEY, 1 if on else 0, DEFAULT_NIGHTMODE_IMAGES
            )
        )
        form.addRow("", pictures)  # field column: indented under night mode
        return panel

    def _check(self, name: str, label: str, on: bool, then: Callable[[bool], None]) -> QCheckBox:
        box = QCheckBox(label, self)
        box.setObjectName(name)
        box.setChecked(on)
        box.toggled.connect(then)
        return box

    def _combo(
        self,
        name: str,
        choices: tuple[tuple[str, str], ...],
        current: str,
        then: Callable[[str], None],
    ) -> QComboBox:
        combo = QComboBox(self)
        combo.setObjectName(name)
        for value, label in choices:
            combo.addItem(label, value)
        combo.setCurrentIndex(max(0, combo.findData(current)))
        combo.currentIndexChanged.connect(lambda _index: then(str(combo.currentData())))
        return combo

    def _header_panel(self) -> QWidget:
        """crengine's alternative status bar at the top (readercoptlistener.lua)."""
        panel = QGroupBox("Status bar at the top", self)
        form = QFormLayout(panel)
        layout = book_layout(self.context.device, self.edited.data)
        shown = self._check(
            "alt-status-bar",
            "Show the alternative status bar",
            layout.header is not None,
            lambda on: self._set_or_default(
                ("copt_status_line",), 0 if on else 1, DEFAULT_STATUS_LINE
            ),
        )
        form.addRow(shown)
        items = QWidget(self)
        items_form = QFormLayout(items)
        items_form.setContentsMargins(0, 0, 0, 0)
        for item, label, default in HEADER_ITEMS:
            path = (header_key(item),)
            items_form.addRow(
                self._check(
                    f"header-{item}",
                    label,
                    self._on(path, default),
                    partial(self._set_header_item, path, default),
                )
            )
        items.setEnabled(layout.header is not None)
        shown.toggled.connect(items.setEnabled)
        form.addRow(items)
        size = self._slider(
            form,
            SliderSpec(
                "header-font-size",
                "Font size",
                ("cre_header_status_font_size",),
                8,
                40,
                layout.header_font_px,
            ),
        )
        size.setEnabled(layout.header is not None)
        shown.toggled.connect(size.setEnabled)
        return panel

    def _set_header_item(self, path: KeyPath, default: int, on: bool) -> None:
        self._set_or_default(path, 1 if on else 0, default)

    def _on(self, path: KeyPath, default: int) -> bool:
        value = self._value(path)
        return (value if isinstance(value, int) and not isinstance(value, bool) else default) == 1

    def _footer_panel(self) -> QWidget:
        """KOReader's own status bar at the bottom (readerfooter.lua)."""
        panel = QGroupBox("Status bar at the bottom", self)
        form = QFormLayout(panel)
        settings = self.edited.data
        mode = settings.get(FOOTER_MODE_KEY)
        shown = self._check(
            "footer-shown",
            "Show the status bar",
            mode != 0 and footer_value(settings, "disabled") is not True,
            self._set_footer_shown,
        )
        form.addRow(shown)
        details = QWidget(self)
        details_form = QFormLayout(details)
        details_form.setContentsMargins(0, 0, 0, 0)

        def check(key: str, label: str, invert: bool = False) -> QCheckBox:
            on = footer_value(settings, key) is True
            return self._check(
                f"footer-{key}",
                label,
                on != invert,
                lambda value: self._set_footer(key, value != invert),
            )

        details_form.addRow(check("all_at_once", "Show all selected items at once"))
        for key, label in FOOTER_ITEMS:
            details_form.addRow("", check(key, label))  # field column: indented items
        details_form.addRow(check("disable_progress_bar", "Progress bar", invert=True))

        def combo(key: str, choices: tuple[tuple[str, str], ...]) -> QComboBox:
            current = footer_value(settings, key)
            return self._combo(
                f"footer-{key}",
                choices,
                current if isinstance(current, str) else str(FOOTER_DEFAULTS[key]),
                lambda value: self._set_footer(key, value),
            )

        details_form.addRow("Progress bar position", combo("progress_bar_position", BAR_POSITIONS))
        details_form.addRow("Item prefix", combo("item_prefix", ITEM_PREFIXES))
        details_form.addRow("Separator", combo("items_separator", SEPARATORS))
        details_form.addRow("Alignment", combo("align", ALIGNMENTS))
        slider = QSlider(Qt.Orientation.Horizontal, self)
        slider.setObjectName("footer-text_font_size")
        slider.setRange(*FONT_SIZE_RANGE)
        slider.setValue(footer_int(settings, "text_font_size"))
        value_label = QLabel(str(slider.value()), self)
        value_label.setMinimumWidth(32)
        slider.valueChanged.connect(lambda v: value_label.setText(str(v)))
        slider.valueChanged.connect(lambda v: self._set_footer("text_font_size", v))
        row = QHBoxLayout()
        row.addWidget(slider, 1)
        row.addWidget(value_label)
        details_form.addRow("Font size", row)
        details_form.addRow(check("text_font_bold", "Bold"))
        details.setEnabled(shown.isChecked())
        shown.toggled.connect(details.setEnabled)
        form.addRow(details)
        return panel

    def _icons_extension(self) -> Extension | None:
        return installed_for(Area.ICONS, self.context.plugins)

    def _menu_panel(self) -> QWidget:
        """The Menu view: what is drawn, and the icons (locked without an icon extension)."""
        panel = QGroupBox("Menu", self)
        panel.setObjectName("menu-box")
        layout = QVBoxLayout(panel)
        drawn = (
            "The tab icons are the files on your reader (its icons folder first, as KOReader does)."
            if self.context.icons
            else "The tab icons are drawn as plain signs (no icon files from a reader)."
        )
        about = QLabel(
            "KOReader's top menu over the page, measured from KOReader's own code "
            f"(touchmenu.lua). {drawn}",
            panel,
        )
        about.setWordWrap(True)
        layout.addWidget(about)
        extension = self._icons_extension()
        pack = QLabel(panel)
        pack.setObjectName("icon-pack")
        pack.setWordWrap(True)
        if extension is None:
            pack.setText("Icon pack: KOReader's own")
            pack.setEnabled(False)  # possible with an extension: greyed
            pack.setToolTip(ICONS_LOCK_DETAILS)
        else:
            pack.setText(f"Icon pack: {extension.unlocks}.")
        layout.addWidget(pack)
        self.icons_lock.update_for(self.context.plugins)
        self.icons_lock.storeRequested.connect(self.storeRequested)
        layout.addWidget(self.icons_lock)
        layout.addStretch(1)
        return panel

    def select_spot(self, spot_id: str) -> None:
        """A part of the drawn screen was clicked or chosen by keyboard."""
        self.device_view.select_spot(spot_id)
        if spot_id == "icons":
            locked = self._icons_extension() is None
            self.status.setText(
                "Tab icons: " + ("needs an extension (see the panel)." if locked else "yours.")
            )
            target = self.icons_lock.buttons[0] if locked else None
            if target is not None:
                target.setFocus()

    def _spots(self) -> list[Spot]:
        """The clickable parts of the drawn screen in the current view."""
        if self.view is View.MENU:
            return [
                Spot(
                    "icons",
                    "Tab icons",
                    tab_bar_rect(self.context.device.screen_px),
                    locked=self._icons_extension() is None,
                )
            ]
        return []

    def _library_mode_changed(self) -> None:
        self.library_page = 0
        self.set_view(View.LIBRARY)

    def set_library_writer(self, writer: LibraryWriter | None) -> None:
        """Allow writing the library look (a connected reader with Cover browser on)."""
        self.library_panel.set_writer(writer if self.context.library.drawer.cache else None)

    @property
    def dropped_sleep_image(self) -> QImage | None:
        """The picture dropped or chosen for the sleep screen (None: the reader's own)."""
        return self.sleep_panel.image

    def _sleep_dropped(self, image: QImage) -> None:
        self.sleep_panel.image = image
        self.set_view(View.SLEEP)
        self.sleep_panel.enable_write()

    def set_sleep_writer(self, writer: SleepWriter | None) -> None:
        """Allow putting sleep pictures on the reader (a connected PocketBook)."""
        self.sleep_panel.writer = writer
        self.sleep_panel.enable_write()

    def sleep_picture_png(self) -> bytes:
        """The chosen picture at exactly the screen size, as PNG (no metadata carried over)."""
        assert self.dropped_sleep_image is not None
        picture = fit_picture(self.context.device.screen_px, self.dropped_sleep_image)
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        # The stubs ask for bytes, PySide6 6.12 at runtime only takes str.
        picture.save(buffer, "PNG")  # type: ignore[call-overload]
        buffer.close()
        return bytes(data.data())

    def confirm_sleep_write(self) -> bool:
        """Preview before write: exactly one new file, nothing replaced."""
        width, height = self.context.device.screen_px
        answer = QMessageBox.question(
            self,
            "Put the picture on your reader",
            f"Add this picture ({width} × {height} px, PNG) as a new file to the Photo folder "
            "of your reader? Nothing on the reader is replaced or changed; kodesk takes a "
            "snapshot first.",
        )
        return answer == QMessageBox.StandardButton.Yes

    def write_sleep_picture(self) -> None:
        """Put the chosen picture on the reader, then say how to pick it there."""
        writer = self.sleep_panel.writer
        if writer is None or self.dropped_sleep_image is None or not self.confirm_sleep_write():
            return
        try:
            where = writer(self.sleep_picture_png())
        except Exception as error:  # pylint: disable=broad-exception-caught  # shown, not raised
            QMessageBox.warning(self, "Put the picture on your reader", str(error))
            return
        self.status.setText(f"Picture added to your reader: {where}")
        QMessageBox.information(
            self,
            "Picture added",
            f"The picture is on your reader as {where}.\n\nTo use it as the sleep screen, "
            "on the reader open Settings → Personalize → Logos, choose the sleep logo and pick "
            "the picture. The firmware, not KOReader, shows it.",
        )

    # ----------------------------------------------------------------- hardware keys

    def press_key(self, control_id: str) -> None:
        """Simulate a short press the way KOReader handles it on this device."""
        self.select_key(control_id)
        invert = self._value(INVERT_KEY) is True
        step = {"next": 1, "prev": -1}.get(control_id, 0) * (-1 if invert else 1)
        if self.view is View.SLEEP:
            self.set_view(self.previous_view)
            self.status.setText("Woke up from sleep.")
            return
        if step and self.view is View.BOOK:
            self.page = max(0, min(self.page + step, self._book().page_count - 1))
        elif step and self.view is View.LIBRARY:
            self.library_page = max(0, self.library_page + step)
        elif control_id == "home" and self._home_is_back():
            if self.view is View.MENU:
                self.set_view(View.BOOK)
            self.status.setText("Home acts as Back (extension on your reader).")
            return
        elif control_id == "home":
            self.set_view(View.LIBRARY)
        elif control_id == "menu":
            self.set_view(View.BOOK if self.view is View.MENU else View.MENU)
        self.refresh()

    def _home_is_back(self) -> bool:
        option = KEY_OPTIONS["Home"]
        extension = installed_for(option.area, self.context.plugins)
        if extension is None:
            return False
        value = self._value(option.path)
        return option.default if value is None else value is True

    def long_press_key(self, control_id: str) -> None:
        """Simulate holding a key (about 2 s); only Menu | On/Off reacts, in the firmware."""
        self.select_key(control_id)
        if control_id == "menu":
            self.status.setText(
                "Hold Menu | On/Off: the firmware powers the reader off "
                "(KOReader does not see this)."
            )

    def select_key(self, control_id: str) -> None:
        """Highlight a button on the drawing and show what it does and what can be changed."""
        self.device_view.select_key(control_id)
        control = next(c for c in self.context.device.controls if c.id == control_id)
        self.key_panel.show_control(control, self.context.plugins, self._value)

    # ----------------------------------------------------------------- views

    def set_view(self, view: View) -> None:
        if view is View.SLEEP and self.view is not View.SLEEP:
            self.previous_view = self.view
        self.view = view
        self.view_group.button(list(View).index(view)).setChecked(True)
        self.panels.setCurrentIndex(list(View).index(view))
        self.refresh()

    def _book(self) -> BookRenderer:
        if self._renderer is None:
            self._renderer = BookRenderer(book_layout(self.context.device, self.edited.data))
        return self._renderer

    def screen_image(self) -> QImage:
        """The current view rendered at the device's own pixel size."""
        device = self.context.device
        size = device.screen_px
        night = self._value(("night_mode",)) is True
        if self.view is View.BOOK:
            return self._book().render(self.page)
        if self.view is View.MENU:
            return render_menu(size, night, self._book().render(self.page), self.context.icons)
        if self.view is View.LIBRARY:
            entries = list(self.context.library_entries) or ["(no reader connected)"]
            look = self.library_panel.look
            shelf = Shelf(self.library_panel.mode, look.grid, look.per_page)
            return render_library(size, night, entries, self.library_page, shelf)
        if self.dropped_sleep_image is not None:
            return fit_picture(size, self.dropped_sleep_image)
        return render_sleep(size, self.context.sleep_image)

    def refresh(self) -> None:
        """Redraw the screen and the list of pending changes."""
        self.device_view.set_screen(self.screen_image())
        self.device_view.set_spots(self._spots())
        box = self.changes_box
        box.list.clear()
        box.write_button.setEnabled(box.writer is not None and bool(self.changed()))
        for path in self.changed():
            old, new = self.original.get(path), self.edited.get(path)
            name = ".".join(str(part) for part in path)
            box.list.addItem(f"{name}: {_short(old)} → {_short(new)}")
        if self.view is View.BOOK:
            book = self._book()
            self.status.setText(
                f"{self.context.device.name} · page {self.page + 1} of {book.page_count}"
            )
        elif self.view is View.LIBRARY:
            panel = self.library_panel
            self.status.setText(f"Library · {mode_label(panel.look.drawer, panel.mode)}")


def _short(value: LuaValue | None) -> str:
    if value is None:
        return "default"
    if isinstance(value, dict):
        return "{…}"
    return (
        repr(value)
        if isinstance(value, str)
        else str(value).lower()
        if isinstance(value, bool)
        else str(value)
    )
