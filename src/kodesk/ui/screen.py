"""Draw the reader's screen content at the device's own pixel size (D-15 mockup).

Each view renders into a QImage of exactly ``device.screen_px`` so it can be laid over a real
screenshot pixel by pixel; the device view scales it down for display. Geometry comes from
kodesk.core.layout (KOReader formulas with source refs); this module only paints.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QFontMetricsF,
    QImage,
    QPainter,
    QPen,
    QTextLayout,
    QTextOption,
)

from kodesk.core.layout import BookLayout

TEXT_INDENT_EM = 1.2  # koreader/data/epub.css: p { text-indent: 1.2em; }
NOTO_SANS_HEIGHT_EM = 1.362


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


@dataclass
class _Line:
    layout: QTextLayout
    index: int
    height: float


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

    def _paginate(self, paragraphs: list[str]) -> list[list[tuple[_Line, float]]]:
        width = self.layout.text.w
        indent = TEXT_INDENT_EM * self.layout.font_px
        option = QTextOption()
        option.setAlignment(Qt.AlignmentFlag.AlignJustify)
        option.setWrapMode(QTextOption.WrapMode.WordWrap)
        lines: list[_Line] = []
        for paragraph in paragraphs:
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
        pages: list[list[tuple[_Line, float]]] = [[]]
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
            text_line = line.layout.lineAt(line.index)
            # Baseline sits at ascent within the (possibly stretched) line box.
            extra = (line.height - QFontMetricsF(self.font).height()) / 2
            origin = QPointF(lay.text.x, lay.text.y + y + extra - text_line.y())
            text_line.draw(painter, origin)
        if lay.header is not None:
            self._paint_header(painter, ink, page, battery, clock)
        painter.end()
        return image

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
        if "battery_percent" in items or "battery" in items:
            right_parts.append(f"[{battery}%]")
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

    @property
    def page_count(self) -> int:
        return len(self.pages)


def _canvas(size: tuple[int, int], night: bool) -> tuple[QImage, QPainter, QColor]:
    image = QImage(size[0], size[1], QImage.Format.Format_RGB32)
    image.fill(QColor("black") if night else QColor("white"))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    ink = QColor("white") if night else QColor("black")
    painter.setPen(ink)
    return image, painter, ink


def render_menu(size: tuple[int, int], night: bool, scale: float) -> QImage:
    """KOReader's top menu (TouchMenu) over the page — simplified: tab icons and items.

    Geometry is approximate until B-13 measures touchmenu.lua; labelled as such in the UI.
    """
    image, painter, ink = _canvas(size, night)
    width = size[0]
    tab_h = round(40 * scale)
    item_h = round(30 * scale)  # Size.item.height_default = scaleBySize(30)
    font = _font("Noto Sans", round(20 * scale), QFont.StyleHint.SansSerif)
    painter.setFont(font)
    tabs = ["⚙", "☰", "🔖", "⌕", "✎", "≡"]
    for index, glyph in enumerate(tabs):
        rect = QRectF(index * width / len(tabs), 0, width / len(tabs), tab_h)
        painter.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), glyph)
    painter.drawLine(0, tab_h, width, tab_h)
    entries = [
        "Table of contents",
        "Bookmarks",
        "Book map",
        "Page browser",
        "Go to page",
        "Skim document",
        "Follow links",
        "Reading statistics",
    ]
    y = tab_h
    for entry in entries:
        painter.drawText(
            QRectF(round(10 * scale), y, width, item_h), int(Qt.AlignmentFlag.AlignVCenter), entry
        )
        y += item_h
        painter.setPen(QPen(ink, 1))
        painter.drawLine(round(10 * scale), int(y), width - round(10 * scale), int(y))
    painter.drawLine(0, int(y) + 2, width, int(y) + 2)
    painter.end()
    return image


def render_library(
    size: tuple[int, int], night: bool, scale: float, entries: list[str], page: int
) -> QImage:
    """File browser list (classic mode) — simplified rows with title and folder marker."""
    image, painter, _ = _canvas(size, night)
    width, height = size
    title_h = round(40 * scale)
    row_h = round(64 * scale)
    painter.setFont(_font("Noto Sans", round(22 * scale), QFont.StyleHint.SansSerif))
    painter.drawText(QRectF(0, 0, width, title_h), int(Qt.AlignmentFlag.AlignCenter), "Books")
    painter.drawLine(0, title_h, width, title_h)
    per_page = max(1, (height - title_h - row_h) // row_h)
    pages = max(1, -(-len(entries) // per_page))
    page = max(0, min(page, pages - 1))
    painter.setFont(_font("Noto Sans", round(18 * scale), QFont.StyleHint.SansSerif))
    y = title_h
    for entry in entries[page * per_page : (page + 1) * per_page]:
        painter.drawText(
            QRectF(round(12 * scale), y, width - round(24 * scale), row_h),
            int(Qt.AlignmentFlag.AlignVCenter),
            entry,
        )
        y += row_h
        painter.drawLine(round(12 * scale), y, width - round(12 * scale), y)
    painter.drawText(
        QRectF(0, height - row_h, width, row_h),
        int(Qt.AlignmentFlag.AlignCenter),
        f"Page {page + 1} of {pages}",
    )
    painter.end()
    return image


def render_sleep(size: tuple[int, int], image_path: Path | None) -> QImage:
    """The firmware's sleep logo, scaled like the firmware does (it is stored at screen size)."""
    canvas, painter, _ = _canvas(size, night=False)
    picture = QImage(str(image_path)) if image_path else QImage()
    if not picture.isNull():
        scaled = picture.scaled(
            size[0],
            size[1],
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        painter.drawImage((size[0] - scaled.width()) // 2, (size[1] - scaled.height()) // 2, scaled)
    else:
        painter.setFont(_font("Noto Sans", 40, QFont.StyleHint.SansSerif))
        painter.drawText(
            QRect(0, 0, size[0], size[1]),
            int(Qt.AlignmentFlag.AlignCenter),
            f"Sleep screen\n{size[0]} × {size[1]} px",
        )
    painter.end()
    return canvas
