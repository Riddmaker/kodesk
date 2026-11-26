"""KOReader's top menu and file list geometry (core.menus), pinned to the measured values
for the Verse Pro Color (1072 × 1448): docs/research/configurator/menu-geometry-2026-11-26.html."""

from __future__ import annotations

from kodesk.core.menus import file_list, touch_menu


def test_touch_menu_matches_touchmenu_lua() -> None:
    menu = touch_menu(1072, 1448)
    assert (menu.icon, menu.bar_h, menu.item_h, menu.item_gap) == (72, 90, 90, 2)
    assert (menu.item_font_px, menu.footer_font_px, menu.footer_h) == (40, 36, 75)
    assert menu.tab_pad == 29 and menu.per_page == 10
    assert menu.tab_x(0, 7, 1072) == 4
    assert menu.tab_x(6, 7, 1072) + 2 * menu.tab_pad + menu.icon + menu.tab_sep == 1072
    # 3 + 90 + 3 + 10 rows (9 gaps) + 4 + 75 + 9 + 3
    assert menu.height(10) == 3 + 90 + 3 + 10 * 90 + 9 * 2 + 4 + 75 + 9 + 3


def test_file_list_matches_menu_lua() -> None:
    files = file_list(1072, 1448)
    assert (files.per_page, files.name_font_px, files.info_font_px) == (14, 34, 27)
    assert (files.side_pad, files.line, files.footer_h, files.icon) == (27, 2, 84, 72)
    title_h = files.title_h(59, 45)  # Noto Sans line heights at 43 and 33 px (about 1.36 em)
    assert title_h == 11 + 59 + 6 + 45 + 18
    assert files.item_h(1448, title_h) == (1448 - title_h - 84) // 14


def test_fewer_items_per_page_get_bigger_names() -> None:
    assert file_list(1072, 1448, per_page=6).name_font_px == 43  # 24 → scaleBySize
