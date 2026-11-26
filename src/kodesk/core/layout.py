"""Book-view geometry, computed the way KOReader computes it (no Qt here).

Every number carries the KOReader source it was taken from (DESIGN fidelity rule, D-15). The
references were read from KOReader v2026.07.2 as installed on the reference device and from
koreader/crengine ``lvdocview.cpp``. When a different KOReader version is installed, re-check
these lines before trusting the mockup (tests/test_layout.py pins the formulas).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from kodesk.core.devices import DeviceModel
from kodesk.core.luadump import LuaTable, LuaValue

# frontend/ui/data/creoptions.lua + defaults.lua: defaults used when a key is absent.
DEFAULT_FONT_SIZE = 22  # DCREREADER_CONFIG_DEFAULT_FONT_SIZE
DEFAULT_H_MARGINS = (
    10,
    10,
)  # h_page_margins default_pos 2 = DCREREADER_CONFIG_H_MARGIN_SIZES_MEDIUM
DEFAULT_T_MARGIN = 15  # t_page_margin default_pos 3 = DCREREADER_CONFIG_T_MARGIN_SIZES_LARGE
DEFAULT_B_MARGIN = 15  # b_page_margin default_pos 3 = DCREREADER_CONFIG_B_MARGIN_SIZES_LARGE
DEFAULT_LINE_SPACING = 100  # DCREREADER_CONFIG_LINE_SPACE_PERCENT_MEDIUM
DEFAULT_STATUS_LINE = 1  # creoptions status_line: 0 = crengine top bar on, 1 = off
DEFAULT_HEADER_FONT_SIZE = 20  # readercoptlistener.lua CRE_HEADER_DEFAULT_SIZE (raw px)
DEFAULT_FOOTER_HEIGHT = 14  # defaults.lua DMINIBAR_CONTAINER_HEIGHT
DEFAULT_FOOTER_PADDING = 1  # readerfooter.lua container_bottom_padding
DEFAULT_FONT_FACE = "Noto Serif"  # cre_font fallback shipped in koreader/fonts/noto
# creoptions.lua nightmode_images default_value = 1. The name reads backwards: 1 makes crengine
# pre-invert pictures (credocument.lua drawCurrentView), so after night mode inverts the whole
# screen they show in their original colours; 0 leaves them as negatives like the text.
DEFAULT_NIGHTMODE_IMAGES = 1
HEADER_MARGIN = 4  # crengine lvdocview.cpp #define HEADER_MARGIN
FONT_SIZES = (12, 16, 20, 22, 24, 26, 28, 30, 34, 38, 44)  # DCREREADER_CONFIG_FONT_SIZES

# crengine header items (readercoptlistener.lua, cre_header_* keys; 1 = shown).
HEADER_ITEMS = (
    "title",
    "author",
    "clock",
    "page_number",
    "page_count",
    "reading_percent",
    "battery",
    "battery_percent",
    "chapter_marks",
)


def scale_by_size(px: float, screen_w: int, screen_h: int) -> int:
    """ffi/framebuffer.lua ``fb:scaleBySize`` without a DPI override.

    ``size_scale = min(w, h) / 600``; ``dpi_scale = size_scale``;
    ``ceil(px * (size_scale + dpi_scale) / 2)``.
    """
    size_scale = min(screen_w, screen_h) / 600
    return math.ceil(px * size_scale)


def _number(settings: LuaTable, key: str, default: float) -> float:
    value = settings.get(key)
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def _pair(value: LuaValue | None, default: tuple[int, int]) -> tuple[float, float]:
    if isinstance(value, dict):
        left, right = value.get(1), value.get(2)
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            return float(left), float(right)
    return float(default[0]), float(default[1])


@dataclass(frozen=True)
class Box:
    """A rectangle in device pixels."""

    x: int
    y: int
    w: int
    h: int


@dataclass(frozen=True)
class BookLayout:
    """Where things sit on the device screen, in device pixels (portrait)."""

    screen_w: int
    screen_h: int
    font_face: str
    font_size: float  # the user-facing value (copt_font_size)
    font_px: int  # what crengine gets: scaleBySize(font_size)
    line_spacing_pct: int
    margins_px: tuple[int, int, int, int]  # left, top, right, bottom (bottom incl. footer)
    header: Box | None  # crengine alt status bar
    header_font_px: int
    header_items: tuple[str, ...]
    footer: Box | None  # KOReader status bar (mini bar)
    text: Box
    night_mode: bool
    night_pictures_original: bool  # copt_nightmode_images == 1, see DEFAULT_NIGHTMODE_IMAGES


def _header(settings: LuaTable, width: int) -> tuple[Box | None, int, tuple[str, ...]]:
    """crengine's alt status bar (top): its box, font px and the items shown."""
    font_px = int(_number(settings, "cre_header_status_font_size", DEFAULT_HEADER_FONT_SIZE))
    if int(_number(settings, "copt_status_line", DEFAULT_STATUS_LINE)) != 0:
        return None, font_px, ()
    # lvdocview.cpp getPageHeaderHeight(): font height + HEADER_MARGIN. The font height of
    # Noto Sans is ~1.362 em; battery icon height is not modelled (estimate).
    box = Box(0, 0, width, round(font_px * 1.362) + HEADER_MARGIN)
    items = tuple(item for item in HEADER_ITEMS if _number(settings, f"cre_header_{item}", 1) == 1)
    return box, font_px, items


def _footer(settings: LuaTable, width: int, height: int) -> tuple[Box | None, bool]:
    """KOReader's own status bar (bottom) and whether it reclaims its height from the text."""
    footer_settings = settings.get("footer")
    footer_settings = footer_settings if isinstance(footer_settings, dict) else {}
    reclaim = footer_settings.get("reclaim_height") is True
    footer_mode = int(_number(settings, "reader_footer_mode", 1))
    if footer_mode == 0 or footer_settings.get("disabled") is True:
        return None, reclaim
    # readerfooter.lua: height = scaleBySize(container_height) + scaleBySize(bottom_padding)
    footer_h = scale_by_size(
        _number(footer_settings, "container_height", DEFAULT_FOOTER_HEIGHT), width, height
    ) + scale_by_size(
        _number(footer_settings, "container_bottom_padding", DEFAULT_FOOTER_PADDING), width, height
    )
    return Box(0, height - footer_h, width, footer_h), reclaim


def book_layout(device: DeviceModel, settings: LuaTable) -> BookLayout:
    """Compute the book-open view from global settings (copt_* = defaults for EPUB/CRE)."""
    width, height = device.screen_px

    def scale(px: float) -> int:
        return scale_by_size(px, width, height)

    font_size = _number(settings, "copt_font_size", DEFAULT_FONT_SIZE)
    h_left, h_right = _pair(settings.get("copt_h_page_margins"), DEFAULT_H_MARGINS)
    header, header_font_px, items = _header(settings, width)
    footer, reclaim = _footer(settings, width, height)

    # readertypeset.lua onSetPageMargins: scaleBySize each margin; bottom adds the footer
    # height unless footer.reclaim_height; crengine adds the header above the top margin.
    left, right = scale(h_left), scale(h_right)
    top = scale(_number(settings, "copt_t_page_margin", DEFAULT_T_MARGIN))
    bottom = scale(_number(settings, "copt_b_page_margin", DEFAULT_B_MARGIN))
    if footer is not None and not reclaim:
        bottom += footer.h
    header_h = header.h if header else 0
    text = Box(left, header_h + top, width - left - right, height - header_h - top - bottom)

    face = settings.get("cre_font")
    return BookLayout(
        screen_w=width,
        screen_h=height,
        font_face=face if isinstance(face, str) else DEFAULT_FONT_FACE,
        font_size=font_size,
        font_px=scale(font_size),
        line_spacing_pct=int(_number(settings, "copt_line_spacing", DEFAULT_LINE_SPACING)),
        margins_px=(left, top, right, bottom),
        header=header,
        header_font_px=header_font_px,
        header_items=items,
        footer=footer,
        text=text,
        night_mode=settings.get("night_mode") is True,
        night_pictures_original=int(
            _number(settings, "copt_nightmode_images", DEFAULT_NIGHTMODE_IMAGES)
        )
        == 1,
    )
