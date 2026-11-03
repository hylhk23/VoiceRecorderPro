"""Smaller dialogs: timed recording, schedules, properties, recovery, join, formats, calibration."""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta

from PySide6.QtCore import QDateTime, Qt, QTime, QTimer
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDateTimeEdit, QDialog,
                               QDialogButtonBox, QFileDialog, QFormLayout, QGridLayout,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QMessageBox,
                               QPushButton, QRadioButton, QSpinBox, QTableWidget, QTableWidgetItem,
                               QTimeEdit, QVBoxLayout, QWidget)

from vrp import APP_NAME, BUILD_DATE, PUBLISHER, WEBSITE, __version__
from vrp.core import scheduler
from vrp.core.model import (FLAC_RATES, MP3_RATES, WAV_RATES, WAV_SUBTYPES, FileModel, FormatSpec,
                            fmt_duration, fmt_rate, fmt_size, fmt_time_precise, mp3_kbps_choices)
from vrp.settings import HOTKEY_ACTIONS


# ================================================================== format form
class FormatForm(QWidget):
    """Codec, sample rate, channels and quality, with an estimate of file size."""

    def __init__(self, parent=None):
        super().__init__(parent)
        f = QFormLayout(self)
        f.setContentsMargins(0, 0, 0, 0)
        self.codec = QComboBox()
        self.codec.addItem("MP3 (small files)", "mp3")
        self.codec.addItem("WAV (uncompressed or telephone)", "wav")
        self.codec.addItem("FLAC (lossless, compressed)", "flac")
        self.rate = QComboBox()
        self.channels = QComboBox()
        self.channels.addItem("Mono", 1)
        self.channels.addItem("Stereo", 2)
        self.mp3_mode = QComboBox()
        self.mp3_mode.addItem("Constant bitrate (CBR)", "cbr")
        self.mp3_mode.addItem("Variable bitrate (VBR)", "vbr")
        self.kbps = QComboBox()
        self.vbr_q = QSpinBox()
        self.vbr_q.setRange(0, 9)
        self.vbr_q.setPrefix("V")
        self.vbr_q.setToolTip("0 = best quality and largest files, 9 = smallest files")
        self.subtype = QComboBox()
        for k, v in WAV_SUBTYPES.items():
            self.subtype.addItem(v, k)
        self.flac_bits = QComboBox()
        self.flac_bits.addItem("16-bit", 16)
        self.flac_bits.addItem("24-bit", 24)
        self.flac_level = QSpinBox()
        self.flac_level.setRange(0, 8)
        self.estimate = QLabel()
        self.estimate.setStyleSheet("color: palette(mid);")
        f.addRow("Format:", self.codec)
        f.addRow("Sample rate:", self.rate)
        f.addRow("Channels:", self.channels)
        self._rows = {}
        for label, w, key in (("MP3 mode:", self.mp3_mode, "mp3"), ("Bitrate:", self.kbps, "mp3cbr"),
                              ("VBR quality:", self.vbr_q, "mp3vbr"), ("Encoding:", self.subtype, "wav"),
                              ("Bit depth:", self.flac_bits, "flac"), ("Compression:", self.flac_level, "flac")):
            lab = QLabel(label)
            f.addRow(lab, w)
            self._rows.setdefault(key, []).append((lab, w))
        f.addRow("", self.estimate)
        for w in (self.codec, self.rate, self.channels, self.mp3_mode, self.kbps, self.subtype, self.flac_bits):
            w.currentIndexChanged.connect(self._refresh)
        self.vbr_q.valueChanged.connect(self._refresh)
        self.flac_level.valueChanged.connect(self._refresh)
        self._busy = False

    def _fill_rates(self, codec: str, keep: int) -> None:
        rates = {"mp3": MP3_RATES, "wav": WAV_RATES, "flac": FLAC_RATES}[codec]
        if codec == "wav" and self.subtype.currentData() in ("ULAW", "ALAW"):
            rates = (8000, 11025, 16000)
        self.rate.blockSignals(True)
        self.rate.clear()
        for r in rates:
            self.rate.addItem(fmt_rate(r), r)
        idx = self.rate.findData(keep)
        if idx < 0:
            idx = min(range(len(rates)), key=lambda i: abs(rates[i] - keep))
        self.rate.setCurrentIndex(idx)
        self.rate.blockSignals(False)

    def _fill_kbps(self, keep: int) -> None:
        rate = self.rate.currentData() or 16000
        self.kbps.blockSignals(True)
        self.kbps.clear()
        choices = mp3_kbps_choices(rate)
        for k in choices:
            self.kbps.addItem(f"{k} kbit/s", k)
        idx = self.kbps.findData(keep)
        if idx < 0:
            idx = min(range(len(choices)), key=lambda i: abs(choices[i] - keep))
        self.kbps.setCurrentIndex(idx)
        self.kbps.blockSignals(False)

    def _refresh(self) -> None:
        if self._busy:
            return
        self._busy = True
        codec = self.codec.currentData()
        self._fill_rates(codec, self.rate.currentData() or 16000)
        self._fill_kbps(self.kbps.currentData() or 32)
        vis = {"mp3": codec == "mp3", "mp3cbr": codec == "mp3" and self.mp3_mode.currentData() == "cbr",
               "mp3vbr": codec == "mp3" and self.mp3_mode.currentData() == "vbr",
               "wav": codec == "wav", "flac": codec == "flac"}
        for key, rows in self._rows.items():
            for lab, w in rows:
                lab.setVisible(vis[key])
                w.setVisible(vis[key])
        spec = self.spec()
        mb_min = spec.bytes_per_second() * 60 / 1e6
        self.estimate.setText(f"About {mb_min:.2f} MB per minute")
        self._busy = False

    def set_spec(self, f: FormatSpec) -> None:
        self._busy = True
        self.codec.setCurrentIndex(max(0, self.codec.findData(f.codec)))
        self.subtype.setCurrentIndex(max(0, self.subtype.findData(f.wav_subtype)))
        self._fill_rates(f.codec, f.rate)
        self.channels.setCurrentIndex(0 if f.channels == 1 else 1)
        self.mp3_mode.setCurrentIndex(max(0, self.mp3_mode.findData(f.mp3_mode)))
        self._fill_kbps(f.mp3_kbps)
        self.vbr_q.setValue(f.mp3_vbr_q)
        self.flac_bits.setCurrentIndex(0 if f.flac_bits == 16 else 1)
        self.flac_level.setValue(f.flac_level)
        self._busy = False
        self._refresh()

    def spec(self) -> FormatSpec:
        return FormatSpec(codec=self.codec.currentData(), rate=self.rate.currentData() or 16000,
                          channels=self.channels.currentData() or 1, wav_subtype=self.subtype.currentData(),
                          flac_bits=self.flac_bits.currentData() or 16, flac_level=self.flac_level.value(),
                          mp3_mode=self.mp3_mode.currentData(), mp3_kbps=self.kbps.currentData() or 32,
                          mp3_vbr_q=self.vbr_q.value()).normalised()


class FormatDialog(QDialog):
    def __init__(self, spec: FormatSpec, title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        lay = QVBoxLayout(self)
        self.form = FormatForm()
        self.form.set_spec(spec)
        lay.addWidget(self.form)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


# ============================================================== timed recording
class TimedDialog(QDialog):
    def __init__(self, parent=None, seconds: int = 3600):
        super().__init__(parent)
        self.setWindowTitle("Timed Recording")
        lay = QFormLayout(self)
        self.dur = QTimeEdit()
        self.dur.setDisplayFormat("H:mm:ss")
        self.dur.setTime(QTime(0, 0).addSecs(seconds))
        self.dur.setMinimumTime(QTime(0, 0, 1))
        lay.addRow("Record for (h:mm:ss):", self.dur)
        lay.addRow(QLabel("Recording starts now and stops automatically."))
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Start")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addRow(bb)

    def seconds(self) -> int:
        return QTime(0, 0).secsTo(self.dur.time())


# ===================================================================== schedules
class ScheduleEntryDialog(QDialog):
    def __init__(self, entry: scheduler.Entry, devices: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Scheduled Recording")
        self.entry = entry
        f = QFormLayout(self)
        self.name = QLineEdit(entry.name)
        self.start = QDateTimeEdit()
        self.start.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.start.setCalendarPopup(True)
        try:
            st = entry.start_dt
        except (ValueError, TypeError):
            st = (datetime.now() + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        self.start.setDateTime(QDateTime(st))
        self.by_duration = QRadioButton("Duration")
        self.by_end = QRadioButton("End time")
        self.by_duration.setChecked(True)
        self.dur = QTimeEdit()
        self.dur.setDisplayFormat("H:mm:ss")
        self.dur.setTime(QTime(0, 0).addSecs(min(entry.duration_s, 86399)))
        self.end = QTimeEdit()
        self.end.setDisplayFormat("HH:mm")
        self.end.setTime(QTime(st.hour, st.minute).addSecs(entry.duration_s))
        end_row = QGridLayout()
        end_row.addWidget(self.by_duration, 0, 0)
        end_row.addWidget(self.dur, 0, 1)
        end_row.addWidget(self.by_end, 1, 0)
        end_row.addWidget(self.end, 1, 1)
        self.repeat = QComboBox()
        for k, v in scheduler.REPEATS.items():
            self.repeat.addItem(v, k)
        self.repeat.setCurrentIndex(max(0, self.repeat.findData(entry.repeat)))
        days = QHBoxLayout()
        self.day_boxes = []
        for i, d in enumerate(scheduler.DAY_NAMES):
            cb = QCheckBox(d)
            cb.setChecked(i in (entry.days or []))
            days.addWidget(cb)
            self.day_boxes.append(cb)
        self.days_widget = QWidget()
        self.days_widget.setLayout(days)
        self.vox = QComboBox()
        for k, v in (("current", "Use current setting"), ("on", "On"), ("off", "Off (record continuously)")):
            self.vox.addItem(v, k)
        self.vox.setCurrentIndex(max(0, self.vox.findData(entry.vox)))
        self.device = QComboBox()
        self.device.addItem("Use current input device", "")
        for d in devices:
            self.device.addItem(d, d)
        self.device.setCurrentIndex(max(0, self.device.findData(entry.device)))
        self.codec = QComboBox()
        for k, v in (("", "Use current format"), ("mp3", "MP3"), ("wav", "WAV"), ("flac", "FLAC")):
            self.codec.addItem(v, k)
        self.codec.setCurrentIndex(max(0, self.codec.findData(entry.codec)))
        self.pattern = QLineEdit(entry.pattern)
        self.pattern.setPlaceholderText("Use current naming pattern (e.g. {Name}_{yyyy}{mm}{dd})")
        self.take_over = QCheckBox("Take over: finish a manual recording and start this one")
        self.take_over.setChecked(entry.take_over)
        self.enabled = QCheckBox("Enabled")
        self.enabled.setChecked(entry.enabled)
        f.addRow("Name:", self.name)
        f.addRow("Start:", self.start)
        f.addRow("End:", end_row)
        f.addRow("Repeat:", self.repeat)
        f.addRow("", self.days_widget)
        f.addRow("Vox:", self.vox)
        f.addRow("Input device:", self.device)
        f.addRow("Format:", self.codec)
        f.addRow("File names:", self.pattern)
        f.addRow("", self.take_over)
        f.addRow("", self.enabled)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)
        self.repeat.currentIndexChanged.connect(self._sync)
        self.by_duration.toggled.connect(self._sync)
        self._sync()

    def _sync(self) -> None:
        self.days_widget.setVisible(self.repeat.currentData() == "weekly")
        self.dur.setEnabled(self.by_duration.isChecked())
        self.end.setEnabled(not self.by_duration.isChecked())

    def result_entry(self) -> scheduler.Entry:
        e = self.entry
        st = self.start.dateTime().toPython()
        e.name = self.name.text().strip() or "Schedule"
        e.start = st.replace(second=0, microsecond=0).isoformat(timespec="minutes")
        if self.by_duration.isChecked():
            e.duration_s = max(1, QTime(0, 0).secsTo(self.dur.time()))
        else:
            secs = QTime(st.hour, st.minute).secsTo(self.end.time())
            e.duration_s = secs if secs > 0 else secs + 86400
        e.repeat = self.repeat.currentData()
        e.days = [i for i, cb in enumerate(self.day_boxes) if cb.isChecked()]
        e.vox = self.vox.currentData()
        e.device = self.device.currentData()
        e.codec = self.codec.currentData()
        e.pattern = self.pattern.text().strip()
        e.take_over = self.take_over.isChecked()
        e.enabled = self.enabled.isChecked()
        return e


class ScheduleDialog(QDialog):
    """Control > Schedule (FR-SCHED-002)."""

    def __init__(self, settings, devices: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Scheduled Recordings")
        self.resize(640, 320)
        self.settings = settings
        self.devices = devices
        self.entries = [scheduler.Entry.from_json(d) for d in settings.get("sched.entries")]
        lay = QVBoxLayout(self)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Name", "Next run", "Repeat and length", "Enabled"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.doubleClicked.connect(self._edit)
        lay.addWidget(self.table)
        btns = QHBoxLayout()
        for text, fn in (("Add…", self._add), ("Edit…", self._edit), ("Duplicate", self._dup),
                         ("Enable / Disable", self._toggle), ("Delete", self._delete)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            btns.addWidget(b)
        btns.addStretch()
        lay.addLayout(btns)
        note = QLabel("Schedules run while the app is open or in the tray. If the computer was off or asleep, "
                      f"a recording starts late when no more than {settings.get('sched.late_minutes')} min "
                      "have passed.")
        note.setWordWrap(True)
        lay.addWidget(note)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.accept)
        lay.addWidget(bb)
        self._fill()

    def _fill(self) -> None:
        self.table.setRowCount(len(self.entries))
        now = datetime.now()
        for r, e in enumerate(self.entries):
            nxt = scheduler.next_occurrence(e, now) if e.enabled else None
            vals = [e.name, nxt.strftime("%a %Y-%m-%d %H:%M") if nxt else "—", e.describe(),
                    "Yes" if e.enabled else "No"]
            for c, v in enumerate(vals):
                self.table.setItem(r, c, QTableWidgetItem(v))

    def _save(self) -> None:
        self.settings.set("sched.entries", [e.to_json() for e in self.entries])
        self._fill()

    def _row(self) -> int:
        rows = self.table.selectionModel().selectedRows()
        return rows[0].row() if rows else -1

    def _edit_entry(self, e: scheduler.Entry, is_new: bool) -> None:
        while True:
            d = ScheduleEntryDialog(e, self.devices, self)
            if d.exec() != QDialog.Accepted:
                return
            e = d.result_entry()
            others = [x for x in self.entries if x.id != e.id]
            clash = scheduler.find_overlap(others, e)
            if clash is None:
                break
            QMessageBox.warning(self, "Overlapping schedule",
                                f"This schedule overlaps “{clash.name}” ({clash.describe()}). "
                                "Change the time or disable one of them.")
        if is_new:
            self.entries.append(e)
        self._save()

    def _add(self) -> None:
        self._edit_entry(scheduler.Entry(name=f"Schedule {len(self.entries) + 1}"), True)

    def _edit(self) -> None:
        r = self._row()
        if r >= 0:
            self._edit_entry(self.entries[r], False)

    def _dup(self) -> None:
        r = self._row()
        if r >= 0:
            d = scheduler.Entry.from_json(self.entries[r].to_json())
            d.id = scheduler.Entry().id
            d.name += " (copy)"
            d.enabled = False
            self.entries.append(d)
            self._save()

    def _toggle(self) -> None:
        r = self._row()
        if r < 0:
            return
        e = self.entries[r]
        if not e.enabled:
            e.enabled = True
            clash = scheduler.find_overlap([x for x in self.entries if x.id != e.id], e)
            if clash is not None:
                e.enabled = False
                QMessageBox.warning(self, "Overlapping schedule", f"It overlaps “{clash.name}”.")
                return
        else:
            e.enabled = False
        self._save()

    def _delete(self) -> None:
        r = self._row()
        if r >= 0 and QMessageBox.question(self, "Delete schedule",
                                           f"Delete “{self.entries[r].name}”?") == QMessageBox.Yes:
            del self.entries[r]
            self._save()


# ==================================================================== properties
class PropertiesDialog(QDialog):
    def __init__(self, m: FileModel, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Properties — {os.path.basename(m.path)}")
        self.resize(560, 420)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        fmt = {"mp3": "MP3", "wav": "WAV", "flac": "FLAC"}.get(m.codec, m.codec.upper())
        detail = f"{fmt}, {m.sample_text()}"
        if m.codec == "mp3" and m.kbps:
            detail += f", about {m.kbps} kbit/s"
        elif m.subtype:
            detail += f", {m.subtype}"
        for label, value in (("File:", m.path), ("Format:", detail), ("Duration:", fmt_time_precise(m.duration)),
                             ("Size:", f"{fmt_size(m.size)} ({m.size:,} bytes)"),
                             ("Segments:", str(len(m.segments))), ("Title:", m.title or "—"),
                             ("Comment:", m.comment or "—")):
            v = QLabel(value)
            v.setTextInteractionFlags(Qt.TextSelectableByMouse)
            v.setWordWrap(True)
            form.addRow(label, v)
        lay.addLayout(form)
        table = QTableWidget(len(m.segments), 4)
        table.setHorizontalHeaderLabels(["#", "Start", "Length", "Time stamp"])
        table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        starts = m.segment_starts() + [m.frames]
        for i, s in enumerate(m.segments):
            vals = [str(i + 1), fmt_time_precise(s.start / m.rate),
                    fmt_time_precise((starts[i + 1] - s.start) / m.rate), s.timestamp or ""]
            for c, v in enumerate(vals):
                table.setItem(i, c, QTableWidgetItem(v))
        lay.addWidget(table)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.accept)
        lay.addWidget(bb)


# ====================================================================== recovery
class RecoveryDialog(QDialog):
    """Offered at start-up after an unclean exit (FR-REC-006, TC-06)."""

    def __init__(self, pending, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Recover recordings")
        self.pending = pending
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Voice Recorder Pro did not close normally. These recordings were still open and "
                             "can be repaired:"))
        self.list = QListWidget()
        for p in pending:
            self.list.addItem(f"{p.audio}   ({fmt_size(p.size)}, about {fmt_duration(p.seconds)})")
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list.selectAll()
        lay.addWidget(self.list)
        bb = QDialogButtonBox()
        self.recover_btn = bb.addButton("Recover selected", QDialogButtonBox.AcceptRole)
        self.discard_btn = bb.addButton("Leave as is", QDialogButtonBox.DestructiveRole)
        later = bb.addButton("Ask me later", QDialogButtonBox.RejectRole)
        self.recover_btn.clicked.connect(self.accept)
        self.discard_btn.clicked.connect(lambda: self.done(2))
        later.clicked.connect(self.reject)
        lay.addWidget(bb)

    def selected(self):
        return [self.pending[i.row()] for i in self.list.selectedIndexes()]


# ========================================================================= join
class JoinDialog(QDialog):
    def __init__(self, start_dir: str, first: str | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Join Files")
        self.resize(560, 360)
        self.start_dir = start_dir
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Files are joined in this order:"))
        row = QHBoxLayout()
        self.list = QListWidget()
        if first:
            self.list.addItem(first)
        row.addWidget(self.list)
        col = QVBoxLayout()
        for text, fn in (("Add…", self._add), ("Remove", self._remove), ("Up", lambda: self._move(-1)),
                         ("Down", lambda: self._move(1))):
            b = QPushButton(text)
            b.clicked.connect(fn)
            col.addWidget(b)
        col.addStretch()
        row.addLayout(col)
        lay.addLayout(row)
        out = QHBoxLayout()
        self.out = QLineEdit()
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        out.addWidget(QLabel("Save as:"))
        out.addWidget(self.out)
        out.addWidget(browse)
        lay.addLayout(out)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Join")
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._suggest()

    def files(self) -> list[str]:
        return [self.list.item(i).text() for i in range(self.list.count())]

    def _suggest(self) -> None:
        f = self.files()
        if f and not self.out.text():
            base, ext = os.path.splitext(f[0])
            self.out.setText(f"{base}_joined{ext}")

    def _add(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Add files", self.start_dir,
                                                "Audio files (*.mp3 *.wav *.flac);;All files (*)")
        for p in paths:
            self.list.addItem(p)
        self._suggest()

    def _remove(self) -> None:
        for it in self.list.selectedItems():
            self.list.takeItem(self.list.row(it))

    def _move(self, d: int) -> None:
        r = self.list.currentRow()
        if r < 0 or not (0 <= r + d < self.list.count()):
            return
        it = self.list.takeItem(r)
        self.list.insertItem(r + d, it)
        self.list.setCurrentRow(r + d)

    def _browse(self) -> None:
        p, _ = QFileDialog.getSaveFileName(self, "Save joined file as", self.out.text() or self.start_dir,
                                           "Audio files (*.mp3 *.wav *.flac)")
        if p:
            self.out.setText(p)

    def _ok(self) -> None:
        if len(self.files()) < 2:
            QMessageBox.information(self, "Join Files", "Add at least two files.")
            return
        if not self.out.text().strip():
            QMessageBox.information(self, "Join Files", "Choose where to save the joined file.")
            return
        if os.path.exists(self.out.text()):
            if QMessageBox.question(self, "Join Files", "That file exists. Replace it?") != QMessageBox.Yes:
                return
        self.accept()


# ================================================================== calibration
class CalibrateDialog(QDialog):
    """Measure 5 s of background noise and suggest a threshold (FR-VOX-008)."""

    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.setWindowTitle("Calibrate Vox")
        lay = QVBoxLayout(self)
        self.info = QLabel("Stay quiet for 5 seconds while the background noise is measured.")
        self.info.setWordWrap(True)
        lay.addWidget(self.info)
        self.result_label = QLabel("")
        lay.addWidget(self.result_label)
        bb = QDialogButtonBox()
        self.start_btn = bb.addButton("Measure", QDialogButtonBox.ActionRole)
        self.apply_btn = bb.addButton("Apply", QDialogButtonBox.AcceptRole)
        self.apply_btn.setEnabled(False)
        bb.addButton(QDialogButtonBox.Cancel)
        bb.rejected.connect(self.reject)
        self.apply_btn.clicked.connect(self.accept)
        self.start_btn.clicked.connect(self._start)
        lay.addWidget(bb)
        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self._sample)
        self.peak = 0.0
        self.t0 = 0.0
        self.value = None
        self._opened = False

    def _start(self) -> None:
        if not self.engine.recorder.input_open:
            self.engine.monitor(True)
            self._opened = True
        if not self.engine.recorder.input_open:
            self.result_label.setText("The microphone could not be opened.")
            return
        self.peak = 0.0
        self.t0 = time.monotonic()
        self.start_btn.setEnabled(False)
        self.timer.start()

    def _sample(self) -> None:
        lv = self.engine.recorder.levels
        if lv.size:
            self.peak = max(self.peak, float(lv.max()))
        left = 5 - (time.monotonic() - self.t0)
        self.info.setText(f"Measuring… {max(0, left):.0f} s")
        if left <= 0:
            self.timer.stop()
            self.start_btn.setEnabled(True)
            noise_pct = self.peak * 100
            thr = min(100, max(1, round(noise_pct * 2.0)))      # + 6 dB
            self.value = int(thr)
            self.info.setText("Measurement finished.")
            self.result_label.setText(f"Background noise peak: {noise_pct:.1f}.  Suggested threshold: {thr} "
                                      "(noise + 6 dB).")
            self.apply_btn.setEnabled(True)

    def done(self, r) -> None:
        self.timer.stop()
        if self._opened and not self.engine.capturing:
            self.engine.monitor(False)
        super().done(r)


# ===================================================================== shortcuts
WINDOW_SHORTCUTS = [
    ("R", "Record / pause recording"), ("Space", "Play / pause"), ("S", "Stop"),
    ("Left (hold)", "Rewind (tap jumps, hold scans)"), ("Right (hold)", "Fast forward"),
    ("Ctrl+Left / Ctrl+Right", "Skip back / forward"), ("PgUp / PgDn", "Previous / next segment"),
    ("Home / End", "Go to start / end"), ("[ / ]", "Set In / Out mark"), ("Esc", "Clear selection"),
    ("M", "Insert segment boundary"), ("Ctrl+E", "Extract selection to file"), ("Del", "Delete selection"),
    ("Alt+V", "Vox on/off"), ("Ctrl+M", "Monitor input"), ("Ctrl+N", "New recording"),
    ("Ctrl+O", "Open"), ("Ctrl+,", "All settings"), ("F1", "Help"),
]


class ShortcutsDialog(QDialog):
    def __init__(self, settings, status: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Keyboard Shortcuts")
        self.resize(560, 520)
        lay = QVBoxLayout(self)
        t = QTableWidget(0, 2)
        t.setHorizontalHeaderLabels(["Keys", "Action"])
        t.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        t.verticalHeader().setVisible(False)
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        rows = [("In the main window", "")] + WINDOW_SHORTCUTS + [("", ""), ("Global hot-keys", "")]
        b = settings.get("hotkeys.bindings")
        for action, label, _ in HOTKEY_ACTIONS:
            e = b.get(action) or {}
            if e.get("keys"):
                note = status.get(action)
                scope = "" if e.get("scope", "global") == "global" else " (window only)"
                rows.append((e["keys"] + scope, label + (f" — {note}" if note else "")))
        for k, a in rows:
            r = t.rowCount()
            t.insertRow(r)
            ki = QTableWidgetItem(k)
            if not a and k:
                f = ki.font()
                f.setBold(True)
                ki.setFont(f)
            t.setItem(r, 0, ki)
            t.setItem(r, 1, QTableWidgetItem(a))
        lay.addWidget(t)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.accept)
        lay.addWidget(bb)


COPYRIGHT_YEAR = "2026"
DISCLAIMER = ("This program comes with ABSOLUTELY NO WARRANTY! It is provided \u201cas is\u201d, to the "
              "extent permitted by law. {pub} is not liable for any loss or damage arising from its use, "
              "including lost, incomplete or damaged recordings. Use it at your own risk.")


def about_text(libs: str) -> str:
    """The About box as one HTML block (name, donation link, details, fine print)."""
    return (f"<h3 style='margin-bottom:2px'>{APP_NAME} {__version__}</h3>"
            f"{about_link_html()}"
            f"{about_body_html(libs)}"
            f"<p><small>{about_fine_print_html()}</small></p>")


def about_link_html() -> str:
    return (f"<p style='margin-top:0'>Support this program with a donation: "
            f"<a href='{WEBSITE}'>{WEBSITE}</a></p>")


def about_body_html(libs: str) -> str:
    return (f"<p>Voice-activated sound recorder for Windows 11: microphone, computer audio, or both. "
            f"Build {BUILD_DATE}.</p>"
            f"<p>{libs}</p>"
            "<p>Uses Qt for Python (LGPL-3.0), PortAudio via sounddevice (MIT), WASAPI loopback via "
            "PyAudioWPatch (Apache-2.0), libsndfile via soundfile (LGPL-2.1 / BSD-3), LAME via lameenc (LGPL), "
            "NumPy (BSD-3), soxr (LGPL-2.1), pyserial (BSD-3) and hidapi (BSD). Licence texts are in "
            "THIRD-PARTY-LICENSES.txt.</p>")


def about_fine_print_html() -> str:
    return (f"Copyright \u00a9 {COPYRIGHT_YEAR} {PUBLISHER}. All rights reserved.<br>"
            + DISCLAIMER.format(pub=PUBLISHER))


class AboutDialog(QDialog):
    """Help > About: name and version, donation link, details, copyright and disclaimer in fine print."""

    def __init__(self, libs: str, icon=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"About {APP_NAME}")
        lay = QVBoxLayout(self)
        lay.setSpacing(6)
        head = QHBoxLayout()
        if icon is not None:
            pic = QLabel()
            pic.setPixmap(icon.pixmap(64, 64))
            pic.setAlignment(Qt.AlignTop)
            head.addWidget(pic)
        col = QVBoxLayout()
        col.setSpacing(2)
        self.title = QLabel(f"<h3 style='margin:0'>{APP_NAME} {__version__}</h3>")
        self.link = QLabel(about_link_html())
        self.link.setTextFormat(Qt.RichText)
        self.link.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.link.setOpenExternalLinks(True)
        self.link.setAccessibleName(f"Donation website {WEBSITE}")
        self.body = QLabel(about_body_html(libs))
        self.body.setWordWrap(True)
        self.body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        col.addWidget(self.title)
        col.addWidget(self.link)
        col.addWidget(self.body)
        head.addLayout(col, 1)
        lay.addLayout(head)
        self.fine = QLabel(about_fine_print_html())
        self.fine.setWordWrap(True)
        f = self.fine.font()
        f.setPointSizeF(max(7.0, f.pointSizeF() * 0.85))
        self.fine.setFont(f)
        self.fine.setStyleSheet("color: palette(placeholder-text);")
        self.fine.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.fine)
        bb = QDialogButtonBox(QDialogButtonBox.Ok)
        bb.accepted.connect(self.accept)
        lay.addWidget(bb)
        self.setMinimumWidth(460)
        self.setMaximumWidth(560)
