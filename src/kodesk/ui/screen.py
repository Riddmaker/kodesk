"""Draw the reader's screen content at the device's own pixel size (D-15 mockup).

Each view renders into a QImage of exactly ``device.screen_px`` so it can be laid over a real
screenshot pixel by pixel; the device view scales it down for display. Geometry comes from
kodesk.core.layout (KOReader formulas with source refs); this module only paints.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QFontMetricsF,
    QImage,
    QLinearGradient,
    QPainter,
    QPen,
    QPolygonF,
    QTextLayout,
    QTextOption,
)
from PySide6.QtSvg import QSvgRenderer

from kodesk.core.icons import MENU_TABS as MENU_TAB_ICONS
from kodesk.core.layout import BookLayout, scale_by_size
from kodesk.core.menus import FileList, TouchMenu, file_list, touch_menu
from kodesk.core.statusbar import BATTERY_SIGN, Footer, Sample, footer_text

TEXT_INDENT_EM = 1.2  # koreader/data/epub.css: p { text-indent: 1.2em; }
NOTO_SANS_HEIGHT_EM = 1.362
PICTURE_AFTER_PARAGRAPH = 1  # the sample page shows a picture after the first paragraph


def sample_book() -> tuple[str, str, str, list[str]]:
    """Title, author, chapter and paragraphs of the bundled public-domain sample."""
    text = resources.files("kodesk.ui").joinpath("resources/sample-book.txt").read_text("utf-8")
    title, author, chapter, *paragraphs = [line for line in text.splitlines() if line.strip()]
    return title, author, chapter, paragraphs


def load_device_fonts(fonts_dir: Path | None) -> list[str]:
    """Register the reader's own fonts (read-only) so the mockup uses the same faces."""
    families: set[str] = set()
    if fonts_dir is None or not fonts_dir.is_dir():
        return []
    for path in sorted(fonts_dir.rglob("*")):
        if path.suffix.lower() in {".ttf", ".otf"} and path.is_file():
            font_id = QFontDatabase.addApplicationFont(str(path))
            if font_id >= 0:
                families.update(QFontDatabase.applicationFontFamilies(font_id))
    return sorted(families)


def _font(family: str, pixel_size: int, fallback: QFont.StyleHint) -> QFont:
    font = QFont(family)
    font.setStyleHint(fallback)
    font.setPixelSize(max(1, pixel_size))
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    return font


def sample_picture(width: int, height: int) -> QImage:
    """A small colour landscape standing in for a book illustration (drawn, nothing shipped)."""
    image = QImage(max(1, width), max(1, height), QImage.Format.Format_RGB32)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    sky = QLinearGradient(0, 0, 0, height)
    sky.setColorAt(0, QColor("#4a90d9"))
    sky.setColorAt(1, QColor("#cfe8ff"))
    painter.fillRect(0, 0, width, height, sky)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#f5c518"))
    painter.drawEllipse(QPointF(width * 0.78, height * 0.25), height * 0.12, height * 0.12)
    for color, top, bottom in (("#5a9e4b", 0.55, 1.0), ("#2f6b2f", 0.7, 1.0)):
        painter.setBrush(QColor(color))
        painter.drawPolygon(
            QPolygonF(
                [
                    QPointF(0, height * bottom),
                    QPointF(0, height * (top + 0.1)),
                    QPointF(width * 0.35, height * top),
                    QPointF(width * 0.7, height * (top + 0.12)),
                    QPointF(width, height * (top + 0.02)),
                    QPointF(width, height * bottom),
                ]
            )
        )
    painter.end()
    return image


@dataclass
class _Line:
    layout: QTextLayout
    index: int
    height: float


@dataclass
class _Picture:
    width: int
    height: int


class BookRenderer:
    """Paginates the sample book for a BookLayout and paints single pages."""

    def __init__(self, layout: BookLayout) -> None:
        self.layout = layout
        self.title, self.author, self.chapter, paragraphs = sample_book()
        self.font = _font(layout.font_face, layout.font_px, QFont.StyleHint.Serif)
        metrics = QFontMetricsF(self.font)
        # crengine: line height = font height * line_spacing% (interline space).
        self.line_height = metrics.height() * layout.line_spacing_pct / 100
        self.ascent = metrics.ascent()
        self._layouts: list[QTextLayout] = []
        self.pages = self._paginate(paragraphs)

    def _picture(self) -> _Picture:
        width = round(self.layout.text.w * 0.8)
        return _Picture(width, min(round(width * 0.6), self.layout.text.h // 2))

    def _paginate(self, paragraphs: list[str]) -> list[list[tuple[_Line | _Picture, float]]]:
        width = self.layout.text.w
        indent = TEXT_INDENT_EM * self.layout.font_px
        option = QTextOption()
        option.setAlignment(Qt.AlignmentFlag.AlignJustify)
        option.setWrapMode(QTextOption.WrapMode.WordWrap)
        lines: list[_Line | _Picture] = []
        for number, paragraph in enumerate(paragraphs):
            if number == PICTURE_AFTER_PARAGRAPH:
                lines.append(self._picture())
            text_layout = QTextLayout(paragraph, self.font)
            text_layout.setTextOption(option)
            text_layout.beginLayout()
            first = True
            while True:
                line = text_layout.createLine()
                if not line.isValid():
                    break
                offset = indent if first else 0.0
                line.setLineWidth(width - offset)
                line.setPosition(QPointF(offset, 0))
                lines.append(_Line(text_layout, line.lineNumber(), self.line_height))
                first = False
            text_layout.endLayout()
            self._layouts.append(text_layout)
        pages: list[list[tuple[_Line | _Picture, float]]] = [[]]
        y = 0.0
        for entry in lines:
            if y + entry.height > self.layout.text.h and pages[-1]:
                pages.append([])
                y = 0.0
            pages[-1].append((entry, y))
            y += entry.height
        return pages

    def render(self, page: int, battery: int = 87, clock: str = "19:26") -> QImage:
        """One book page at device pixels, with the top bar; night mode inverts it."""
        lay = self.layout
        page = max(0, min(page, len(self.pages) - 1))
        image = QImage(lay.screen_w, lay.screen_h, QImage.Format.Format_RGB32)
        background, ink = (
            (QColor("black"), QColor("white"))
            if lay.night_mode
            else (QColor("white"), QColor("black"))
        )
        image.fill(background)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.setPen(ink)
        for line, y in self.pages[page]:
            if isinstance(line, _Picture):
                self._paint_picture(painter, line, y)
                continue
            text_line = line.layout.lineAt(line.index)
            # Baseline sits at ascent within the (possibly stretched) line box.
            extra = (line.height - QFontMetricsF(self.font).height()) / 2
            origin = QPointF(lay.text.x, lay.text.y + y + extra - text_line.y())
            text_line.draw(painter, origin)
        if lay.header is not None:
            self._paint_header(painter, ink, page, battery, clock)
        if lay.footer is not None and lay.footer_bar is not None:
            sample = Sample(
                page=page + 1,
                pages=len(self.pages),
                chapter_pages_left=len(self.pages) - page - 1,
                title=self.title,
                author=self.author,
                chapter=self.chapter,
                clock=clock,
                battery=battery,
            )
            self._paint_footer(painter, ink, lay.footer_bar, sample)
        painter.end()
        return image

    def _paint_picture(self, painter: QPainter, picture: _Picture, y: float) -> None:
        """Pictures as they end up on screen: in night mode they are negatives unless
        crengine pre-inverts them (copt_nightmode_images = 1, KOReader's default)."""
        lay = self.layout
        image = sample_picture(picture.width, picture.height)
        if lay.night_mode and not lay.night_pictures_original:
            image.invertPixels()
        x = lay.text.x + (lay.text.w - picture.width) // 2
        painter.drawImage(QPointF(x, lay.text.y + y), image)

    def _paint_header(
        self, painter: QPainter, ink: QColor, page: int, battery: int, clock: str
    ) -> None:
        lay = self.layout
        assert lay.header is not None
        font = _font("Noto Sans", lay.header_font_px, QFont.StyleHint.SansSerif)
        painter.setFont(font)
        painter.setPen(ink)
        items = set(lay.header_items)
        left = " · ".join(
            part for key, part in (("author", self.author), ("title", self.title)) if key in items
        )
        right_parts = []
        if "clock" in items:
            right_parts.append(clock)
        if "page_number" in items:
            total = f" / {len(self.pages)}" if "page_count" in items else ""
            right_parts.append(f"{page + 1}{total}")
        if "reading_percent" in items:
            right_parts.append(f"{100 * (page + 1) // len(self.pages)}%")
        if "battery" in items:  # crengine draws a battery icon; a plain sign stands in for it
            right_parts.append(BATTERY_SIGN)
        if "battery_percent" in items:
            right_parts.append(f"{battery}%")
        box = QRect(lay.header.x + 4, lay.header.y + 4, lay.header.w - 8, lay.header.h - 4)
        painter.drawText(box, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), left)
        painter.drawText(
            box,
            int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            "  ".join(right_parts),
        )
        if "chapter_marks" in items:
            line_y = lay.header.y + lay.header.h - 2
            painter.drawLine(lay.header.x, line_y, lay.header.x + lay.header.w, line_y)

    def _paint_footer(self, painter: QPainter, ink: QColor, status: Footer, sample: Sample) -> None:
        """KOReader's status bar (readerfooter.lua updateFooterContainer), simplified: the
        progress bar alongside, above or below the items; chapter marks are not drawn."""
        lay = self.layout
        assert lay.footer is not None

        def scale(px: float) -> int:
            return scale_by_size(px, lay.screen_w, lay.screen_h)

        # Font:getFace scales the size; the bottom padding sits below the bar's row.
        font = _font("Noto Sans", scale(status.font_size), QFont.StyleHint.SansSerif)
        font.setBold(status.bold)
        metrics = QFontMetricsF(font)
        margin = scale(status.progress_margin_width)
        row = QRectF(lay.footer.x + margin, lay.footer.y, lay.footer.w - 2 * margin, 0)
        row.setHeight(lay.footer.h - scale(1))
        text = footer_text(status, sample)
        thickness = scale(3 if status.thin_bar else 7)  # progress_style_thin/thick_height
        if status.progress_bar and status.bar_position == "alongside":
            max_text = row.width() * (100 - status.min_bar_pct) / 100
            text_w = min(metrics.horizontalAdvance(text), max_text) if text else 0.0
            bar_w = row.width() - text_w - (margin if text else 0)
            _paint_progress(
                painter,
                ink,
                QRectF(row.x(), row.center().y() - thickness / 2, bar_w, thickness),
                sample.page / sample.pages,
            )
            row = QRectF(row.right() - text_w, row.y(), text_w, row.height())
        elif status.progress_bar:
            above = status.bar_position == "above"
            y = row.y() if above else row.bottom() - thickness
            _paint_progress(
                painter, ink, QRectF(row.x(), y, row.width(), thickness), sample.page / sample.pages
            )
            row = row.adjusted(0, thickness if above else 0, 0, 0 if above else -thickness)
        if text:
            align = {
                "left": Qt.AlignmentFlag.AlignLeft,
                "right": Qt.AlignmentFlag.AlignRight,
            }.get(status.align, Qt.AlignmentFlag.AlignHCenter)
            painter.setFont(font)
            painter.setPen(ink)
            elided = metrics.elidedText(text, Qt.TextElideMode.ElideRight, row.width())
            painter.drawText(row, int(align | Qt.AlignmentFlag.AlignVCenter), elided)

    @property
    def page_count(self) -> int:
        return len(self.pages)


def _paint_progress(painter: QPainter, ink: QColor, rect: QRectF, fraction: float) -> None:
    """KOReader's ProgressWidget: an outline, filled up to the reading position."""
    painter.setPen(QPen(ink, 1))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRect(rect)
    painter.fillRect(QRectF(rect.x(), rect.y(), rect.width() * fraction, rect.height()), ink)


def _canvas(size: tuple[int, int], night: bool) -> tuple[QImage, QPainter, QColor]:
    image = QImage(size[0], size[1], QImage.Format.Format_RGB32)
    image.fill(QColor("black") if night else QColor("white"))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    ink = QColor("white") if night else QColor("black")
    painter.setPen(ink)
    return image, painter, ink


MENU_TABS = ("☰", "Aa", "⚙", "✎", "⌕", "▤", "≡")  # stand-ins for the appbar.* icons
TAB_ICONS = MENU_TAB_ICONS
MENU_ITEMS = (  # the navigation tab of a reflowable book (reader_menu_order.lua navi)
    ("Table of contents", False),
    ("Bookmarks", False),
    ("Book map", False),
    ("Page browser", False),
    ("Go to page", False),
    ("Skim document", False),
    ("Follow links", True),
    ("Reading statistics", True),
)


def load_icon(path: Path, size: int, night: bool = False) -> QImage | None:
    """An icon file from the reader (SVG or PNG) at ``size`` × ``size`` px, or None if Qt
    cannot read it. QtSvg draws SVG Tiny only: no scripts, no external files."""
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    if path.suffix == ".svg":
        renderer = QSvgRenderer(str(path))
        if not renderer.isValid():
            return None
        painter = QPainter(image)
        renderer.render(painter, QRectF(0, 0, size, size))
        painter.end()
    else:
        picture = QImage(str(path))
        if picture.isNull():
            return None
        image = picture.scaled(
            QSize(size, size),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ).convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    if night:  # KOReader inverts the whole screen in night mode
        image.invertPixels(QImage.InvertMode.InvertRgb)
    return image


def tab_bar_rect(size: tuple[int, int]) -> QRect:
    """Where the menu's tab icons are, in screen pixels (for the Garage's clickable spot)."""
    menu = touch_menu(*size)
    return QRect(0, menu.border, size[0], menu.bar_h)


def _paint_menu_tabs(
    painter: QPainter,
    ink: QColor,
    menu: TouchMenu,
    width: int,
    icons: list[QImage | None],
) -> None:
    """The tab bar; the first tab (navigation) is selected: no line under it, separators
    left and right of it (touchmenu.lua:341-422). Real icons where loaded, plain signs
    otherwise."""
    top = menu.border
    cell = 2 * menu.tab_pad + menu.icon
    painter.setFont(_font("Noto Sans", round(menu.icon * 0.6), QFont.StyleHint.SansSerif))
    painter.setPen(ink)
    for index, (icon, glyph) in enumerate(zip(icons, MENU_TABS, strict=True)):
        x = menu.tab_x(index, len(MENU_TABS), width)
        if icon is not None:
            painter.drawImage(QPointF(x + menu.tab_pad, top + (menu.bar_h - menu.icon) / 2), icon)
            continue
        painter.drawText(QRectF(x, top, cell, menu.bar_h), int(Qt.AlignmentFlag.AlignCenter), glyph)
    selected = menu.tab_x(0, len(MENU_TABS), width)
    line_y = top + menu.bar_h
    painter.fillRect(0, line_y, selected, menu.line, ink)
    painter.fillRect(selected + cell, line_y, width - selected - cell, menu.line, ink)
    for x in (selected - menu.tab_sep, selected + cell):
        painter.fillRect(max(0, x), top, menu.tab_sep, menu.bar_h, ink)


def render_menu(
    size: tuple[int, int],
    night: bool,
    page: QImage | None = None,
    icons: dict[str, Path] | None = None,
) -> QImage:
    """KOReader's top menu (TouchMenu) over the book page, measured from touchmenu.lua
    (kodesk.core.menus). Tab icons: the reader's own files where ``icons`` names them
    (kodesk.core.icons), plain signs otherwise."""
    width, height = size
    image, painter, ink = _canvas(size, night)
    if page is not None:
        painter.drawImage(0, 0, page)
    menu = touch_menu(width, height)
    menu_h = menu.height(len(MENU_ITEMS))
    painter.fillRect(0, 0, width, menu_h, QColor("black") if night else QColor("white"))
    painter.fillRect(0, 0, width, menu.border, ink)
    painter.fillRect(0, menu_h - menu.border, width, menu.border, ink)
    paths = icons or {}
    tab_icons = [
        load_icon(paths[name], menu.icon, night) if name in paths else None for name in TAB_ICONS
    ]
    _paint_menu_tabs(painter, ink, menu, width, tab_icons)
    line_y = menu.border + menu.bar_h
    # Items: checkmark column, text, "▸" for submenus.
    font = _font("Noto Sans", menu.item_font_px, QFont.StyleHint.SansSerif)
    painter.setFont(font)
    check_w = QFontMetricsF(font).horizontalAdvance("▢ ")
    y = line_y + menu.line
    for text, submenu in MENU_ITEMS:
        painter.drawText(
            QRectF(menu.padding + check_w, y, width - 2 * menu.padding - check_w, menu.item_h),
            int(Qt.AlignmentFlag.AlignVCenter),
            f"{text} ▸" if submenu else text,
        )
        y += menu.item_h + menu.item_gap
    # Footer: one page, so only the time and battery on the right (touchmenu.lua:723).
    footer = QRectF(
        menu.padding, y - menu.item_gap + menu.footer_top, width - 2 * menu.padding, menu.footer_h
    )
    painter.setFont(_font("Noto Sans", menu.footer_font_px, QFont.StyleHint.SansSerif))
    footer.setRight(footer.right() - scale_by_size(10, width, height))
    painter.drawText(
        footer, int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), "19:26 ⌁87%"
    )
    painter.end()
    return image


def _paint_title_bar(painter: QPainter, files: FileList, width: int, path: str) -> int:
    """FileManager's title bar: home icon left, plus icon right, "KOReader" and the path
    centred (filemanager.lua:118, titlebar.lua); returns its height."""
    title_font = _font("Noto Sans", files.title_font_px, QFont.StyleHint.SansSerif)
    title_font.setBold(True)
    sub_font = _font("Noto Sans", files.subtitle_font_px, QFont.StyleHint.SansSerif)
    title_line = QFontMetricsF(title_font).height()
    sub_line = QFontMetricsF(sub_font).height()
    # Title bar: home icon left, plus icon right, "KOReader" and the path centred.
    icon_font = _font("Noto Sans", round(files.icon * 0.7), QFont.StyleHint.SansSerif)
    painter.setFont(icon_font)
    pad = files.button_pad
    painter.drawText(
        QRectF(pad, pad, files.icon, files.icon), int(Qt.AlignmentFlag.AlignCenter), "⌂"
    )
    painter.drawText(
        QRectF(width - pad - files.icon, pad, files.icon, files.icon),
        int(Qt.AlignmentFlag.AlignCenter),
        "+",
    )
    painter.setFont(title_font)
    painter.drawText(
        QRectF(0, files.title_top, width, title_line),
        int(Qt.AlignmentFlag.AlignHCenter),
        "KOReader",
    )
    painter.setFont(sub_font)
    painter.drawText(
        QRectF(0, files.title_top + title_line + files.title_gap, width, sub_line),
        int(Qt.AlignmentFlag.AlignHCenter),
        path,
    )
    return files.title_h(title_line, sub_line)


@dataclass(frozen=True)
class Shelf:
    """How the library is drawn: KOReader's classic list (mode None) or a mode of Cover browser
    / Project: Title (kodesk.core.librarymode), with the page sizes the reader uses."""

    mode: str | None = None
    grid: tuple[int, int] = (3, 3)  # mosaic columns × rows
    per_page: int = 10  # list rows
    path: str = "/mnt/ext1"


def render_library(
    size: tuple[int, int], night: bool, entries: list[str], page: int, shelf: Shelf | None = None
) -> QImage:
    """The file browser. The classic list is measured from menu.lua and titlebar.lua
    (kodesk.core.menus); list and mosaic modes are an approximation (B-21): the same title bar
    and footer, the plugin's rows or grid, plain placeholders instead of real covers. Folder
    sizes, file counts, authors and progress are not drawn (names only)."""
    shelf = shelf or Shelf()
    width, height = size
    image, painter, ink = _canvas(size, night)
    kind = (shelf.mode or "classic").split("_", 1)[0]
    files = file_list(width, height, shelf.per_page) if kind == "list" else file_list(width, height)
    title_h = _paint_title_bar(painter, files, width, shelf.path)
    per_page = shelf.grid[0] * shelf.grid[1] if kind == "mosaic" else files.per_page
    pages = max(1, -(-len(entries) // per_page))
    page = max(0, min(page, pages - 1))
    shown = entries[page * per_page : (page + 1) * per_page]
    gray = QColor("#aaaaaa") if night else QColor("#555555")  # COLOR_DARK_GRAY
    body = QRectF(0, title_h, width, height - title_h - files.footer_h)
    if kind == "mosaic":
        _paint_mosaic(painter, (ink, gray), files, body, shown, shelf.grid)
    else:
        _paint_rows(painter, (ink, gray), files, body, shown, shelf.mode)
    _paint_page_footer(painter, ink if pages > 1 else gray, files, size, (page, pages))
    painter.end()
    return image


def _paint_rows(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    painter: QPainter,
    pens: tuple[QColor, QColor],
    files: FileList,
    body: QRectF,
    entries: list[str],
    mode: str | None,
) -> None:
    """One row per entry, a dark gray line under each; list modes with a cover placeholder
    left (listmenu.lua) and the name in bold where the plugin shows the book's title."""
    ink, gray = pens
    item_h = files.item_h(round(body.bottom() + files.footer_h), round(body.top()))
    covers = mode is not None and "image" in mode
    name_font = _font("Noto Sans", files.name_font_px, QFont.StyleHint.SansSerif)
    name_font.setBold(mode is not None and "meta" in mode)
    metrics = QFontMetricsF(name_font)
    painter.setFont(name_font)
    y = body.top()
    for entry in entries:
        left = files.side_pad
        if covers:
            pad = files.side_pad / 3
            cover_h = item_h - files.line - 2 * pad
            _paint_cover(painter, (ink, gray), QRectF(left, y + pad, cover_h * 0.7, cover_h), entry)
            left += round(cover_h * 0.7 + files.side_pad)
        content_w = body.width() - left - files.side_pad
        painter.setPen(ink)
        painter.drawText(
            QRectF(left, y, content_w, item_h - files.line),
            int(Qt.AlignmentFlag.AlignVCenter),
            metrics.elidedText(entry, Qt.TextElideMode.ElideRight, content_w),
        )
        painter.fillRect(
            QRectF(
                files.side_pad,
                y + item_h - files.line,
                body.width() - 2 * files.side_pad,
                files.line,
            ),
            gray,
        )
        y += item_h


def _paint_mosaic(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    painter: QPainter,
    pens: tuple[QColor, QColor],
    files: FileList,
    body: QRectF,
    entries: list[str],
    grid: tuple[int, int],
) -> None:
    """A grid of covers (mosaicmenu.lua); a book without a cover image shows its name in a
    frame, which is what every placeholder here looks like."""
    cols, rows = grid
    cell_w = (body.width() - 2 * files.side_pad) / cols
    cell_h = body.height() / rows
    margin = files.side_pad / 2
    painter.setFont(_font("Noto Sans", files.info_font_px, QFont.StyleHint.SansSerif))
    for index, entry in enumerate(entries):
        x = files.side_pad + (index % cols) * cell_w
        y = body.top() + (index // cols) * cell_h
        cover_h = cell_h - 2 * margin
        cover_w = min(cell_w - 2 * margin, cover_h * 0.7)
        rect = QRectF(x + (cell_w - cover_w) / 2, y + margin, cover_w, cover_h)
        _paint_cover(painter, pens, rect, entry, named=True)


def _paint_cover(
    painter: QPainter,
    pens: tuple[QColor, QColor],
    rect: QRectF,
    entry: str,
    named: bool = False,
) -> None:
    """A cover placeholder: a frame (two for a folder), the name inside when ``named``."""
    ink, gray = pens
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(gray, max(1.0, rect.width() / 80)))
    if entry.endswith("/"):  # a folder: the edge of a second frame behind, like a stack
        step = rect.width() / 12
        rect = rect.adjusted(0, step, -step, 0)
        painter.drawPolyline(
            QPolygonF(
                [
                    QPointF(rect.left() + step, rect.top()),
                    QPointF(rect.left() + step, rect.top() - step),
                    QPointF(rect.right() + step, rect.top() - step),
                    QPointF(rect.right() + step, rect.bottom() - step),
                    QPointF(rect.right(), rect.bottom() - step),
                ]
            )
        )
    painter.drawRect(rect)
    if named:
        painter.setPen(ink)
        pad = rect.width() / 10
        inner = rect.adjusted(pad, pad, -pad, -pad)
        painter.drawText(
            inner,
            int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap),
            entry.rstrip("/"),
        )


def _paint_page_footer(
    painter: QPainter, pen: QColor, files: FileList, size: tuple[int, int], page: tuple[int, int]
) -> None:
    """Chevrons and "Page x of y", centred at the bottom (menu.lua:808); grey on one page."""
    width, height = size
    painter.setPen(pen)
    painter.setFont(_font("Noto Sans", files.footer_font_px, QFont.StyleHint.SansSerif))
    spacer = " " * max(1, round(files.footer_spacer / max(1, files.footer_font_px / 3)))
    text = f"Page {page[0] + 1} of {page[1]}"
    footer = f"\u00ab{spacer}\u2039{spacer}{text}{spacer}\u203a{spacer}\u00bb"
    painter.drawText(
        QRectF(0, height - files.footer_h, width, files.footer_h),
        int(Qt.AlignmentFlag.AlignCenter),
        footer,
    )


def fit_picture(size: tuple[int, int], picture: QImage) -> QImage:
    """A picture at exactly the screen size: scaled to fit, centred on white (the way the
    firmware shows a sleep logo, and what kodesk puts on the reader)."""
    canvas, painter, _ = _canvas(size, night=False)
    scaled = picture.scaled(
        size[0],
        size[1],
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    painter.drawImage((size[0] - scaled.width()) // 2, (size[1] - scaled.height()) // 2, scaled)
    painter.end()
    return canvas


def render_sleep(size: tuple[int, int], image_path: Path | None) -> QImage:
    """The firmware's sleep logo, scaled like the firmware does (it is stored at screen size)."""
    picture = QImage(str(image_path)) if image_path else QImage()
    if not picture.isNull():
        return fit_picture(size, picture)
    canvas, painter, _ = _canvas(size, night=False)
    painter.setFont(_font("Noto Sans", 40, QFont.StyleHint.SansSerif))
    painter.drawText(
        QRect(0, 0, size[0], size[1]),
        int(Qt.AlignmentFlag.AlignCenter),
        f"Sleep screen\n{size[0]} × {size[1]} px",
    )
    painter.end()
    return canvas
