"""Geometry of KOReader's top menu and file list, as KOReader computes it (no Qt here).

Measured from KOReader v2026.07.2 (docs/research/configurator/menu-geometry-2026-11-26.html):
frontend/ui/widget/touchmenu.lua, menu.lua, titlebar.lua, size.lua and font.lua. Every size goes
through ``scaleBySize`` (Font:getFace too). Text heights depend on the font's line metrics, so
the caller passes them in (the Qt layer measures Noto Sans).
"""

from __future__ import annotations

from dataclasses import dataclass

from kodesk.core.layout import scale_by_size

ICON = 40  # defaults.lua:30 DGENERIC_ICON_SIZE


@dataclass(frozen=True)
class TouchMenu:
    """The reader's top menu (ReaderMenu → TouchMenu), in device pixels."""

    border: int  # Size.border.window (touchmenu.lua:464), top and bottom
    padding: int  # Size.padding.default (touchmenu.lua:465), left, right and bottom
    icon: int  # tab icon size (touchmenu.lua:293)
    tab_sep: int  # Size.span.vertical_default, separators between tabs (touchmenu.lua:290)
    tab_pad: int  # icon_padding (touchmenu.lua:298)
    bar_h: int  # icon + 2 × padding (touchmenu.lua:299)
    line: int  # Size.line.thick, the line under the tabs (touchmenu.lua:341)
    item_h: int  # Size.item.height_large (touchmenu.lua:463)
    item_gap: int  # Size.line.medium, blank span between items (touchmenu.lua:467)
    item_font_px: int  # smallinfofont 22 (touchmenu.lua:51)
    footer_font_px: int  # ffont 20 (touchmenu.lua:466)
    footer_h: int  # chevron button + line (touchmenu.lua:602)
    footer_top: int  # touchmenu.lua:649
    per_page: int  # max_per_page_default (touchmenu.lua:471), if it fits

    def tab_x(self, index: int, count: int, width: int) -> int:
        """Left edge of tab ``index``; the last tab sits at the right edge (touchmenu.lua:409)."""
        cell = self.tab_pad * 2 + self.icon
        if index < count - 1:
            return self.tab_sep + index * (cell + self.tab_sep)
        return width - cell - self.tab_sep

    def height(self, items: int) -> int:
        """The menu's height with ``items`` rows on the page (touchmenu.lua:740)."""
        rows = items * self.item_h + max(0, items - 1) * self.item_gap
        return (
            self.border
            + self.bar_h
            + self.line
            + rows
            + self.footer_top
            + self.footer_h
            + (self.padding + self.border)
        )


def touch_menu(width: int, height: int) -> TouchMenu:
    """The top menu for a screen of ``width`` × ``height`` device pixels."""

    def s(px: float) -> int:
        return scale_by_size(px, width, height)

    menu = TouchMenu(
        border=s(1.5),
        padding=s(5),
        icon=s(ICON),
        tab_sep=s(2),
        tab_pad=0,
        bar_h=s(ICON) + 2 * s(5),
        line=s(1.5),
        item_h=s(50),
        item_gap=s(1),
        item_font_px=s(22),
        footer_font_px=s(20),
        footer_h=s(ICON) + s(1.5),
        footer_top=s(2),
        per_page=10,
    )
    tabs = 7  # reader_menu_order.lua: navi, typeset, setting, tools, search, filemanager, main
    content = menu.icon * tabs + menu.tab_sep * (tabs + 1)
    pad = min(int((width - content) / (2 * tabs)), s(16))
    available = height - menu.bar_h - menu.footer_top - menu.footer_h
    per_page = min(available // (menu.item_h + menu.item_gap), 10)
    return TouchMenu(**{**menu.__dict__, "tab_pad": pad, "per_page": per_page})


@dataclass(frozen=True)
class FileList:
    """The file browser in classic list mode (FileManager → FileChooser → Menu)."""

    icon: int  # title bar icons (titlebar.lua:100)
    button_pad: int  # filemanager.lua:118 button_padding
    title_top: int  # filemanager.lua title_top_padding
    title_gap: int  # titlebar.lua title_subtitle_v_padding
    title_bottom: int  # Size.padding.large without a bottom line (titlebar.lua:332)
    title_font_px: int  # smalltfont 24, bold (titlebar.lua:30)
    subtitle_font_px: int  # xx_smallinfofont 18 (titlebar.lua:38)
    per_page: int  # items_per_page_default (menu.lua:613)
    name_font_px: int  # getItemFontSize(per_page) (menu.lua:1546)
    info_font_px: int  # infont, name size - 4 (menu.lua:1113)
    side_pad: int  # Size.padding.fullscreen (menu.lua:102)
    line: int  # Size.line.medium, dark gray under every row (menu.lua:634)
    footer_h: int  # chevron button (menu.lua:760) + Size.padding.button (menu.lua:663)
    footer_font_px: int  # Button cfont 20 (button.lua:66)
    footer_spacer: int  # menu.lua:774

    def title_h(self, title_line_h: float, subtitle_line_h: float) -> int:
        return round(
            self.title_top + title_line_h + self.title_gap + subtitle_line_h + self.title_bottom
        )

    def item_h(self, screen_h: int, title_h: int) -> int:
        """menu.lua:670: the space between title bar and footer, split into per_page rows."""
        return (screen_h - title_h - self.footer_h) // self.per_page


def file_list(width: int, height: int, per_page: int = 14) -> FileList:
    """The file list for a screen of ``width`` × ``height`` (``per_page``: the user's
    "items per page", KOReader's default 14)."""

    def s(px: float) -> int:
        return scale_by_size(px, width, height)

    name_size = int(24 - ((per_page - 6) * (1 / 18)) * 10)
    return FileList(
        icon=s(ICON),
        button_pad=s(5),
        title_top=s(6),
        title_gap=s(3),
        title_bottom=s(10),
        title_font_px=s(24),
        subtitle_font_px=s(18),
        per_page=per_page,
        name_font_px=s(name_size),
        info_font_px=s(name_size - 4),
        side_pad=s(15),
        line=s(1),
        footer_h=s(ICON) + 2 * s(2) + s(2),
        footer_font_px=s(20),
        footer_spacer=s(32),
    )
