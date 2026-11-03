"""Icons drawn with QPainter so the app ships without image files."""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPainterPath, QPen, QPixmap, QPolygonF

RED = QColor("#d93025")
GREY = QColor("#8a8f98")
AMBER = QColor("#f2a900")
GREEN = QColor("#1e9e5a")
BLUE = QColor("#2f6fde")


def _pix(size: int) -> tuple[QPixmap, QPainter]:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    return pm, p


def transport_icon(kind: str, color: QColor, size: int = 32) -> QIcon:
    pm, p = _pix(size)
    p.setPen(Qt.NoPen)
    s = size
    if kind == "record":
        p.setBrush(RED)
        p.drawEllipse(QRectF(s * 0.28, s * 0.28, s * 0.44, s * 0.44))
    else:
        p.setBrush(color)
        if kind == "play":
            p.drawPolygon(QPolygonF([QPointF(s * 0.32, s * 0.24), QPointF(s * 0.76, s * 0.5),
                                     QPointF(s * 0.32, s * 0.76)]))
        elif kind == "pause":
            p.drawRect(QRectF(s * 0.3, s * 0.26, s * 0.14, s * 0.48))
            p.drawRect(QRectF(s * 0.56, s * 0.26, s * 0.14, s * 0.48))
        elif kind == "stop":
            p.drawRect(QRectF(s * 0.3, s * 0.3, s * 0.4, s * 0.4))
        elif kind == "rewind":
            for x0 in (0.5, 0.18):
                p.drawPolygon(QPolygonF([QPointF(s * (x0 + 0.32), s * 0.28), QPointF(s * x0, s * 0.5),
                                         QPointF(s * (x0 + 0.32), s * 0.72)]))
        elif kind == "ffwd":
            for x0 in (0.18, 0.5):
                p.drawPolygon(QPolygonF([QPointF(s * x0, s * 0.28), QPointF(s * (x0 + 0.32), s * 0.5),
                                         QPointF(s * x0, s * 0.72)]))
    p.end()
    return QIcon(pm)


def app_icon(state_color: QColor | None = None, size: int = 64) -> QIcon:
    """Microphone in a rounded square; a coloured dot shows the state (tray)."""
    icon = QIcon()
    for sz in (16, 20, 24, 32, 48, 64, 128, 256):
        pm, p = _pix(sz)
        s = sz
        p.setBrush(QColor("#1f3b73"))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(QRectF(0, 0, s, s), s * 0.22, s * 0.22)
        p.setBrush(QColor("#ffffff"))
        p.drawRoundedRect(QRectF(s * 0.38, s * 0.16, s * 0.24, s * 0.42), s * 0.12, s * 0.12)
        pen = QPen(QColor("#ffffff"), max(1.0, s * 0.06))
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        path = QPainterPath()
        path.moveTo(s * 0.28, s * 0.44)
        path.cubicTo(s * 0.28, s * 0.72, s * 0.72, s * 0.72, s * 0.72, s * 0.44)
        p.drawPath(path)
        p.drawLine(QPointF(s * 0.5, s * 0.66), QPointF(s * 0.5, s * 0.8))
        if state_color is not None:
            p.setPen(QPen(QColor("#ffffff"), max(1.0, s * 0.05)))
            p.setBrush(state_color)
            r = s * 0.34
            p.drawEllipse(QRectF(s - r - 1, s - r - 1, r, r))
        p.end()
        icon.addPixmap(pm)
    return icon


def tray_icon(state: str) -> QIcon:
    color = {"idle": GREY, "stopped": GREY, "armed": AMBER, "recording": RED, "rec_paused": AMBER,
             "playing": GREEN, "play_paused": GREEN, "seeking": GREEN}.get(state, GREY)
    return app_icon(color)


# Recorder state -> taskbar badge kind (None = no badge). Same colours as the tray icon.
BADGE_FOR_STATE = {"recording": "recording", "armed": "armed", "rec_paused": "paused"}


def badge_image(kind: str, size: int) -> QImage:
    """Taskbar badge: a red dot while recording, amber while Vox waits or recording is paused.

    The white ring keeps the dot visible on both light and dark taskbars.
    """
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    ring = max(1.5, size * 0.13)
    m = ring / 2 + 0.5
    p.setPen(QPen(QColor("#ffffff"), ring))
    p.setBrush(RED if kind == "recording" else AMBER)
    p.drawEllipse(QRectF(m, m, size - 2 * m, size - 2 * m))
    if kind == "paused":
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#ffffff"))
        bw, bh = size * 0.11, size * 0.38
        for x in (size * 0.335, size * 0.555):
            p.drawRect(QRectF(x, (size - bh) / 2, bw, bh))
    p.end()
    return img
