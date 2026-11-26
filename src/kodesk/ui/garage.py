"""Tuning-Garage spike (B-02): the reader in the middle, its settings around it.

Read-only in this spike: edits change an in-memory copy of settings.reader.lua and are listed as
a diff; writing to the reader waits for snapshot/restore (B-04, hard rule "snapshot before every
write").
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QImage
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QSlider,
    QSplitter,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from kodesk.core.devices import Control, DeviceModel, Face
from kodesk.core.layout import (
    DEFAULT_B_MARGIN,
    DEFAULT_H_MARGINS,
    DEFAULT_NIGHTMODE_IMAGES,
    DEFAULT_T_MARGIN,
    book_layout,
    scale_by_size,
)
from kodesk.core.luadump import KeyPath, LuaValue, SettingsDocument, changed_paths
from kodesk.ui.device_view import DeviceView
from kodesk.ui.screen import BookRenderer, render_library, render_menu, render_sleep


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
class KeyUnlock:
    """A plugin that makes a locked hardware key configurable, and the setting it reads."""

    plugin: str  # plugins/<plugin>.koplugin
    label: str
    path: KeyPath
    default: bool  # the plugin's value while the key is absent


# pbfixes.koplugin 0.4.1 (github.com/glaucon1984/pbfixes.koplugin, main.lua):
# HOME_SETTING = "pbfixes_home_back", read with G_reader_settings:nilOrTrue (absent = on).
KEY_UNLOCKS = {
    "Home": KeyUnlock(
        "pbfixes", "Home button acts as Back (pbfixes plugin)", ("pbfixes_home_back",), True
    ),
}


def key_description(control: Control, plugins: tuple[str, ...] = ()) -> str:
    """What KOReader does with a hardware control on this device, and what can be changed."""
    if control.koreader_key in PAGE_KEYS:
        return (
            "KOReader can swap the two page-turn buttons. Other actions cannot be "
            "assigned to them in KOReader on this device."
        )
    if control.koreader_key == "Home":
        if "pbfixes" in plugins:
            return (
                "In KOReader: opens the library (file browser). <b>Unlocked</b> by the "
                "pbfixes plugin installed on your reader: with the option on, Home acts as "
                "Back instead (closes menus, goes up a folder)."
            )
        return (
            "In KOReader: opens the library (file browser). <b>Locked:</b> KOReader has no "
            "setting for this button on PocketBook (its Hotkeys plugin needs a keyboard). "
            "The plugin <i>pbfixes</i> can make Home act as Back; once it is installed on "
            "the reader, kodesk unlocks this button here. The reader's own settings must "
            "leave a single press of Home to KOReader."
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
        self.unlock = QCheckBox(self)  # the option a plugin adds to a locked key
        self.unlock.setObjectName("key-unlock")
        self.unlock.setVisible(False)
        layout.addWidget(self.unlock)
        self.current_unlock: KeyUnlock | None = None

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
        unlock = KEY_UNLOCKS.get(control.koreader_key or "")
        self.current_unlock = unlock if unlock and unlock.plugin in plugins else None
        self.unlock.setVisible(self.current_unlock is not None)
        if self.current_unlock is not None:
            value = value_of(self.current_unlock.path)
            self.unlock.blockSignals(True)  # showing the state is not a change
            self.unlock.setText(self.current_unlock.label)
            self.unlock.setChecked(self.current_unlock.default if value is None else value is True)
            self.unlock.blockSignals(False)


class TuningGarage(QWidget):
    """The Tuning-Garage: the drawn reader in the middle, its settings around it."""

    settingsChanged = Signal()

    def __init__(self, context: ReaderContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.original = context.settings
        self.edited = context.settings
        self.view = View.BOOK
        self.previous_view = View.BOOK
        self.page = 0
        self.library_page = 0
        self.dropped_sleep_image: QImage | None = None
        self._renderer: BookRenderer | None = None

        self.device_view = DeviceView(context.device, self)
        self.device_view.keyPressed.connect(self.press_key)
        self.device_view.keyLongPressed.connect(self.long_press_key)
        self.status = QLabel(self)
        self.status.setObjectName("garage-status")
        self.status.setWordWrap(True)
        view_switch, self.view_group = _segmented([v.value for v in View], self)
        self.view_group.idClicked.connect(lambda index: self.set_view(list(View)[index]))

        self.panels = QStackedWidget(self)
        self.key_panel = KeyPanel(self)
        self.key_panel.invert.setChecked(self._value(INVERT_KEY) is True)
        self.key_panel.invert.toggled.connect(self._set_invert)
        self.key_panel.unlock.toggled.connect(self._set_unlocked_option)
        self.changes = QListWidget(self)
        self.changes.setObjectName("changes")

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
        self.panels.addWidget(self._book_panel())
        self.panels.addWidget(
            self._info_panel(
                "KOReader's main menu over the page. Simplified drawing: tab and item sizes "
                "are measured in a later step (B-13)."
            )
        )
        self.panels.addWidget(self._library_panel())
        self.panels.addWidget(self._sleep_panel())
        write_button = QPushButton("Write to reader", self)
        write_button.setEnabled(False)
        write_button.setToolTip(
            "Writing comes with snapshot and one-click restore (B-04). This spike only previews."
        )
        changes_box = QGroupBox("Changed settings (not written yet)", self)
        changes_layout = QVBoxLayout(changes_box)
        changes_layout.addWidget(self.changes)
        changes_layout.addWidget(write_button)
        right = QWidget(self)
        right_layout = QVBoxLayout(right)
        right_layout.addWidget(self.panels, 1)
        right_layout.addWidget(self.key_panel)
        right_layout.addWidget(changes_box)
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

    def _set_invert(self, on: bool) -> None:
        self._set_or_default(INVERT_KEY, on, False)

    def _set_unlocked_option(self, on: bool) -> None:
        unlock = self.key_panel.current_unlock
        if unlock is not None:
            self._set_or_default(unlock.path, on, unlock.default)

    def changed(self) -> list[KeyPath]:
        return changed_paths(self.original.data, self.edited.data)

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
        header = QCheckBox("Alternative status bar at the top", self)
        header.setObjectName("alt-status-bar")
        header.setChecked(layout.header is not None)
        header.toggled.connect(lambda on: self._set(("copt_status_line",), 0 if on else 1))
        form.addRow(header)
        self._slider(
            form,
            SliderSpec(
                "header-font-size",
                "Top bar font size",
                ("cre_header_status_font_size",),
                8,
                40,
                layout.header_font_px,
            ),
        )
        return panel

    def _info_panel(self, text: str) -> QWidget:
        label = QLabel(text, self)
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignmentFlag.AlignTop)
        return label

    def _library_panel(self) -> QWidget:
        text = (
            "Your book folder as KOReader lists it (classic list). Simplified drawing; "
            "mosaic view and the Project: Title look follow in B-14."
        )
        return self._info_panel(text)

    def _sleep_panel(self) -> QWidget:
        width, height = self.context.device.screen_px
        panel = SleepDropPanel(width, height, self)
        panel.imageDropped.connect(self._sleep_dropped)
        return panel

    def _sleep_dropped(self, image: QImage) -> None:
        self.dropped_sleep_image = image
        self.refresh()

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
            self.status.setText("Home acts as Back (pbfixes plugin).")
            return
        elif control_id == "home":
            self.set_view(View.LIBRARY)
        elif control_id == "menu":
            self.set_view(View.BOOK if self.view is View.MENU else View.MENU)
        self.refresh()

    def _home_is_back(self) -> bool:
        unlock = KEY_UNLOCKS["Home"]
        if unlock.plugin not in self.context.plugins:
            return False
        value = self._value(unlock.path)
        return unlock.default if value is None else value is True

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
        scale = scale_by_size(1, *size)
        night = self._value(("night_mode",)) is True
        if self.view is View.BOOK:
            return self._book().render(self.page)
        if self.view is View.MENU:
            return render_menu(size, night, scale)
        if self.view is View.LIBRARY:
            entries = list(self.context.library_entries) or ["(no reader connected)"]
            return render_library(size, night, scale, entries, self.library_page)
        if self.dropped_sleep_image is not None:
            return self.dropped_sleep_image.scaled(
                size[0],
                size[1],
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        return render_sleep(size, self.context.sleep_image)

    def refresh(self) -> None:
        """Redraw the screen and the list of pending changes."""
        self.device_view.set_screen(self.screen_image())
        self.changes.clear()
        for path in self.changed():
            old, new = self.original.get(path), self.edited.get(path)
            name = ".".join(str(part) for part in path)
            self.changes.addItem(f"{name}: {_short(old)} → {_short(new)}")
        if self.view is View.BOOK:
            book = self._book()
            self.status.setText(
                f"{self.context.device.name} · page {self.page + 1} of {book.page_count}"
            )


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


class SleepDropPanel(QGroupBox):
    """Drop zone for a sleep-screen picture, stating the exact target size."""

    imageDropped = Signal(QImage)

    def __init__(self, width: int, height: int, parent: QWidget | None = None) -> None:
        super().__init__("Sleep screen", parent)
        self.setAcceptDrops(True)
        layout = QVBoxLayout(self)
        self.hint = QLabel(
            f"<p>Drop a picture here.</p><p><b>Best size: {width} × {height} px</b> "
            "(portrait). Other sizes are scaled to fit.</p>"
            "<p>On PocketBook the firmware draws the sleep screen, not KOReader: kodesk will "
            "place the picture in the reader's <code>Photo</code> folder; you then choose it "
            "on the reader under Settings → Personalize → Logos.</p>",
            self,
        )
        self.hint.setWordWrap(True)
        self.hint.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.hint)
        layout.addStretch(1)

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
