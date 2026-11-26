"""The reader drawn as a vector mockup, with every hardware control as a real, clickable button.

The body is drawn from the device definition in millimetres (no product photos are shipped:
they are the manufacturer's copyright). Each pressable control is a child ``KeyButton`` placed
over its drawn position, so it has keyboard focus, a tooltip and an accessible name — tests and
screen readers can press it like any other button.
"""

from __future__ import annotations

from PySide6.QtCore import QElapsedTimer, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QImage,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPalette,
    QPen,
    QPolygonF,
    QResizeEvent,
)
from PySide6.QtWidgets import QAbstractButton, QSizePolicy, QWidget

from kodesk.core.devices import Control, DeviceModel, Face, Rect

LONG_PRESS_MS = 2000  # manual: hold Menu | On/Off about 2 s to power off/on
BODY = QColor("#3b4048")  # device plastics: a fixed physical colour, not UI chrome
RIM = QColor("#8c96a3")
KEY = QColor("#2c3036")
GLYPH = QColor("#d8dee9")


def draw_glyph(painter: QPainter, glyph: str, rect: QRectF, color: QColor) -> None:
    """Small pictograms as printed on the keys / next to the ports."""
    painter.save()
    painter.setPen(QPen(color, max(1.0, rect.height() * 0.08)))
    painter.setBrush(color)
    center = rect.center()
    size = min(rect.width(), rect.height()) * 0.42
    if glyph == "home":
        roof = QPolygonF(
            [
                QPointF(center.x() - size / 2, center.y()),
                QPointF(center.x(), center.y() - size / 2),
                QPointF(center.x() + size / 2, center.y()),
            ]
        )
        painter.drawPolygon(roof)
        painter.drawRect(QRectF(center.x() - size / 3, center.y(), size * 2 / 3, size / 2.4))
    elif glyph in ("prev", "next"):
        sign = -1 if glyph == "prev" else 1
        triangle = QPolygonF(
            [
                QPointF(center.x() + sign * size / 2, center.y()),
                QPointF(center.x() - sign * size / 3, center.y() - size / 2),
                QPointF(center.x() - sign * size / 3, center.y() + size / 2),
            ]
        )
        painter.drawPolygon(triangle)
    elif glyph == "power":
        painter.setBrush(Qt.BrushStyle.NoBrush)
        arc = QRectF(center.x() - size / 2, center.y() - size / 2, size, size)
        painter.drawArc(arc, 120 * 16, 300 * 16)
        painter.drawLine(QPointF(center.x(), center.y() - size * 0.6), center)
    elif glyph == "usb":
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
    elif glyph == "led":
        painter.drawEllipse(rect)
    painter.restore()


class KeyButton(QAbstractButton):
    """A hardware key. Emits ``clicked`` for a short press and ``longPressed`` after a hold."""

    longPressed = Signal()

    def __init__(self, control: Control, parent: QWidget) -> None:
        super().__init__(parent)
        self.control = control
        self.setObjectName(f"key-{control.id}")
        self.setText(control.label)
        self.setAccessibleName(control.label)
        self.setToolTip(f"{control.label} — {control.firmware_role}")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCheckable(False)
        self.selected = False
        self._timer = QElapsedTimer()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self._timer.start()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        held = self._timer.isValid() and self._timer.elapsed() >= LONG_PRESS_MS
        if held and self.rect().contains(event.position().toPoint()):
            self.setDown(False)
            self._timer.invalidate()
            self.longPressed.emit()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        fill = KEY.lighter(160) if self.isDown() else KEY.lighter(125) if self.underMouse() else KEY
        painter.setPen(QPen(RIM, 1))
        painter.setBrush(fill)
        painter.drawRoundedRect(rect, 2, 2)
        draw_glyph(painter, self.control.glyph, rect, GLYPH)
        if self.selected or self.hasFocus():
            highlight = self.palette().color(QPalette.ColorRole.Highlight)
            painter.setPen(QPen(highlight, 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 2, 2)
        painter.end()

    def sizeHint(self) -> QSize:
        return QSize(40, 12)


class DeviceView(QWidget):
    """Draws one face of the device; the front shows the screen image."""

    keyPressed = Signal(str)  # control id
    keyLongPressed = Signal(str)

    def __init__(self, device: DeviceModel, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.device = device
        self.face = Face.FRONT
        self.screen_image = QImage()
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(240, 320)
        self.keys: dict[str, KeyButton] = {}
        for control in device.controls:
            if not control.pressable:
                continue
            button = KeyButton(control, self)
            button.clicked.connect(lambda _=False, cid=control.id: self.keyPressed.emit(cid))
            button.longPressed.connect(lambda cid=control.id: self.keyLongPressed.emit(cid))
            self.keys[control.id] = button
        self._place_keys()

    # ----------------------------------------------------------------- geometry

    def face_size_mm(self, face: Face) -> tuple[float, float]:
        width, height, depth = self.device.body_mm
        if face in (Face.FRONT, Face.BACK):
            return width, height
        if face in (Face.TOP, Face.BOTTOM):
            return width, depth
        return depth, height

    def _transform(self) -> tuple[float, QPointF]:
        """mm → widget px scale and the top-left offset that centres the face."""
        face_w, face_h = self.face_size_mm(self.face)
        margin = 12.0
        scale = min((self.width() - 2 * margin) / face_w, (self.height() - 2 * margin) / face_h)
        offset = QPointF((self.width() - face_w * scale) / 2, (self.height() - face_h * scale) / 2)
        return max(scale, 0.1), offset

    def map_rect(self, rect: Rect) -> QRectF:
        scale, offset = self._transform()
        return QRectF(
            offset.x() + rect.x * scale, offset.y() + rect.y * scale, rect.w * scale, rect.h * scale
        )

    def _place_keys(self) -> None:
        for control_id, button in self.keys.items():
            control = next(c for c in self.device.controls if c.id == control_id)
            visible = control.face is self.face
            button.setVisible(visible)
            if visible:
                button.setGeometry(self.map_rect(control.rect).toAlignedRect())

    def set_face(self, face: Face) -> None:
        self.face = face
        self._place_keys()
        self.update()

    def set_screen(self, image: QImage) -> None:
        self.screen_image = image
        self.update()

    def select_key(self, control_id: str | None) -> None:
        for cid, button in self.keys.items():
            button.selected = cid == control_id
            button.update()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._place_keys()

    # ----------------------------------------------------------------- painting

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        scale, offset = self._transform()
        face_w, face_h = self.face_size_mm(self.face)
        body = QRectF(offset.x(), offset.y(), face_w * scale, face_h * scale)
        radius = (
            self.device.corner_radius_mm
            if self.face in (Face.FRONT, Face.BACK)
            else self.device.body_mm[2] / 2
        ) * scale
        path = QPainterPath()
        path.addRoundedRect(body, radius, radius)
        painter.setPen(QPen(RIM, max(1.0, scale * 0.6)))
        painter.setBrush(BODY)
        painter.drawPath(path)

        if self.face is Face.FRONT:
            screen = self.map_rect(self.device.screen_rect)
            if not self.screen_image.isNull():
                painter.drawImage(screen, self.screen_image)
            painter.setPen(QPen(QColor("#1d2025"), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(screen)
        elif self.face is Face.BACK:
            painter.setPen(QPen(BODY.lighter(130), 1))
            for index in range(1, 12):
                x = body.left() + body.width() * index / 12
                painter.drawLine(
                    QPointF(x, body.top() + radius), QPointF(x, body.bottom() - radius)
                )

        for control in self.device.controls_on(self.face):
            if not control.pressable:
                draw_glyph(painter, control.glyph, self.map_rect(control.rect), GLYPH)
        painter.end()
