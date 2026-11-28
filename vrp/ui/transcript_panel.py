"""Live transcript on screen (1.1.0): the transcript panel, the mini recorder bar,
the caption bar and the names list.

The panel draws only what the engine's TranscriptionService holds. Colours follow
the light or dark theme; "Me" is a filled blue tag and "Others" plain bold orange
text, so the two differ in shape and lightness, not only in hue (red/green colour
vision deficiency). Text still being recognised is in italics and ends with "…".
"""
from __future__ import annotations

import html
import os

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPalette
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QHeaderView,
                               QLabel, QPushButton, QSizePolicy, QTableWidget, QTableWidgetItem, QTextBrowser,
                               QToolButton, QVBoxLayout, QWidget)

from vrp.core.engine import CAPTURE, State
from vrp.core.model import fmt_duration, fmt_size
from vrp.transcribe import SPEAKERS, language_label, script_label
from vrp.transcribe.transcript import BOOKMARK, OFF, clock
from vrp.ui import icons
from vrp.ui.widgets import meter_colours


def is_dark(widget: QWidget) -> bool:
    return widget.palette().color(QPalette.Window).lightness() < 128


def colours(dark: bool) -> dict:
    """Colours of the live transcript; every text colour has at least 4.5:1 contrast on its background."""
    if dark:
        return {"text": "#f2f2f2", "muted": "#c4c4c4", "me_bg": "#3a8dde", "me_fg": "#0b1a2e",
                "others": "#f5b971", "bm_bg": "#1f3550", "off_bg": "#333333", "strip_bg": "#4a2800",
                "strip_fg": "#fcebd5", "accent": "#8ab4f8", "base": "#1e1e1e"}
    return {"text": "#1a1a1a", "muted": "#5c5c5c", "me_bg": "#0067c0", "me_fg": "#ffffff",
            "others": "#8a4b00", "bm_bg": "#eaf2fb", "off_bg": "#f0f0f0", "strip_bg": "#fcebd5",
            "strip_fg": "#4a2800", "accent": "#0067c0", "base": "#ffffff"}


def tag_html(speaker: str | None, c: dict) -> str:
    name = SPEAKERS.get(speaker or "")
    if not name:
        return ""
    if speaker == "me":
        return (f'<span style="background-color:{c["me_bg"]}; color:{c["me_fg"]}; font-weight:600;">'
                f'&nbsp;{name}&nbsp;</span>&nbsp; ')
    return f'<span style="color:{c["others"]}; font-weight:600;">{name}</span>&nbsp; '


def entry_html(e, c: dict) -> str:
    t = clock(e.wall)
    if e.kind == BOOKMARK:
        return (f'<table width="100%" cellspacing="0" cellpadding="4" style="background-color:{c["bm_bg"]};">'
                f'<tr><td><span style="color:{c["accent"]};">★</span>&nbsp; '
                f'<span style="color:{c["muted"]};">{t}</span>&nbsp; <b>Bookmark</b></td></tr></table>')
    if e.kind == OFF:
        return (f'<table width="100%" cellspacing="0" cellpadding="4" style="background-color:{c["off_bg"]};">'
                f'<tr><td><span style="color:{c["muted"]};">❚❚ Off the record {t} – '
                f'{clock(e.wall_end)} · nothing saved</span></td></tr></table>')
    return (f'<p style="margin-top:3px; margin-bottom:3px;"><span style="color:{c["muted"]};">{t}</span>'
            f'&nbsp;&nbsp; {tag_html(e.speaker, c)}{html.escape(e.text)}</p>')


def partial_html(partials: dict, c: dict) -> str:
    rows = []
    for sid in ("me", "others", "mix"):
        if sid in partials:
            text, wall = partials[sid]
            rows.append(f'<span style="color:{c["muted"]};">{clock(wall)}</span>&nbsp;&nbsp; '
                        f'{tag_html(None if sid == "mix" else sid, c)}'
                        f'<i><span style="color:{c["muted"]};">{html.escape(text)}…</span></i>')
    return "<br>".join(rows)


def rolling_text(service, limit_entries: int = 4) -> str:
    """The newest words as one line of plain text (for the recorder-only layout)."""
    parts = []
    for e in service.entries[-limit_entries:]:
        if e.kind == "text":
            who = SPEAKERS.get(e.speaker or "")
            parts.append(f"{who + ': ' if who else ''}{e.text}")
        elif e.kind == BOOKMARK:
            parts.append("★ Bookmark")
    for sid in ("me", "others", "mix"):
        if sid in service.partials:
            who = SPEAKERS.get(sid, "")
            parts.append(f"{who + ': ' if who else ''}{service.partials[sid][0]}…")
    return "   ".join(parts)


def save_text(settings) -> str:
    mode = settings.get("lt.save")
    if mode == "text_only":
        s = "Text only: no sound is being saved"
    elif mode == "text_check":
        s = f"Saving text, plus audio for checking (deleted after {settings.get('lt.keep_days')} days)"
    else:
        s = "Saving audio and text"
    if settings.get("lt.encrypt"):
        s += " · Windows encryption on"
    return s


class MiniMeter(QWidget):
    """A small horizontal level meter: the same colours as the main meters, laid on its side."""
    SEGMENTS = 10

    def __init__(self, name: str, parent=None):
        super().__init__(parent)
        self.level = 0.0
        self.threshold = 4.0
        self.setFixedSize(80, 12)
        self.setAccessibleName(name)

    def set_level(self, pct: float) -> None:
        self.level = pct if pct >= self.level else self.level * 0.7 + pct * 0.3
        self.setAccessibleDescription(f"{self.level:.0f} percent")
        self.update()

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        n = self.SEGMENTS
        gap = 2
        w = (self.width() - gap * (n - 1)) / n
        dark = is_dark(self)
        off = QColor(255, 255, 255, 40) if dark else QColor(0, 0, 0, 30)
        for i in range(n):
            frac = (i + 1) / n
            lit = self.level / 100.0 >= frac - 1.0 / n / 2
            mc = meter_colours(dark)
            colour = QColor(mc["below"] if frac * 100 <= max(self.threshold, 100.0 / n) else mc["above"]) \
                if lit else off
            p.fillRect(int(i * (w + gap)), 0, int(w), self.height(), colour)


class MiniRecorderBar(QFrame):
    """Recording status at the bottom of the transcript when the recorder panel is hidden."""

    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.setObjectName("miniRecorderBar")
        self.setFrameShape(QFrame.StyledPanel)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 3, 10, 3)
        lay.setSpacing(8)
        self.rec_btn = QToolButton()
        self.rec_btn.setAccessibleName("Record or pause")
        self.rec_btn.setToolTip("Record / pause (R)")
        self.stop_btn = QToolButton()
        self.stop_btn.setAccessibleName("Stop")
        self.stop_btn.setToolTip("Stop (S)")
        for b in (self.rec_btn, self.stop_btn):
            b.setIconSize(QSize(16, 16))
            b.setMinimumSize(34, 26)
            b.setFocusPolicy(Qt.NoFocus)
            lay.addWidget(b)
        self.rec_btn.clicked.connect(lambda: engine.record())
        self.stop_btn.clicked.connect(lambda: engine.stop())
        self.state = QLabel()
        self.state.setAccessibleName("Recording state")
        self.state.setMinimumWidth(92)
        f = self.state.font()
        f.setBold(True)
        self.state.setFont(f)
        lay.addWidget(self.state)
        self.mic = MiniMeter("Microphone level")
        self.sys = MiniMeter("Computer audio level")
        for label, m in (("Mic", self.mic), ("Computer", self.sys)):
            lay.addWidget(QLabel(label))
            lay.addWidget(m)
        self.info = QLabel()
        self.info.setAccessibleName("Size and segment")
        lay.addStretch()
        lay.addWidget(self.info)
        self.update_icons()

    def update_icons(self) -> None:
        ink = self.palette().windowText().color()
        self.rec_btn.setIcon(icons.transport_icon("record", ink))
        self.stop_btn.setIcon(icons.transport_icon("stop", ink))

    def tick(self) -> None:
        e = self.engine
        st = e.state
        f = e.file
        if st == State.RECORDING:
            label = "● REC"
        elif st == State.ARMED:
            label = "VOX waiting"
        elif st == State.REC_PAUSED:
            label = "Paused"
        else:
            label = "Stopped"
        dur = f" {fmt_duration(f.duration)}" if f is not None and st in CAPTURE else ""
        self.state.setText(label + dur)
        lv = e.level_percent()
        self.mic.set_level(max(lv) if lv else 0.0)
        sv = e.system_level_percent()
        self.sys.set_level(max(sv) if sv else 0.0)
        thr = e.settings.get("vox.threshold")
        self.mic.threshold = self.sys.threshold = thr
        if f is not None:
            n = len(f.segments)
            seg = f" · segment {n if st in CAPTURE else f.segment_index_at(e.position) + 1}" if n > 1 else ""
            self.info.setText(f"{fmt_size(f.size)}{seg}")
        else:
            self.info.setText("")
        self.rec_btn.setEnabled(st != State.SEEKING and not e.busy)
        self.stop_btn.setEnabled(st in CAPTURE)


class TranscriptPanel(QWidget):
    openInSot = Signal()
    turnOn = Signal()

    def __init__(self, engine, settings, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.settings = settings
        self._shown = 0
        self._session = None
        self._dark = None
        self._placeholder = None
        self._reordered = 0
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 4, 8, 6)
        lay.setSpacing(4)
        head = QHBoxLayout()
        self.title = QLabel("Live transcript")
        f = self.title.font()
        f.setPointSizeF(f.pointSizeF() * 1.15)
        f.setBold(True)
        self.title.setFont(f)
        head.addWidget(self.title)
        self.status = QLabel()
        self.status.setAccessibleName("Transcript status")
        head.addWidget(self.status)
        head.addStretch()
        self.lag = QLabel()
        self.lag.setAccessibleName("How far the text is behind")
        head.addWidget(self.lag)
        self.where = QLabel("On this PC only")
        self.where.setToolTip("Speech is recognised on this computer; nothing is sent over the internet.")
        head.addWidget(self.where)
        lay.addLayout(head)
        self.info = QLabel()
        self.info.setWordWrap(True)
        lay.addWidget(self.info)
        self.strip = QLabel()
        self.strip.setWordWrap(True)
        self.strip.setAccessibleName("What is being saved")
        self.strip.setContentsMargins(8, 4, 8, 4)
        lay.addWidget(self.strip)
        self.body = QTextBrowser()
        self.body.setAccessibleName("Live transcript")
        self.body.setOpenLinks(False)
        self.body.setMinimumHeight(140)
        self.body.setFocusPolicy(Qt.NoFocus)      # the arrow keys stay rewind / fast forward
        self.body.document().setMaximumBlockCount(20000)
        lay.addWidget(self.body, 1)
        self.provisional = QLabel()
        self.provisional.setWordWrap(True)
        self.provisional.setAccessibleName("Words still being recognised")
        self.provisional.setTextFormat(Qt.RichText)
        lay.addWidget(self.provisional)
        foot = QHBoxLayout()
        self.bookmark_btn = QPushButton("★ Bookmark")
        self.bookmark_btn.setToolTip("Mark this moment in the transcript")
        self.bookmark_btn.clicked.connect(lambda: engine.bookmark())
        self.off_btn = QPushButton("❚❚ Off the record")
        self.off_btn.setToolTip("Pause recording and transcript; resume to go back on the record")
        self.off_btn.clicked.connect(lambda: engine.record())
        self.sot_btn = QPushButton("Open in Transcriber\u2026")
        self.sot_btn.setToolTip("Open this recording in Secure Offline Transcriber: proofread, transcribe again "
                                "with a larger model, export to Word, summarise")
        self.sot_btn.clicked.connect(self.openInSot.emit)
        self.on_btn = QPushButton("Turn On Live Transcript…")
        self.on_btn.clicked.connect(self.turnOn.emit)
        for b in (self.bookmark_btn, self.off_btn, self.sot_btn, self.on_btn):
            b.setFocusPolicy(Qt.NoFocus)          # keep Space and R for the transport; all are in the Transcript menu
            foot.addWidget(b)
        foot.addStretch()
        lay.addLayout(foot)
        self.mini = MiniRecorderBar(engine)
        self.mini.hide()
        lay.addWidget(self.mini)
        self.refresh()

    # ------------------------------------------------------------------
    def set_mini_bar(self, on: bool) -> None:
        self.mini.setVisible(on)

    def _apply_colours(self) -> dict:
        dark = is_dark(self)
        c = colours(dark)
        if dark != self._dark:
            self._dark = dark
            self.strip.setStyleSheet(f"background-color: {c['strip_bg']}; color: {c['strip_fg']};")
            self.body.setStyleSheet(f"QTextBrowser {{ background-color: {c['base']}; color: {c['text']}; }}")
            self.status.setStyleSheet(f"color: {c['accent']}; border: 1px solid {c['accent']}; "
                                      "border-radius: 9px; padding: 0px 8px;")
            for w in (self.info, self.lag, self.where):
                w.setStyleSheet(f"color: {c['muted']};")
            self._shown = 0                    # redraw everything in the new colours
            self._placeholder = None
            self.mini.update_icons()
        return c

    def refresh(self) -> None:
        e = self.engine
        sv = e.transcriber
        s = self.settings
        c = self._apply_colours()
        on = bool(s.get("lt.enabled"))
        pack = e.lt_pack_status()
        cap = e.state in CAPTURE
        if not on:
            status = "Off"
        elif not pack.ok:
            status = "Not available"
        elif e.lt_active and sv.off_record:
            status = "Off the record"
        elif e.lt_active:
            status = "Transcribing" if (sv.worker and sv.worker.ready) else "Starting…"
        else:
            status = "Ready" if not cap else "Not transcribing"
        self.status.setText(status)
        self.lag.setText(f"Text {sv.lag:.1f} s behind" if e.lt_active and sv.lag >= 1.0 else "")
        if not on:
            self.info.setText("The live transcript is off. Turn it on to see the words as they are spoken.")
        elif not pack.ok:
            self.info.setText(pack.reason)
        else:
            split = e.fmt().split
            labels = "Labels by channel: Me = microphone, Others = computer audio" if split else \
                "One mixed stream (choose Format > Channels > Separate for Me / Others labels)"
            self.info.setText(f"{language_label(s.get('lt.language'))} · {script_label(s.get('lt.script'))}"
                              f" · {labels}")
        self.strip.setText(save_text(s) if on else "")
        self.strip.setVisible(on and pack.ok)
        self._update_body(sv, c, on)
        ptxt = partial_html(sv.partials, c) if e.lt_active else ""
        self.provisional.setText(ptxt)
        self.provisional.setVisible(bool(ptxt))
        self.refresh_buttons()

    def _update_body(self, sv, c: dict, on: bool) -> None:
        """Append the entries added since the last call; start again for a new recording or theme."""
        if self._session != sv.started_at or self._shown > sv.added or self._reordered != sv.reordered:
            self._session = sv.started_at
            self._reordered = sv.reordered
            self._shown = 0
            self._placeholder = None
        if not sv.entries:
            want = "Words appear here while you record." if on else ""
            if self._placeholder != want:
                self.body.setHtml(f'<p style="color:{c["muted"]};">{want}</p>')
                self._placeholder = want
            return
        new = sv.added - self._shown
        if new <= 0:
            return
        bar = self.body.verticalScrollBar()
        at_end = bar.value() >= bar.maximum() - 4
        if self._shown == 0 or self._placeholder is not None or new > len(sv.entries):
            self.body.clear()
            self._placeholder = None
            new = len(sv.entries)
            at_end = True
        for ent in sv.entries[-new:]:
            self.body.append(entry_html(ent, c))
        self._shown = sv.added
        if at_end:
            bar.setValue(bar.maximum())

    def refresh_buttons(self) -> None:
        e = self.engine
        on = bool(self.settings.get("lt.enabled"))
        active = e.lt_active
        self.bookmark_btn.setEnabled(active and e.state in (State.RECORDING, State.ARMED))
        self.off_btn.setEnabled(active)
        self.off_btn.setText("▶ Back on the record" if e.state == State.REC_PAUSED else
                             "❚❚ Off the record")
        f = e.file
        self.sot_btn.setEnabled(f is not None and not f.recording and e.state not in CAPTURE and
                                os.path.exists(f.path))
        self.on_btn.setVisible(not on)
        for b in (self.bookmark_btn, self.off_btn, self.sot_btn):
            b.setVisible(on)

    def on_tick(self) -> None:
        if self.mini.isVisible():
            self.mini.tick()


class CaptionBar(QWidget):
    """A small always-on-top window with the newest lines, for use over Teams or Zoom."""
    closed = Signal()

    def __init__(self, engine, settings, parent=None):
        super().__init__(parent, Qt.Tool | Qt.WindowStaysOnTopHint)
        self.engine = engine
        self.settings = settings
        self.setWindowTitle("Live captions - Voice Recorder Pro")
        self.setAccessibleName("Live captions")
        self.setStyleSheet("CaptionBar, QLabel, QToolButton { background-color: #202020; color: #ffffff; }"
                           "QToolButton { border: 0; padding: 4px 8px; }"
                           "QToolButton:checked { background-color: #3d3d3d; }")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 10)
        top = QHBoxLayout()
        self.head = QLabel()
        self.head.setStyleSheet("color: #c8c8c8;")
        top.addWidget(self.head)
        top.addStretch()
        self.smaller = QToolButton()
        self.smaller.setText("A−")
        self.smaller.setAccessibleName("Smaller text")
        self.larger = QToolButton()
        self.larger.setText("A+")
        self.larger.setAccessibleName("Larger text")
        self.pin = QToolButton()
        self.pin.setText("Keep on top")
        self.pin.setCheckable(True)
        self.pin.setChecked(True)
        self.pin.setAccessibleName("Keep the captions on top")
        self.off = QToolButton()
        self.off.setText("❚❚ Off the record")
        self.off.setAccessibleName("Off the record")
        for b in (self.smaller, self.larger, self.pin, self.off):
            top.addWidget(b)
        lay.addLayout(top)
        self.lines = QLabel()
        self.lines.setWordWrap(True)
        self.lines.setTextFormat(Qt.RichText)
        self.lines.setAlignment(Qt.AlignLeft | Qt.AlignBottom)
        self.lines.setAccessibleName("Newest captions")
        self.lines.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        lay.addWidget(self.lines, 1)
        self.smaller.clicked.connect(lambda: self._size(-2))
        self.larger.clicked.connect(lambda: self._size(2))
        self.pin.toggled.connect(self._pin)
        self.off.clicked.connect(lambda: engine.record())
        self.resize(820, 190)
        geo = settings.get("ui.caption_geometry")
        if geo:
            try:
                x, y, w, h = (int(v) for v in geo.split(","))
                self.setGeometry(x, y, w, h)
            except ValueError:
                pass
        self.refresh()

    def _size(self, d: int) -> None:
        self.settings.set("ui.caption_size", self.settings.get("ui.caption_size") + d)
        self.refresh()

    def _pin(self, on: bool) -> None:
        vis = self.isVisible()
        self.setWindowFlag(Qt.WindowStaysOnTopHint, on)
        if vis:
            self.show()

    def refresh(self) -> None:
        e = self.engine
        sv = e.transcriber
        c = colours(True)
        size = self.settings.get("ui.caption_size")
        self.head.setText(f"Voice Recorder Pro · Live captions · "
                          f"{language_label(self.settings.get('lt.language'))}"
                          + (f" · Text {sv.lag:.1f} s behind" if e.lt_active and sv.lag >= 1.0 else ""))
        rows = []
        texts = [x for x in sv.entries if x.kind == "text"][-2:]
        for x in texts:
            rows.append(f"{tag_html(x.speaker, c)}{html.escape(x.text)}")
        for sid in ("me", "others", "mix"):
            if sid in sv.partials and e.lt_active:
                rows.append(f"{tag_html(None if sid == 'mix' else sid, c)}<i><span style=\"color:{c['muted']};\">"
                            f"{html.escape(sv.partials[sid][0])}…</span></i>")
        if not rows:
            rows = [f'<span style="color:{c["muted"]};">'
                    + ("Waiting for speech…" if e.lt_active else "Captions appear here while you record "
                       "with the live transcript on.") + "</span>"]
        self.lines.setText(f'<div style="font-size:{size}px; line-height:130%;">' + "<br>".join(rows[-3:])
                           + "</div>")
        self.off.setText("▶ Back on the record" if e.state == State.REC_PAUSED else "❚❚ Off the record")
        self.off.setEnabled(e.lt_active)

    def closeEvent(self, ev) -> None:
        g = self.geometry()
        self.settings.set("ui.caption_geometry", f"{g.x()},{g.y()},{g.width()},{g.height()}")
        super().closeEvent(ev)
        self.closed.emit()


class NamesDialog(QDialog):
    """The names list: words the recogniser gets wrong, and how to write them."""

    def __init__(self, names: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Names and terms")
        self.resize(520, 420)
        lay = QVBoxLayout(self)
        note = QLabel("Each phrase in the left column is replaced by the right column in the live transcript, "
                      "for example names of people, companies or products. English is matched as whole words, "
                      "in any capitals; Chinese anywhere.")
        note.setWordWrap(True)
        lay.addWidget(note)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Heard as", "Write as"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAccessibleName("Names and terms")
        lay.addWidget(self.table, 1)
        row = QHBoxLayout()
        add = QPushButton("&Add")
        rem = QPushButton("&Remove")
        add.clicked.connect(lambda: self._add("", ""))
        rem.clicked.connect(self._remove)
        row.addWidget(add)
        row.addWidget(rem)
        row.addStretch()
        lay.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        for n in names or []:
            if isinstance(n, dict):
                self._add(str(n.get("heard", "")), str(n.get("correct", "")))

    def _add(self, heard: str, correct: str) -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.table.setItem(r, 0, QTableWidgetItem(heard))
        self.table.setItem(r, 1, QTableWidgetItem(correct))
        if not heard:
            self.table.setCurrentCell(r, 0)
            self.table.editItem(self.table.item(r, 0))

    def _remove(self) -> None:
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.table.removeRow(r)

    def names(self) -> list:
        out = []
        for r in range(self.table.rowCount()):
            h = (self.table.item(r, 0).text() if self.table.item(r, 0) else "").strip()
            c = self.table.item(r, 1).text() if self.table.item(r, 1) else ""
            if h:
                out.append({"heard": h[:200], "correct": c[:200]})
        return out[:500]


def elide_left(widget: QWidget, text: str, width: int) -> str:
    return QFontMetrics(widget.font()).elidedText(text, Qt.ElideLeft, max(40, width))


__all__ = ["TranscriptPanel", "MiniRecorderBar", "MiniMeter", "CaptionBar", "NamesDialog", "colours",
           "rolling_text", "elide_left", "entry_html"]
