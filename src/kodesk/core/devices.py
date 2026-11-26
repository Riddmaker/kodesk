"""Physical device definitions for the mockup: body, screen, and every hardware control.

Sources are named per field (``source``) — never guessed silently. Screen pixels and DPI come
from KOReader's device code for the installed version; body and control geometry from the
manufacturer's manual and product photos (docs/research/device-hardware/).

Coordinates are millimetres on a *face* of the device. Each face is seen from outside:
``front`` and ``back`` in portrait, ``bottom`` as the bottom edge seen from below with the
front facing up (x from left to right as on the front).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Face(StrEnum):
    FRONT = "front"
    BOTTOM = "bottom"
    LEFT = "left"
    RIGHT = "right"
    TOP = "top"
    BACK = "back"


@dataclass(frozen=True)
class Rect:
    """Axis-aligned rectangle in millimetres on a face."""

    x: float
    y: float
    w: float
    h: float

    def contains(self, px: float, py: float) -> bool:
        return self.x <= px <= self.x + self.w and self.y <= py <= self.y + self.h


@dataclass(frozen=True)
class Control:
    """A physical button (or port / LED, which are drawn but not pressable)."""

    id: str
    label: str
    face: Face
    rect: Rect
    glyph: str  # "home", "prev", "next", "power", "usb", "led" — drawn by the UI
    koreader_key: str | None  # name in KOReader's input event map, None if KOReader never sees it
    firmware_role: str  # what the device firmware does with it, from the manual
    pressable: bool = True


@dataclass(frozen=True)
class DeviceModel:
    """One reader model: body, screen and hardware controls, in millimetres (portrait)."""

    id: str
    name: str
    koreader_models: tuple[str, ...]  # Device.model values in KOReader (version.log last field)
    screen_px: tuple[int, int]  # portrait width, height
    dpi: int
    color: bool
    body_mm: tuple[float, float, float]  # width, height, depth
    screen_rect: Rect  # active area on the front face
    corner_radius_mm: float
    controls: tuple[Control, ...]
    verified: bool  # tested on a real device (DESIGN rule 4)
    sources: tuple[str, ...] = field(default=())

    def controls_on(self, face: Face) -> list[Control]:
        return [c for c in self.controls if c.face is face]

    @property
    def faces(self) -> list[Face]:
        """Faces worth showing: the front plus every face that carries a control."""
        used = {c.face for c in self.controls}
        return [Face.FRONT, *(f for f in Face if f is not Face.FRONT and f in used)]

    @property
    def screen_mm(self) -> tuple[float, float]:
        return self.screen_px[0] / self.dpi * 25.4, self.screen_px[1] / self.dpi * 25.4


def _verse_pro_color() -> DeviceModel:
    width, height = 108.0, 156.0
    screen_w, screen_h = 1072 / 300 * 25.4, 1448 / 300 * 25.4
    left = (width - screen_w) / 2
    top = 9.0  # photo estimate (research/device-hardware)
    key_y, key_h = height - 4.6, 4.0  # flat key strip flush with the bottom of the chin
    return DeviceModel(
        id="pocketbook-verse-pro-color",
        name="PocketBook Verse Pro Color",
        koreader_models=("PBVerseProColor",),
        screen_px=(1072, 1448),
        dpi=300,
        color=True,
        body_mm=(width, height, 7.6),
        screen_rect=Rect(left, top, screen_w, screen_h),
        corner_radius_mm=6.5,
        controls=(
            Control(
                "home",
                "Home",
                Face.FRONT,
                Rect(8.8, key_y, 19.3, key_h),
                "home",
                "Home",
                "Return to the main menu",
            ),
            Control(
                "prev",
                "Backward",
                Face.FRONT,
                Rect(28.3, key_y, 25.6, key_h),
                "prev",
                "LPgBack",
                "Page back; previous menu or list page",
            ),
            Control(
                "next",
                "Forward",
                Face.FRONT,
                Rect(54.1, key_y, 25.4, key_h),
                "next",
                "LPgFwd",
                "Page forward",
            ),
            Control(
                "menu",
                "Menu | On/Off",
                Face.FRONT,
                Rect(79.7, key_y, 19.5, key_h),
                "power",
                "Menu",
                "Short press: context menu; hold ~2 s: power on/off",
            ),
            Control(
                "usb",
                "USB-C",
                Face.BOTTOM,
                Rect(width / 2 - 4.5, 2.6, 7.0, 2.4),
                "usb",
                None,
                "Charging and USB connection to the computer",
                pressable=False,
            ),
            Control(
                "led",
                "LED",
                Face.BOTTOM,
                Rect(width / 2 + 5.0, 3.3, 1.0, 1.0),
                "led",
                None,
                "Charge / activity indicator",
                pressable=False,
            ),
        ),
        verified=True,
        sources=(
            "KOReader v2026.07.2 frontend/device/pocketbook/device.lua (PocketBook634K3, "
            "display_dpi = 300; event_map Home/Menu/LPgBack/LPgFwd)",
            "PocketBook User Manual Verse Pro Color, p. 12 'Appearance' (control order/roles)",
            "Body 108 × 156 × 7.6 mm: manufacturer spec; bezels/key x from product photo "
            "(estimate, research/device-hardware 2026-11-26)",
        ),
    )


DEVICES: dict[str, DeviceModel] = {d.id: d for d in (_verse_pro_color(),)}


def find_by_koreader_model(model: str) -> DeviceModel | None:
    """Match the model name KOReader writes into version.log."""
    for device in DEVICES.values():
        if model in device.koreader_models:
            return device
    return None
