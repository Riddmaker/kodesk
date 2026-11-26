"""Book-view geometry must follow KOReader's own formulas (core.layout)."""

from __future__ import annotations

from kodesk.core.devices import DEVICES
from kodesk.core.layout import book_layout, scale_by_size
from tests.conftest import OWNER_LIKE_SETTINGS

VERSE = DEVICES["pocketbook-verse-pro-color"]


def test_scale_by_size_matches_framebuffer_lua() -> None:
    # min(1072, 1448) / 600 = 1.7867; ceil(22 * 1.7867) = 40
    assert scale_by_size(22, 1072, 1448) == 40
    assert scale_by_size(10, 1072, 1448) == 18
    assert scale_by_size(1, 1072, 1448) == 2


def test_owner_like_layout() -> None:
    layout = book_layout(VERSE, OWNER_LIKE_SETTINGS)  # type: ignore[arg-type]
    assert layout.font_px == 40
    assert layout.night_mode is True
    assert layout.header is not None and layout.header.h == round(28 * 1.362) + 4
    assert layout.footer is not None and layout.footer.h == 26 + 2
    left, top, right, bottom = layout.margins_px
    assert (left, top, right) == (18, 18, 18)
    assert bottom == 9 + layout.footer.h
    assert layout.text.y == layout.header.h + top
    assert layout.text.x + layout.text.w + right == 1072


def test_defaults_when_keys_are_missing() -> None:
    layout = book_layout(VERSE, {})
    assert layout.font_px == 40
    assert layout.header is None
    assert layout.margins_px[:3] == (18, 27, 18)
    assert layout.night_mode is False


def test_reclaim_height_and_footer_off() -> None:
    reclaim = book_layout(VERSE, {"footer": {"reclaim_height": True}})
    assert reclaim.margins_px[3] == scale_by_size(15, 1072, 1448)
    off = book_layout(VERSE, {"reader_footer_mode": 0})
    assert off.footer is None


def test_night_pictures_keep_original_colours_by_default() -> None:
    assert book_layout(VERSE, {}).night_pictures_original is True
    assert book_layout(VERSE, {"copt_nightmode_images": 0}).night_pictures_original is False
