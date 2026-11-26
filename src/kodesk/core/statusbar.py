"""KOReader's two status bars as data (B-13); no Qt here.

* The **status bar** at the bottom (KOReader's own "footer", ``readerfooter.lua``): one table
  ``footer`` in settings.reader.lua plus ``reader_footer_mode``.
* The **alternative status bar** at the top (crengine's header, ``readercoptlistener.lua``):
  flat keys ``cre_header_*`` and ``copt_status_line``.

Sources: KOReader v2026.07.2, frontend/apps/reader/modules/readerfooter.lua and
readercoptlistener.lua (line numbers in the comments).

**How KOReader reads the footer table** (readerfooter.lua:553):
``G_reader_settings:readSetting("footer", ReaderFooter.default_settings)``. Without a ``footer``
table the *whole* default table is used; with one, a key that is missing in it is ``nil``
(= off), not its default. So kodesk changes one key inside an existing table (one changed line)
and, on a reader that has no table yet, writes the default table with the change in it — the
same table KOReader itself saves the first time it opens a book. Going back to the defaults on
such a reader removes the table again (nothing written).
"""

from __future__ import annotations

from dataclasses import dataclass

from kodesk.core.luadump import LuaTable, LuaValue

FOOTER_KEY = "footer"
FOOTER_MODE_KEY = "reader_footer_mode"

# readerfooter.lua:497 ReaderFooter.default_settings, the keys kodesk shows or draws (the values
# KOReader computes per device are given for e-ink readers with a battery and no Android).
FOOTER_DEFAULTS: LuaTable = {
    "disable_progress_bar": False,
    "chapter_progress_bar": False,
    "disabled": False,
    "all_at_once": False,
    "reclaim_height": False,
    "toc_markers": True,
    "page_progress": True,
    "pages_left_book": False,
    "time": True,
    "pages_left": True,
    "battery": True,  # Device:hasBattery()
    "battery_hide_threshold": 100,  # 200 only with an aux battery
    "percentage": True,
    "book_time_to_read": True,
    "chapter_time_to_read": True,
    "frontlight": False,
    "mem_usage": False,
    "wifi_status": False,
    "page_turning_inverted": False,
    "book_author": False,
    "book_title": False,
    "book_chapter": False,
    "bookmark_count": False,
    "chapter_progress": False,
    "item_prefix": "icons",
    "toc_markers_width": 2,
    "text_font_size": 14,
    "text_font_bold": False,
    "text_font_face": "./fonts/noto/NotoSans-Regular.ttf",
    "container_height": 14,  # G_defaults DMINIBAR_CONTAINER_HEIGHT
    "container_bottom_padding": 1,
    "progress_margin_width": 10,  # Android: scaleByDPI(16)
    "progress_margin": False,
    "progress_bar_min_width_pct": 20,
    "book_author_max_width_pct": 30,
    "book_title_max_width_pct": 30,
    "book_chapter_max_width_pct": 30,
    "skim_widget_on_hold": False,
    "progress_style_thin": False,
    "progress_bar_position": "alongside",
    "bottom_horizontal_separator": False,
    "align": "center",
    "auto_refresh_time": False,
    "progress_style_thin_height": 3,
    "progress_style_thick_height": 7,
    "hide_empty_generators": False,
    "lock_tap": False,
    "items_separator": "bar",
    "progress_pct_format": "0",
    "pages_left_includes_current_page": False,
    "initial_marker": False,
    "invert_progress_direction": False,
}

# readerfooter.lua:35 MODE, in its order (the order items appear in, without a saved ``order``).
FOOTER_MODES = (
    "off",
    "page_progress",
    "pages_left_book",
    "time",
    "pages_left",
    "battery",
    "percentage",
    "book_time_to_read",
    "chapter_time_to_read",
    "frontlight",
    "mem_usage",
    "wifi_status",
    "book_title",
    "book_chapter",
    "bookmark_count",
    "chapter_progress",
    "frontlight_warmth",
    "custom_text",
    "book_author",
    "page_turning_inverted",
    "dynamic_filler",
    "additional_content",
)
DEFAULT_FOOTER_MODE = 1  # readerfooter.lua:484 mode = MODE.page_progress

# The items kodesk offers, with KOReader's own menu wording (readerfooter.lua textOptionTitles).
FOOTER_ITEMS: tuple[tuple[str, str], ...] = (
    ("page_progress", "Current page"),
    ("pages_left", "Pages left in chapter"),
    ("pages_left_book", "Pages left in book"),
    ("percentage", "Progress percentage"),
    ("time", "Current time"),
    ("battery", "Battery percentage"),
    ("chapter_time_to_read", "Time left to finish chapter"),
    ("book_time_to_read", "Time left to finish book"),
    ("book_title", "Book title"),
    ("book_author", "Book author"),
    ("book_chapter", "Chapter title"),
)
ITEM_PREFIXES = (("icons", "Icons"), ("letters", "Letters"), ("compact_items", "Compact"))
SEPARATORS = (
    ("bar", "Vertical line |"),
    ("bullet", "Bullet •"),
    ("dot", "Dot ·"),
    ("none", "None"),
)
ALIGNMENTS = (("left", "Left"), ("center", "Center"), ("right", "Right"))
BAR_POSITIONS = (("alongside", "Alongside the items"), ("above", "Above"), ("below", "Below"))
FONT_SIZE_RANGE = (8, 36)  # readerfooter.lua:1595

# readerfooter.lua:60 symbol_prefix (left-to-right layout). The battery icon is a Nerd Font
# glyph chosen by charge level (powerd:getBatterySymbol); kodesk draws a plain battery sign.
BATTERY_SIGN = "▭"  # ▭, an estimate of KOReader's icon
SYMBOLS: dict[str, dict[str, str | None]] = {
    "letters": {
        "time": None,
        "pages_left_book": "->",
        "pages_left": "=>",
        "battery": "B:",
        "percentage": "R:",
        "book_time_to_read": "TB:",
        "chapter_time_to_read": "TC:",
    },
    "icons": {
        "time": "⌚",
        "pages_left_book": "↣",
        "pages_left": "⇒",
        "battery": BATTERY_SIGN,
        "percentage": "⤠",
        "book_time_to_read": "⏳",
        "chapter_time_to_read": "⤻",
    },
    "compact_items": {
        "time": None,
        "pages_left_book": "\u203a",
        "pages_left": "\u203a",
        "battery": BATTERY_SIGN,
        "percentage": None,
        "book_time_to_read": None,
        "chapter_time_to_read": "»",
    },
}

# readercoptlistener.lua:28-37: the top bar's items and their defaults (1 = shown).
HEADER_ITEMS: tuple[tuple[str, str, int], ...] = (
    ("title", "Book title", 1),
    ("author", "Book author", 1),
    ("clock", "Current time", 1),
    ("page_number", "Page number", 1),
    ("page_count", "Page count", 1),
    ("reading_percent", "Progress percentage", 0),
    ("battery", "Battery status", 1),
    ("battery_percent", "Battery percentage", 0),
    ("chapter_marks", "Chapter marks", 1),
)


def header_key(item: str) -> str:
    return f"cre_header_{item}"


def footer_table(settings: LuaTable) -> LuaTable | None:
    """The reader's own ``footer`` table, or None while KOReader has not saved one yet."""
    table = settings.get(FOOTER_KEY)
    return table if isinstance(table, dict) else None


def footer_value(settings: LuaTable, key: str) -> LuaValue | None:
    """A footer setting as KOReader sees it: from the table if there is one (missing = nil),
    else from the defaults."""
    table = footer_table(settings)
    return table.get(key) if table is not None else FOOTER_DEFAULTS.get(key)


def footer_with(settings: LuaTable, key: str, value: LuaValue) -> LuaTable | None:
    """The whole ``footer`` table to write on a reader that had none: the defaults (or the
    table edited so far) with one more change. None when that is just the defaults again:
    then the reader keeps having no table, nothing is written."""
    current = footer_table(settings)
    table: LuaTable = {}
    table.update(FOOTER_DEFAULTS if current is None else current)
    table[key] = value
    return None if table == FOOTER_DEFAULTS else table


@dataclass(frozen=True)
class Footer:
    """What the bottom status bar shows."""

    items: tuple[str, ...]  # shown items, in KOReader's order
    progress_bar: bool
    bar_position: str  # alongside / above / below
    thin_bar: bool
    align: str
    prefix: str  # icons / letters / compact_items
    separator: str  # bar / bullet / dot / none
    font_size: int  # unscaled, as in the settings
    bold: bool
    progress_margin_width: int  # unscaled
    min_bar_pct: int
    percent_digits: int


def _text(settings: LuaTable, key: str, choices: tuple[tuple[str, str], ...]) -> str:
    value = footer_value(settings, key)
    default = FOOTER_DEFAULTS[key]
    assert isinstance(default, str)
    return value if isinstance(value, str) and value in dict(choices) else default


def footer_int(settings: LuaTable, key: str) -> int:
    """A number setting of the footer, KOReader's default when it is not a number."""
    value = footer_value(settings, key)
    default = FOOTER_DEFAULTS[key]
    assert isinstance(default, int)
    return (
        int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default
    )


def _mode_order(settings: LuaTable) -> list[str]:
    """``mode_index`` (readerfooter.lua set_mode_index): a saved ``order`` first, then the rest."""
    order = footer_value(settings, "order")
    names: list[str] = []
    if isinstance(order, dict):
        for index in sorted(k for k in order if isinstance(k, int)):
            name = order[index]
            if isinstance(name, str) and name in FOOTER_MODES and name not in names:
                names.append(name)
    names += [m for m in FOOTER_MODES if m not in names]
    if "off" in names:  # index 0 is always "off"
        names.remove("off")
    return ["off", *names]


def footer(settings: LuaTable) -> Footer | None:
    """The bottom status bar, or None when it is hidden."""
    if footer_value(settings, "disabled") is True:
        return None
    mode = settings.get(FOOTER_MODE_KEY)
    mode = mode if isinstance(mode, int) and not isinstance(mode, bool) else DEFAULT_FOOTER_MODE
    order = _mode_order(settings)
    enabled = [m for m in order[1:] if footer_value(settings, m) is True]
    progress_bar = footer_value(settings, "disable_progress_bar") is not True
    if mode == 0 or (not enabled and not progress_bar):
        return None
    if footer_value(settings, "all_at_once") is True:
        items = tuple(enabled)
    else:  # one item at a time: the one the user last tapped to, else the first enabled one
        current = order[mode] if 0 < mode < len(order) else None
        items = (current,) if current in enabled else tuple(enabled[:1])
    digits = footer_value(settings, "progress_pct_format")
    return Footer(
        items=tuple(i for i in items if i is not None),
        progress_bar=progress_bar,
        bar_position=_text(settings, "progress_bar_position", BAR_POSITIONS),
        thin_bar=footer_value(settings, "progress_style_thin") is True,
        align=_text(settings, "align", ALIGNMENTS),
        prefix=_text(settings, "item_prefix", ITEM_PREFIXES),
        separator=_text(settings, "items_separator", SEPARATORS),
        font_size=footer_int(settings, "text_font_size"),
        bold=footer_value(settings, "text_font_bold") is True,
        progress_margin_width=footer_int(settings, "progress_margin_width"),
        min_bar_pct=footer_int(settings, "progress_bar_min_width_pct"),
        percent_digits=int(digits) if isinstance(digits, str) and digits.isdigit() else 0,
    )


@dataclass(frozen=True)
class Sample:
    """The made-up reading state the mockup shows."""

    page: int
    pages: int
    chapter_pages_left: int
    title: str
    author: str
    chapter: str
    clock: str = "19:26"
    battery: int = 87
    time_left_chapter: str = "0:12"  # statistics plugin, classic duration format (estimate)
    time_left_book: str = "2:05"


def _with_prefix(prefix: str | None, text: str) -> str:
    return f"{prefix} {text}" if prefix else text


def item_text(status: Footer, item: str, sample: Sample) -> str:
    """One item as readerfooter.lua's generator writes it."""
    symbols = SYMBOLS[status.prefix]
    percent = f"{100 * sample.page / sample.pages:.{status.percent_digits}f}%"
    battery = {"compact_items": BATTERY_SIGN, "icons": f"{BATTERY_SIGN}{sample.battery}%"}
    texts = {
        "page_progress": f"{sample.page} / {sample.pages}",
        "pages_left_book": (
            f"{symbols['pages_left_book']} {sample.pages - sample.page} / {sample.pages}"
        ),
        "pages_left": f"{symbols['pages_left']} {sample.chapter_pages_left}",
        "percentage": _with_prefix(symbols["percentage"], percent),
        "time": _with_prefix(symbols["time"], sample.clock),
        "battery": battery.get(status.prefix, f"B: {sample.battery}%"),
        "book_time_to_read": _with_prefix(symbols["book_time_to_read"], sample.time_left_book),
        "chapter_time_to_read": f"{symbols['chapter_time_to_read']} {sample.time_left_chapter}",
        "book_title": sample.title,
        "book_author": sample.author,
        "book_chapter": sample.chapter,
    }
    return texts.get(item, "")


def footer_text(status: Footer, sample: Sample) -> str:
    """The whole line of items with KOReader's separator (readerfooter.lua genSeparator)."""
    separator = {"bar": " | ", "bullet": " • ", "dot": " · "}.get(
        status.separator, " " if status.prefix == "compact_items" else "  "
    )
    texts = [item_text(status, item, sample) for item in status.items]
    if status.prefix == "compact_items":
        texts = [t.replace(" ", "\u200a") for t in texts]  # hair space, as KOReader does
    return separator.join(t for t in texts if t)


def header_items(settings: LuaTable) -> tuple[str, ...]:
    """The top bar's shown items (``cre_header_<item>`` = 1, each with its own default)."""
    shown = []
    for item, _label, default in HEADER_ITEMS:
        value = settings.get(header_key(item))
        on = value if isinstance(value, int) and not isinstance(value, bool) else default
        if on == 1:
            shown.append(item)
    return tuple(shown)
