"""Custom widgets: level meter and position slider (FR-UI-004/005/009)."""
from __future__ import annotations

import math
import time

from PySide6.QtCore import QPoint, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPalette, QPen
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

from vrp.core.model import fmt_time_precise

SEGMENTS = 20


def _db_pos(pct: float) -> float:
    """0..100 % amplitude -> 0..1 on a -60..0 dBFS scale."""
    if pct <= 0:
        return 0.0
    db = 20 * math.log10(pct / 100.0)
    return max(0.0, min(1.0, (db + 60) / 60))


class LevelMeter(QWidget):
    """Segmented vertical meter, one bar per channel, peak hold, threshold line, clip light."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.levels: list[float] = [0.0]
        self.holds: list[float] = [0.0]
        self.hold_times: list[float] = [0.0]
        self.threshold = 4.0
        self.show_threshold = True
        self.clip_until = 0.0
        self.db_scale = False
        self.setMinimumSize(26, 110)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        self.setAccessibleName("Level meter")

    def sizeHint(self) -> QSize:
        return QSize(28, 120)

    def set_levels(self, levels: list[float], clip: bool = False) -> None:
        now = time.monotonic()
        if not levels:
            levels = [0.0] * max(1, len(self.levels))
        if len(levels) != len(self.levels):
            self.levels = [0.0] * len(levels)
            self.holds = [0.0] * len(levels)
            self.hold_times = [0.0] * len(levels)
        for i, v in enumerate(levels):
            # fast attack, smooth release
            self.levels[i] = v if v >= self.levels[i] else self.levels[i] * 0.7 + v * 0.3
            if v >= self.holds[i] or now - self.hold_times[i] > 1.5:
                self.holds[i] = v
                self.hold_times[i] = now
        if clip:
            self.clip_until = now + 2.0
        self.update()

    def _frac(self, pct: float) -> float:
        return _db_pos(pct) if self.db_scale else max(0.0, min(1.0, pct / 100.0))

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        pal = self.palette()
        w, h = self.width(), self.height()
        n = len(self.levels)
        gap = 2
        bar_w = max(4, (w - gap * (n - 1)) // n)
        seg_h = (h - 2) / SEGMENTS
        thr = self._frac(self.threshold)
        dark = pal.color(QPalette.Window).lightness() < 128
        off = QColor(255, 255, 255, 28) if dark else QColor(0, 0, 0, 22)
        below = QColor("#3a7bd5")
        above = QColor("#c2185b")
        clip_red = QColor("#e53935")
        clipping = time.monotonic() < self.clip_until
        for c in range(n):
            x = c * (bar_w + gap)
            lit = self._frac(self.levels[c])
            for s in range(SEGMENTS):
                frac_top = (s + 1) / SEGMENTS
                y = h - 1 - (s + 1) * seg_h
                r = QRectF(x, y + 1, bar_w, seg_h - 1.5)
                if s == SEGMENTS - 1 and clipping:
                    color = clip_red
                elif lit >= frac_top - 0.5 / SEGMENTS:
                    color = above if (s + 0.5) / SEGMENTS >= thr and self.show_threshold else below
                else:
                    color = off
                p.fillRect(r, color)
            hold = self._frac(self.holds[c])
            if hold > 0.02:
                y = h - 1 - hold * (h - 2)
                p.fillRect(QRectF(x, y, bar_w, 2), pal.color(QPalette.WindowText))
        if self.show_threshold:
            y = h - 1 - thr * (h - 2)
            pen = QPen(pal.color(QPalette.WindowText), 1, Qt.DashLine)
            p.setPen(pen)
            p.drawLine(0, int(y), w, int(y))
        p.end()


class PositionSlider(QWidget):
    """File position with selection band and segment ticks.

    Click or drag to seek, Shift-drag to select, hover for time and segment.
    """

    seekRequested = Signal(int)
    selectionRequested = Signal(int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.total = 0
        self.value = 0
        self.rate = 16000
        self.segments: list[int] = []
        self.selection: tuple[int, int] | None = None
        self.recording = False
        self._drag = False
        self._sel_anchor: int | None = None
        self.setMouseTracking(True)
        self.setMinimumHeight(24)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFocusPolicy(Qt.NoFocus)
        self.setAccessibleName("Position")

    def sizeHint(self) -> QSize:
        return QSize(200, 26)

    def set_state(self, total: int, value: int, rate: int, segments: list[int],
                  selection: tuple[int, int] | None, recording: bool) -> None:
        changed = (total, value, rate, segments, selection, recording) != (
            self.total, self.value, self.rate, self.segments, self.selection, self.recording)
        self.total, self.value, self.rate = total, value, rate
        self.segments, self.selection, self.recording = segments, selection, recording
        if changed:
            self.setEnabled(total > 0 and not recording)
            self.update()

    def _x(self, frame: int) -> float:
        m = 7
        if self.total <= 0:
            return m
        return m + (self.width() - 2 * m) * max(0.0, min(1.0, frame / self.total))

    def _frame(self, x: float) -> int:
        m = 7
        if self.total <= 0:
            return 0
        frac = (x - m) / max(1, self.width() - 2 * m)
        return int(max(0.0, min(1.0, frac)) * self.total)

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = self.palette()
        h = self.height()
        cy = h / 2
        groove = QRectF(7, cy - 2, self.width() - 14, 4)
        p.setPen(Qt.NoPen)
        p.setBrush(pal.color(QPalette.Mid))
        p.drawRoundedRect(groove, 2, 2)
        if self.selection and self.total:
            a, b = self.selection
            sel = QRectF(self._x(a), cy - 7, max(2.0, self._x(b) - self._x(a)), 14)
            c = QColor(pal.color(QPalette.Highlight))
            c.setAlpha(90)
            p.setBrush(c)
            p.drawRect(sel)
        if self.total:
            p.setPen(QPen(pal.color(QPalette.WindowText), 1))
            for s in self.segments[1:]:
                x = self._x(s)
                p.drawLine(int(x), int(cy + 4), int(x), int(cy + 9))
            # handle
            x = self._x(self.total if self.recording else self.value)
            hc = pal.color(QPalette.Highlight) if self.isEnabled() else pal.color(QPalette.Mid)
            p.setPen(QPen(pal.color(QPalette.Window), 1))
            p.setBrush(hc)
            p.drawRoundedRect(QRectF(x - 5, cy - 9, 10, 18), 3, 3)
        p.end()

    def mousePressEvent(self, ev) -> None:
        if not self.isEnabled() or ev.button() != Qt.LeftButton:
            return
        f = self._frame(ev.position().x())
        if ev.modifiers() & Qt.ShiftModifier:
            self._sel_anchor = f
            self.selectionRequested.emit(f, f)
        else:
            self._drag = True
            self.seekRequested.emit(f)

    def mouseMoveEvent(self, ev) -> None:
        f = self._frame(ev.position().x())
        if self._sel_anchor is not None:
            a, b = sorted((self._sel_anchor, f))
            self.selectionRequested.emit(a, b)
        elif self._drag:
            self.seekRequested.emit(f)
        if self.total and self.rate:
            seg = 1
            for i, s in enumerate(self.segments):
                if s <= f:
                    seg = i + 1
            QToolTip.showText(ev.globalPosition().toPoint() + QPoint(0, 16),
                              f"{fmt_time_precise(f / self.rate)}  •  segment {seg}", self)

    def mouseReleaseEvent(self, ev) -> None:
        self._drag = False
        self._sel_anchor = None
