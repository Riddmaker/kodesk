"""KOReader's two status bars as data (core.statusbar, B-13): readerfooter.lua semantics."""

from __future__ import annotations

import pytest

from kodesk.core.luadump import LuaTable
from kodesk.core.statusbar import (
    FOOTER_DEFAULTS,
    Sample,
    footer,
    footer_text,
    footer_value,
    footer_with,
    header_items,
)

SAMPLE = Sample(page=12, pages=240, chapter_pages_left=5, title="T", author="A", chapter="C")


def test_without_a_table_koreader_uses_all_defaults() -> None:
    assert footer_value({}, "page_progress") is True
    assert footer_value({}, "book_title") is False
    status = footer({})
    assert status is not None and status.items == ("page_progress",)  # one at a time
    assert status.progress_bar and status.prefix == "icons" and status.separator == "bar"


def test_with_a_table_a_missing_key_is_off_not_its_default() -> None:
    settings: LuaTable = {"footer": {"time": True}}
    assert footer_value(settings, "page_progress") is None
    assert footer_value(settings, "time") is True
    status = footer(settings)
    assert status is not None and status.items == ("time",)


def test_all_at_once_lists_items_in_koreader_order() -> None:
    status = footer({"footer": {**FOOTER_DEFAULTS, "all_at_once": True}})
    assert status is not None
    shown = "page_progress time pages_left battery percentage book_time_to_read"
    assert status.items == (*shown.split(), "chapter_time_to_read")
    assert footer_text(status, SAMPLE) == (
        "12 / 240 | ⌚ 19:26 | ⇒ 5 | ▭87% | ⤠ 5% | ⏳ 2:05 | ⤻ 0:12"
    )


def test_saved_order_and_mode_pick_the_single_item() -> None:
    table: LuaTable = {"time": True, "battery": True, "order": {1: "battery", 2: "time"}}
    status = footer({"footer": table, "reader_footer_mode": 2})
    assert status is not None and status.items == ("time",)


@pytest.mark.parametrize(
    ("prefix", "separator", "expected"),
    [
        ("letters", "bullet", "B: 87% • R: 5%"),
        ("compact_items", "none", "▭ 5%"),
        ("icons", "dot", "▭87% · ⤠ 5%"),
    ],
)
def test_prefixes_and_separators(prefix: str, separator: str, expected: str) -> None:
    table: LuaTable = {
        "all_at_once": True,
        "percentage": True,
        "battery": True,
        "item_prefix": prefix,
        "items_separator": separator,
    }
    status = footer({"footer": table})
    assert status is not None and footer_text(status, SAMPLE) == expected


def test_hidden_when_off_disabled_or_empty() -> None:
    assert footer({"reader_footer_mode": 0}) is None
    assert footer({"footer": {"disabled": True}}) is None
    assert footer({"footer": {"disable_progress_bar": True}}) is None  # nothing left to show
    only_bar = footer({"footer": {}})
    assert only_bar is not None and not only_bar.items and only_bar.progress_bar


def test_footer_with_writes_the_default_table_once() -> None:
    table = footer_with({}, "book_title", True)
    assert table == {**FOOTER_DEFAULTS, "book_title": True}
    assert footer_with({"footer": table}, "book_title", False) is None  # defaults again
    own: LuaTable = {"time": True}
    assert footer_with({"footer": own}, "battery", True) == {"time": True, "battery": True}


def test_header_items_have_their_own_defaults() -> None:
    assert header_items({}) == (
        "title",
        "author",
        "clock",
        "page_number",
        "page_count",
        "battery",
        "chapter_marks",
    )
    shown = header_items({"cre_header_reading_percent": 1, "cre_header_title": 0})
    assert "reading_percent" in shown and "title" not in shown
