"""Options dialog: every setting, per-tab defaults, export/import."""
from __future__ import annotations

import os

import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QGroupBox, QHBoxLayout, QHeaderView,
                               QInputDialog, QKeySequenceEdit, QLabel, QLineEdit, QListWidget, QMessageBox,
                               QPushButton, QRadioButton, QScrollArea, QSlider, QSpinBox, QTableWidget, QTabWidget,
                               QVBoxLayout, QWidget)

from vrp import paths, winutil
from vrp.core import naming
from vrp.core.cleanup import denoise_support
from vrp.core.dsp import percent_to_dbfs
from vrp.core.model import FormatSpec
from vrp.inputs import external, keymap
from vrp.settings import EXTERNAL_ONLY_ACTIONS, HOTKEY_ACTIONS, TAB_PREFIXES
from vrp.transcribe import LANGUAGES, SCRIPTS
from vrp.transcribe import pack as lt_pack
from vrp.ui.dialogs import CalibrateDialog, FormatForm

ACTION_CHOICES = [(a, label) for a, label, _ in HOTKEY_ACTIONS if a not in ("show_hide", "suspend_hotkeys")] + \
    EXTERNAL_ONLY_ACTIONS


def _row(*widgets, stretch=True) -> QWidget:
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    for x in widgets:
        h.addWidget(x)
    if stretch:
        h.addStretch()
    return w


class OptionsDialog(QDialog):
    def __init__(self, settings, engine, hotkeys, ext_manager, backend, tab: str | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Options")
        self.resize(880, 600)
        self.settings = settings
        self.engine = engine
        self.hotkeys = hotkeys
        self.ext = ext_manager
        self.backend = backend
        self.getters: dict[str, callable] = {}
        self.setters: dict[str, callable] = {}
        self.extra_apply: list = []
        lay = QVBoxLayout(self)
        self.tabs = QTabWidget()
        lay.addWidget(self.tabs)
        self._general()
        self._recording()
        self._format()
        self._vox()
        self._playback()
        self._hotkeys()
        self._external()
        self._devices()
        self._transcript()
        self._advanced()
        bottom = QHBoxLayout()
        for text, fn in (("Export\u2026", self._export), ("Import\u2026", self._import),
                         ("Restore defaults", self._restore)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            bottom.addWidget(b)
        bottom.addStretch()
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel | QDialogButtonBox.Apply)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        bb.button(QDialogButtonBox.Apply).clicked.connect(self.apply)
        bottom.addWidget(bb)
        lay.addLayout(bottom)
        self.load()
        if tab:
            for i in range(self.tabs.count()):
                if self.tabs.tabText(i).lower().replace("-", "") == tab.lower().replace("-", ""):
                    self.tabs.setCurrentIndex(i)
        if settings.read_only:
            lay.insertWidget(0, QLabel("These settings were saved by a newer version and are read-only."))

    # ================================================================ binding
    def _bind(self, key, getter, setter):
        self.getters[key] = getter
        self.setters[key] = setter

    def _check(self, key, text) -> QCheckBox:
        w = QCheckBox(text)
        self._bind(key, w.isChecked, lambda v: w.setChecked(bool(v)))
        return w

    def _spin(self, key, lo, hi, suffix="", step=1, decimals=0, special=None) -> QWidget:
        if decimals:
            w = QDoubleSpinBox()
            w.setDecimals(decimals)
        else:
            w = QSpinBox()
        w.setRange(lo, hi)
        w.setSingleStep(step)
        if suffix:
            w.setSuffix(suffix)
        if special:
            w.setSpecialValueText(special)
        self._bind(key, w.value, lambda v: w.setValue(v))
        return w

    def _combo(self, key, items) -> QComboBox:
        w = QComboBox()
        for label, value in items:
            w.addItem(label, value)
        self._bind(key, w.currentData, lambda v: w.setCurrentIndex(max(0, w.findData(v))))
        return w

    def _line(self, key, placeholder="") -> QLineEdit:
        w = QLineEdit()
        w.setPlaceholderText(placeholder)
        self._bind(key, lambda: w.text().strip(), lambda v: w.setText(v or ""))
        return w

    def _folder(self, key, default_text) -> QWidget:
        line = QLineEdit()
        line.setPlaceholderText(default_text)
        browse = QPushButton("Browse\u2026")
        opn = QPushButton("Open")

        def pick():
            d = QFileDialog.getExistingDirectory(self, "Choose folder", line.text() or default_text)
            if d:
                line.setText(d)
        browse.clicked.connect(pick)
        opn.clicked.connect(lambda: winutil.open_folder(line.text() or default_text))
        self._bind(key, lambda: line.text().strip(), lambda v: line.setText(v or ""))
        w = _row(line, browse, opn, stretch=False)
        w.line = line
        return w

    def _page(self, title) -> QFormLayout:
        page = QWidget()
        form = QFormLayout(page)
        scroll = QScrollArea()
        scroll.setWidget(page)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        self.tabs.addTab(scroll, title)
        return form

    # =================================================================== tabs
    def _general(self) -> None:
        f = self._page("General")
        f.addRow("Theme:", self._combo("general.theme", [("Follow Windows", "system"), ("Light", "light"),
                                                         ("Dark", "dark")]))
        f.addRow("", self._check("general.start_minimised", "Start minimised to the tray"))
        f.addRow("", self._check("general.run_at_login", "Start when I sign in to Windows"))
        f.addRow("", self._check("general.close_to_tray", "Closing the window keeps the app running in the tray"))
        f.addRow("", self._check("general.always_on_top", "Keep the window on top of other windows"))
        f.addRow("", self._check("general.notifications", "Show notifications"))
        f.addRow("Your name (saved in file tags):", self._line("general.user_name"))
        f.addRow("Language:", self._combo("general.language", [("Follow Windows (English)", "system"),
                                                               ("English", "en")]))

    def _recording(self) -> None:
        f = self._page("Recording")
        # sources (v1.0.2)
        src = QGroupBox("Record from")
        sf = QFormLayout(src)
        self.src_combo = QComboBox()
        for label, value in (("Microphone and computer audio", "both"), ("Microphone only", "mic"),
                             ("Computer audio only", "system")):
            self.src_combo.addItem(label, value)
        sf.addRow("Sources:", self.src_combo)
        self.sys_gain = self._spin("rec.system_gain_db", -20, 20, " dB")
        self.sys_gain.setToolTip("Raise or lower computer audio against the microphone in the recording.")
        sf.addRow("Computer audio level:", self.sys_gain)
        ok, why = self.engine.loopback_support()
        note = QLabel("Computer audio is what plays through your default speakers or headphones: calls, "
                      "videos, music. It follows the Windows default output device. You can also tick or "
                      "untick Mic and Computer in the main window at any time, even while recording."
                      if ok else f"Computer audio cannot be recorded on this PC: {why}")
        note.setWordWrap(True)
        note.setStyleSheet("color: palette(mid);")
        sf.addRow(note)
        sep = QLabel("To keep your microphone and the computer audio on separate channels (left and right), "
                     "choose Format > Channels > Separate.")
        sep.setWordWrap(True)
        sf.addRow(sep)
        if not ok:
            self.src_combo.model().item(0).setEnabled(False)
            self.src_combo.model().item(2).setEnabled(False)
            self.sys_gain.setEnabled(False)

        def sources_get():
            v = self.src_combo.currentData()
            if v == self._src_loaded:
                return {}             # unchanged: keep the main-window ticks as they are
            return {"rec.src_mic": v in ("both", "mic"), "rec.src_system": v in ("both", "system")}

        def sources_set(s):
            mic, system = bool(s.get("rec.src_mic")), bool(s.get("rec.src_system"))
            v = "both" if mic and system else "system" if system else "mic"
            self.src_combo.setCurrentIndex(max(0, self.src_combo.findData(v)))
            self._src_loaded = self.src_combo.currentData()
        self._bind("__sources__", sources_get, sources_set)
        f.addRow(src)
        self.folder_w = self._folder("rec.folder", str(paths.default_recordings_dir()))
        f.addRow("Recordings folder:", self.folder_w)
        f.addRow("Fallback local folder:", self._folder("rec.fallback_folder", str(paths.default_fallback_dir())))
        self.pattern = self._line("rec.pattern")
        self.preview = QLabel()
        self.preview.setWordWrap(True)
        self.preview.setStyleSheet("color: palette(mid);")
        self.pattern.textChanged.connect(self._update_preview)
        f.addRow("File names:", self.pattern)
        tokens = QLabel(" ".join(naming.TOKENS) + "   (use / for subfolders)")
        tokens.setWordWrap(True)
        tokens.setStyleSheet("color: palette(mid);")
        f.addRow("", tokens)
        f.addRow("Next file:", self.preview)
        f.addRow("Tag ({tag}):", self._line("rec.tag"))
        f.addRow("Comment (saved in tags):", self._line("rec.comment"))
        f.addRow("When Record is pressed with a file open:",
                 self._combo("rec.open_file_action", [("Append a new segment", "append"),
                                                      ("Start a new file", "new"), ("Ask", "ask")]))
        f.addRow("", self._check("rec.timestamps", "Time-stamp segments (wall-clock start time)"))
        g = QGroupBox("Start a new file automatically")
        gf = QFormLayout(g)
        gf.addRow("When the file reaches:", self._spin("rec.rollover.size_mb", 0, 3900, " MB", special="Off"))
        gf.addRow("", self._combo("rec.size_action", [("then continue in a new file", "rollover"),
                                                      ("then stop recording", "stop")]))
        gf.addRow("After recording for:", self._spin("rec.rollover.minutes", 0, 1440, " min", special="Off"))
        gf.addRow("", self._check("rec.rollover.midnight", "At midnight"))
        gf.addRow("Every:", self._spin("rec.rollover.every_hours", 0, 24, " hours, on the hour", special="Off"))
        f.addRow(g)
        g2 = QGroupBox("Stop recording automatically")
        g2f = QFormLayout(g2)
        g2f.addRow("After silence of (Vox off):", self._spin("rec.autostop.silence_min", 0, 120, " min",
                                                             special="Off"))
        g2f.addRow("At clock time (HH:MM):", self._line("rec.autostop.clock", "e.g. 17:30, empty = off"))
        f.addRow(g2)
        g3 = QGroupBox("Disk space")
        g3f = QFormLayout(g3)
        g3f.addRow("Warn below:", self._spin("rec.disk_warn_mb", 10, 100000, " MB"))
        g3f.addRow("Stop recording below:", self._spin("rec.disk_stop_mb", 5, 100000, " MB"))
        f.addRow(g3)
        f.addRow("", self._check("rec.sidecar_json", "Also write a .segments.json file next to each recording"))
        f.addRow("Run after each file is finished:",
                 self._line("rec.after_command", 'e.g. copy {path} "D:\\Archive"   placeholders: {path} '
                                                 '{duration_s} {segments} {start_iso}'))

    def _update_preview(self) -> None:
        folder = self.folder_w.line.text() or str(paths.default_recordings_dir())
        pat = self.pattern.text()
        err = naming.validate_pattern(pat)
        if err:
            self.preview.setText(f"\u26a0 {err}")
            return
        ext = getattr(self, "fmt_form", None)
        ext = ext.spec().ext if ext else ".mp3"
        try:
            self.preview.setText(str(naming.next_path(folder, pat, ext, host=winutil.computer_name(),
                                                      tag=self.getters["rec.tag"]())))
        except Exception as exc:
            self.preview.setText(str(exc))

    def _format(self) -> None:
        f = self._page("Format")
        self.fmt_form = FormatForm(allow_split=True)
        f.addRow(self.fmt_form)
        f.addRow(QLabel("A format change applies to the next new file; appending needs a matching format."))
        self._bind("__fmt__", self.fmt_form.spec, self.fmt_form.set_spec)
        self.fmt_form.codec.currentIndexChanged.connect(self._update_preview)
        f.addRow("Input gain:", self._spin("fmt.gain_db", -20, 20, " dB"))
        f.addRow("", self._check("fmt.highpass", "High-pass filter at 80 Hz (removes rumble and DC offset)"))
        # clean-up of the microphone (1.0.4)
        g = QGroupBox("Clean-up (microphone)")
        gf = QFormLayout(g)
        self.denoise_chk = self._check("fmt.denoise", "Noise suppression: remove steady background noise "
                                                       "(air conditioning, fans, computer hum)")
        self.level_chk = self._check("fmt.level", "Automatic level: bring near and far voices to a similar "
                                                   "loudness")
        self.keep_chk = self._check("fmt.keep_original", "Also keep an untouched copy "
                                                         "(<name>-original, same format)")
        gf.addRow(self.denoise_chk)
        gf.addRow(self.level_chk)
        gf.addRow(self.keep_chk)
        ok, why = denoise_support()
        note = QLabel("Applies to the microphone only, from the next recording. Vox listens after noise "
                      "suppression and before the automatic level. Noise suppression delays the recording by "
                      "about 0.2 s, and computer audio is delayed with it so the two stay in step. Voices of "
                      "other people nearby are reduced only a little."
                      if ok else f"Noise suppression is not available on this PC: {why}")
        note.setWordWrap(True)
        gf.addRow(note)
        if not ok:
            self.denoise_chk.setEnabled(False)

        def keep_state(*_):
            self.keep_chk.setEnabled(self.denoise_chk.isChecked() or self.level_chk.isChecked())
        self.denoise_chk.toggled.connect(keep_state)
        self.level_chk.toggled.connect(keep_state)
        self._keep_state = keep_state
        f.addRow(g)

    def _vox(self) -> None:
        f = self._page("Vox")
        f.addRow("", self._check("vox.enabled", "Voice activation on (record only while there is sound)"))
        self.thr_slider = QSlider(Qt.Horizontal)
        self.thr_slider.setRange(0, 100)
        self.thr_value = QLabel()
        self.thr_slider.valueChanged.connect(
            lambda v: self.thr_value.setText(f"{v}  ({percent_to_dbfs(v):.1f} dBFS)" if v else "0 (any sound)"))
        self._bind("vox.threshold", self.thr_slider.value, lambda v: self.thr_slider.setValue(int(v)))
        cal = QPushButton("Calibrate\u2026")
        cal.clicked.connect(self._calibrate)
        f.addRow("Threshold:", _row(self.thr_slider, self.thr_value, cal, stretch=False))
        f.addRow("Attack time:", self._spin("vox.attack_ms", 0, 500, " ms", 5))
        f.addRow("Hang time:", self._spin("vox.hang_s", 0.2, 30.0, " s", 0.1, 1))
        f.addRow("Pre-roll:", self._spin("vox.preroll_ms", 0, 5000, " ms", 50))
        f.addRow("Minimum segment:", self._spin("vox.min_seg_s", 0.0, 5.0, " s", 0.1, 1))
        f.addRow("Detection window:", self._spin("vox.window_ms", 5, 50, " ms"))
        f.addRow("Each activation starts:", self._combo("vox.mode", [("A new segment in the same file", "segment"),
                                                                     ("A new file", "file")]))
        f.addRow("Silence between segments on playback:", self._spin("vox.gap_ms", 0, 2000, " ms", 50))
        f.addRow("Level scale:", self._combo("ui.meter_scale", [("Linear 0\u2013100", "linear"),
                                                                ("Decibels (\u221260 to 0 dBFS)", "db")]))
        f.addRow("", self._check("vox.disabled", "Disable Vox (always record continuously)"))
        f.addRow("", self._check("vox.hidden", "Hide the Vox controls in the main window"))

    def _calibrate(self) -> None:
        d = CalibrateDialog(self.engine, self)
        if d.exec() == QDialog.Accepted and d.value is not None:
            self.thr_slider.setValue(d.value)

    def _playback(self) -> None:
        f = self._page("Playback")
        f.addRow("Volume:", self._spin("play.volume", 0, 100, " %", 5))
        f.addRow("Speed (pitch kept):", self._spin("play.speed", 50, 200, " %", 5))
        f.addRow("Auto-backspace on resume:", self._spin("play.backspace_s", 0.0, 10.0, " s", 0.5, 1,
                                                          special="Off"))
        f.addRow("Skip step (tap Rewind/FF):", self._spin("play.skip_s", 1.0, 60.0, " s", 1.0, 1))
        f.addRow("", self._check("play.audible_scan", "Hear the audio while scanning (hold Rewind/FF)"))
        f.addRow("At the end of the file:", self._combo("play.at_end", [("Stay at the end", "stay"),
                                                                        ("Return to the start", "start")]))

    # ------------------------------------------------------------- hot-keys
    def _hotkeys(self) -> None:
        page = QWidget()
        lay = QVBoxLayout(page)
        self.tabs.addTab(page, "Hot-keys")
        info = QLabel("Click a box and press the key combination. Global keys work while other programs have "
                      "focus. Use the clear button to remove a binding.")
        info.setWordWrap(True)
        lay.addWidget(info)
        if not self.hotkeys.supported:
            lay.addWidget(QLabel("\u26a0 Global hot-keys are only available on Windows."))
        self.hk_table = QTableWidget(len(HOTKEY_ACTIONS), 4)
        self.hk_table.setHorizontalHeaderLabels(["Action", "Keys", "Scope", "Status"])
        self.hk_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.hk_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.hk_table.verticalHeader().setVisible(False)
        self.hk_table.setSelectionMode(QAbstractItemView.NoSelection)
        self.hk_rows = {}
        for r, (action, label, _d) in enumerate(HOTKEY_ACTIONS):
            self.hk_table.setCellWidget(r, 0, QLabel(" " + label))
            ed = QKeySequenceEdit()
            try:
                ed.setMaximumSequenceLength(1)
                ed.setClearButtonEnabled(True)
            except AttributeError:
                pass
            scope = QComboBox()
            scope.addItem("Global", "global")
            scope.addItem("Window only", "window")
            status = QLabel()
            self.hk_table.setCellWidget(r, 1, ed)
            self.hk_table.setCellWidget(r, 2, scope)
            self.hk_table.setCellWidget(r, 3, status)
            self.hk_rows[action] = (ed, scope, status)
            ed.keySequenceChanged.connect(lambda _s, a=action: self._hk_changed(a))
        self.hk_table.resizeColumnsToContents()
        self.hk_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        lay.addWidget(self.hk_table)
        self.hk_warn = QLabel()
        self.hk_warn.setWordWrap(True)
        lay.addWidget(self.hk_warn)
        f = QFormLayout()
        f.addRow("", self._check("hotkeys.media_keys", "Respond to keyboard media keys (Play/Pause, Stop, "
                                                       "Previous, Next)"))
        f.addRow("Beep when recording starts/stops:",
                 self._combo("hotkeys.tones", [("When the window is hidden", "hidden"), ("Always", "always"),
                                               ("Never", "never")]))
        f.addRow("Beep volume:", self._spin("hotkeys.tone_volume", 0, 100, " %", 5))
        lay.addLayout(f)
        self._bind("hotkeys.bindings", self._hk_get, self._hk_set)
        self._hk_loading = False

    @staticmethod
    def _seq_text(ed: QKeySequenceEdit) -> str:
        return ed.keySequence().toString(QKeySequence.PortableText)

    def _hk_get(self) -> dict:
        return {a: {"keys": self._seq_text(ed), "scope": sc.currentData()} for a, (ed, sc, _s) in
                self.hk_rows.items()}

    def _hk_set(self, b: dict) -> None:
        self._hk_loading = True
        for a, (ed, sc, st) in self.hk_rows.items():
            e = b.get(a) or {}
            ed.setKeySequence(QKeySequence(e.get("keys", ""), QKeySequence.PortableText))
            sc.setCurrentIndex(0 if e.get("scope", "global") == "global" else 1)
            err = self.hotkeys.status.get(a, "")
            st.setText(("\u26a0 " + err) if err else ("OK" if e.get("keys") else ""))
            st.setStyleSheet("color: #d93025;" if err else "")
        self._hk_loading = False

    def _hk_changed(self, action: str) -> None:
        if self._hk_loading:
            return
        ed, sc, st = self.hk_rows[action]
        text = self._seq_text(ed)
        st.setText("")
        if not text:
            return
        norm = keymap.normalise(text)
        for other, (oed, _osc, _ost) in self.hk_rows.items():
            if other != action and keymap.normalise(self._seq_text(oed)) == norm:
                label = dict((a, lab) for a, lab, _ in HOTKEY_ACTIONS)[other]
                if QMessageBox.question(self, "Hot-key in use", f"{text} is already used for \u201c{label}\u201d. "
                                        "Move it here?") == QMessageBox.Yes:
                    self._hk_loading = True
                    oed.clear()
                    self._hk_loading = False
                else:
                    self._hk_loading = True
                    ed.clear()
                    self._hk_loading = False
                    return
        ok, warn = keymap.is_safe_global(text) if sc.currentData() == "global" else (True, "")
        if not ok:
            st.setText("\u26a0 " + warn)
            st.setStyleSheet("color: #d93025;")
            self._hk_loading = True
            ed.clear()
            self._hk_loading = False
            return
        self.hk_warn.setText(("\u26a0 " + warn) if warn else "")

    # --------------------------------------------------------- external
    def _external(self) -> None:
        page = QWidget()
        lay = QVBoxLayout(page)
        self.tabs.addTab(page, "External devices")
        kinds = external.available_kinds()
        avail = ", ".join(k for k, ok in (("game controllers", kinds["joystick"]), ("serial ports", kinds["serial"]),
                                          ("USB HID pedals", kinds["hid"])) if ok) or "none"
        top = QHBoxLayout()
        top.addWidget(QLabel(f"Supported here: {avail}."))
        top.addStretch()
        self.ext_enabled = self._check("ext.enabled", "Use external devices")
        top.addWidget(self.ext_enabled)
        lay.addLayout(top)
        prof = QHBoxLayout()
        self.profile = QComboBox()
        load = QPushButton("Load")
        save = QPushButton("Save as\u2026")
        delete = QPushButton("Delete")
        load.clicked.connect(self._profile_load)
        save.clicked.connect(self._profile_save)
        delete.clicked.connect(self._profile_delete)
        prof.addWidget(QLabel("Profile:"))
        prof.addWidget(self.profile, 1)
        for b in (load, save, delete):
            prof.addWidget(b)
        lay.addLayout(prof)
        # devices
        dev_row = QHBoxLayout()
        self.dev_list = QListWidget()
        self.dev_list.setMaximumHeight(90)
        dev_row.addWidget(self.dev_list, 2)
        self.test_list = QListWidget()
        self.test_list.setMaximumHeight(90)
        self.test_list.setToolTip("Live state of the selected device's active inputs")
        dev_row.addWidget(self.test_list, 1)
        col = QVBoxLayout()
        rb = QPushButton("Refresh")
        rb.clicked.connect(self._ext_refresh)
        col.addWidget(rb)
        col.addStretch()
        dev_row.addLayout(col)
        lay.addWidget(QLabel("Detected devices (select one to see its inputs live):"))
        lay.addLayout(dev_row)
        self.dev_list.currentRowChanged.connect(self._ext_select_device)
        # mappings
        lay.addWidget(QLabel("Mappings:"))
        self.map_table = QTableWidget(0, 7)
        self.map_table.setHorizontalHeaderLabels(["Device", "Input", "Trigger", "Invert", "Debounce", "Axis %",
                                                  "Action"])
        self.map_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.map_table.verticalHeader().setVisible(False)
        self.map_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        lay.addWidget(self.map_table, 1)
        mb = QHBoxLayout()
        add = QPushButton("Add")
        rem = QPushButton("Remove")
        self.learn_btn = QPushButton("Learn")
        self.learn_btn.setToolTip("Press a pedal, button or switch to fill in the selected row")
        add.clicked.connect(lambda: self._map_add(external.Mapping()))
        rem.clicked.connect(self._map_remove)
        self.learn_btn.clicked.connect(self._learn)
        for b in (add, rem, self.learn_btn):
            mb.addWidget(b)
        mb.addStretch()
        lay.addLayout(mb)
        # outputs
        lay.addWidget(QLabel("Outputs (serial lines follow the app state):"))
        self.out_table = QTableWidget(0, 4)
        self.out_table.setHorizontalHeaderLabels(["When", "Port", "Line", ""])
        self.out_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.out_table.verticalHeader().setVisible(False)
        self.out_table.setMaximumHeight(110)
        lay.addWidget(self.out_table)
        ob = QHBoxLayout()
        oadd = QPushButton("Add output")
        orem = QPushButton("Remove output")
        oadd.clicked.connect(lambda: self._out_add({"state": "recording", "port": "", "line": "RTS"}))
        orem.clicked.connect(self._out_remove)
        ob.addWidget(oadd)
        ob.addWidget(orem)
        ob.addStretch()
        lay.addLayout(ob)
        f = QFormLayout()
        f.addRow("If a device disconnects:", self._combo("ext.on_loss", [
            ("Keep the current recording/playback state", "keep"),
            ("Treat it as the input being released", "release")]))
        f.addRow("", self._check("ext.suspend_with_hotkeys", "\u201cSuspend all hot-keys\u201d also suspends "
                                                              "external devices"))
        lay.addLayout(f)
        note = QLabel("Wiring a switch to a serial port: VRP drives DTR high as a source voltage; a contact between "
                      "DTR and CTS, DSR, DCD or RI registers as active (DTR then cannot also be an output).")
        note.setWordWrap(True)
        note.setStyleSheet("color: palette(mid);")
        lay.addWidget(note)
        self._bind("ext.mappings", self._map_get, self._map_set)
        self._bind("ext.outputs", self._out_get, self._out_set)
        self._bind("ext.profiles", lambda: self._profiles, self._profiles_set)
        self.known_devices: list[external.DeviceRef] = []
        self._watched: str | None = None
        self.ext.learned.connect(self._learned)
        self.ext_timer = QTimer(self)
        self.ext_timer.setInterval(100)
        self.ext_timer.timeout.connect(self._ext_live)
        self.ext_timer.start()
        self._learning = False
        QTimer.singleShot(0, self._ext_refresh)

    def _profiles_set(self, saved: dict) -> None:
        self._profiles = dict(saved)
        self.profile.clear()
        for name in external.BUILTIN_PROFILES:
            self.profile.addItem(name + " (built-in)", ("builtin", name))
        for name in saved:
            self.profile.addItem(name, ("saved", name))

    def _profile_load(self) -> None:
        data = self.profile.currentData()
        if not data:
            return
        src = external.BUILTIN_PROFILES if data[0] == "builtin" else self._profiles
        p = src.get(data[1], {})
        self._map_set(p.get("mappings", []))
        self._out_set(p.get("outputs", []))

    def _profile_save(self) -> None:
        name, ok = QInputDialog.getText(self, "Save profile", "Profile name:")
        if ok and name.strip():
            self._profiles[name.strip()] = {"mappings": self._map_get(), "outputs": self._out_get()}
            self._profiles_set(self._profiles)

    def _profile_delete(self) -> None:
        data = self.profile.currentData()
        if data and data[0] == "saved":
            self._profiles.pop(data[1], None)
            self._profiles_set(self._profiles)

    def _ext_refresh(self) -> None:
        self.known_devices = external.enumerate_devices()
        self.dev_list.clear()
        for d in self.known_devices:
            self.dev_list.addItem(f"{d.name}   [{d.kind}]")
        if not self.known_devices:
            self.dev_list.addItem("No game controllers, serial ports or HID devices found.")
        for r in range(self.map_table.rowCount()):
            self._fill_device_combo(self.map_table.cellWidget(r, 0))
        for r in range(self.out_table.rowCount()):
            self._fill_ports(self.out_table.cellWidget(r, 1))

    def _ext_select_device(self, row: int) -> None:
        if self._watched:
            self.ext.watch(self._watched, False)
            self._watched = None
        if 0 <= row < len(self.known_devices):
            self._watched = self.known_devices[row].key
            self.ext.watch(self._watched, True)

    def _ext_live(self) -> None:
        if not self._watched:
            return
        vals = self.ext.live_values(self._watched)
        active = [f"{k}: {v:.2f}" if k[-1:] in "+-" else k for k, v in vals.items() if v >= 0.5]
        texts = active or (["(no input active)"] if vals else ["(opening\u2026)"])
        if [self.test_list.item(i).text() for i in range(self.test_list.count())] != texts:
            self.test_list.clear()
            self.test_list.addItems(texts)

    def _fill_device_combo(self, combo: QComboBox) -> None:
        cur = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        keys = set()
        for d in self.known_devices:
            combo.addItem(d.name, (d.kind, d.id, d.name))
            keys.add((d.kind, d.id))
        if cur and (cur[0], cur[1]) not in keys:
            combo.addItem(f"{cur[2] or cur[1]} (not connected)", cur)
        if cur:
            for i in range(combo.count()):
                d = combo.itemData(i)
                if d and d[0] == cur[0] and d[1] == cur[1]:
                    combo.setCurrentIndex(i)
        combo.blockSignals(False)

    @staticmethod
    def _inputs_for(kind: str) -> list[str]:
        if kind == "serial":
            return external.SERIAL_INPUTS
        if kind == "joystick":
            return ([f"Button {i}" for i in range(1, 33)] + [f"{a}{s}" for a in "XYZRUV" for s in "+-"]
                    + ["POV Up", "POV Down", "POV Left", "POV Right"])
        return [f"Bit {i}" for i in range(64)]

    def _map_add(self, m: external.Mapping) -> None:
        r = self.map_table.rowCount()
        self.map_table.insertRow(r)
        dev = QComboBox()
        if m.device_id:
            dev.addItem(m.device_name or m.device_id, (m.device_kind, m.device_id, m.device_name))
        self._fill_device_combo(dev)
        if m.device_id:
            for i in range(dev.count()):
                d = dev.itemData(i)
                if d and d[0] == m.device_kind and d[1] == m.device_id:
                    dev.setCurrentIndex(i)
        inp = QComboBox()
        inp.setEditable(True)

        def refill_inputs():
            d = dev.currentData()
            cur = inp.currentText()
            inp.clear()
            inp.addItems(self._inputs_for(d[0] if d else "hid"))
            inp.setCurrentText(cur)
        dev.currentIndexChanged.connect(refill_inputs)
        refill_inputs()
        inp.setCurrentText(m.input)
        trig = QComboBox()
        for k, v in external.TRIGGERS.items():
            trig.addItem(v, k)
        trig.setCurrentIndex(max(0, trig.findData(m.trigger)))
        inv = QCheckBox()
        inv.setChecked(m.invert)
        deb = QSpinBox()
        deb.setRange(0, 500)
        deb.setSuffix(" ms")
        deb.setValue(m.debounce_ms)
        thr = QSpinBox()
        thr.setRange(1, 100)
        thr.setValue(m.threshold)
        act = QComboBox()
        for a, label in ACTION_CHOICES:
            act.addItem(label, a)
        act.setCurrentIndex(max(0, act.findData(m.action)))
        for c, w in enumerate((dev, inp, trig, _row(inv), deb, thr, act)):
            self.map_table.setCellWidget(r, c, w)
        self.map_table.resizeColumnsToContents()
        self.map_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)

    def _map_remove(self) -> None:
        rows = sorted({i.row() for i in self.map_table.selectionModel().selectedRows()}, reverse=True)
        if not rows and self.map_table.rowCount():
            rows = [self.map_table.rowCount() - 1]
        for r in rows:
            self.map_table.removeRow(r)

    def _map_get(self) -> list:
        out = []
        for r in range(self.map_table.rowCount()):
            dev = self.map_table.cellWidget(r, 0).currentData()
            if not dev:
                continue
            inv = self.map_table.cellWidget(r, 3).findChild(QCheckBox)
            out.append(external.Mapping(dev[0], dev[1], dev[2], self.map_table.cellWidget(r, 1).currentText(),
                                        self.map_table.cellWidget(r, 2).currentData(), inv.isChecked(),
                                        self.map_table.cellWidget(r, 4).value(),
                                        self.map_table.cellWidget(r, 5).value(),
                                        self.map_table.cellWidget(r, 6).currentData()).to_json())
        return out

    def _map_set(self, items: list) -> None:
        self.map_table.setRowCount(0)
        for d in items:
            self._map_add(external.Mapping.from_json(d))

    def _learn(self) -> None:
        if self.map_table.rowCount() == 0:
            self._map_add(external.Mapping())
            self.map_table.selectRow(0)
        for d in self.known_devices:
            self.ext.watch(d.key, True)
        self._learning = True
        self.learn_btn.setText("Press a pedal or button\u2026")
        self.learn_btn.setEnabled(False)
        QTimer.singleShot(300, self.ext.start_learn)
        QTimer.singleShot(10000, self._learn_timeout)

    def _learn_timeout(self) -> None:
        if self._learning:
            self._end_learn()
            QMessageBox.information(self, "Learn", "No input was detected. Check the device is connected.")

    def _end_learn(self) -> None:
        self._learning = False
        self.ext.stop_learn()
        self.learn_btn.setText("Learn")
        self.learn_btn.setEnabled(True)
        for d in self.known_devices:
            if d.key != self._watched:
                self.ext.watch(d.key, False)

    def _learned(self, kind: str, ident: str, _name: str, inp: str) -> None:
        if not self._learning:
            return
        self._end_learn()
        rows = self.map_table.selectionModel().selectedRows()
        r = rows[0].row() if rows else self.map_table.rowCount() - 1
        name = next((d.name for d in self.known_devices if d.kind == kind and d.id == ident), ident)
        dev = self.map_table.cellWidget(r, 0)
        idx = -1
        for i in range(dev.count()):
            d = dev.itemData(i)
            if d and d[0] == kind and d[1] == ident:
                idx = i
        if idx < 0:
            dev.addItem(name, (kind, ident, name))
            idx = dev.count() - 1
        dev.setCurrentIndex(idx)
        self.map_table.cellWidget(r, 1).setCurrentText(inp)

    def _fill_ports(self, combo: QComboBox) -> None:
        cur = combo.currentText()
        combo.clear()
        ports = [d.id for d in self.known_devices if d.kind == "serial"]
        combo.addItems(ports)
        if cur and cur not in ports:
            combo.addItem(cur)
        combo.setCurrentText(cur)

    def _out_add(self, o: dict) -> None:
        r = self.out_table.rowCount()
        self.out_table.insertRow(r)
        st = QComboBox()
        for k, v in external.OUTPUT_STATES.items():
            st.addItem(v, k)
        st.setCurrentIndex(max(0, st.findData(o.get("state"))))
        port = QComboBox()
        port.setEditable(True)
        port.addItem(o.get("port", ""))
        self._fill_ports(port)
        port.setCurrentText(o.get("port", ""))
        line = QComboBox()
        line.addItems(external.SERIAL_OUTPUTS)
        line.setCurrentText(o.get("line", "RTS"))
        test = QPushButton("Test")
        test.setCheckable(True)
        test.toggled.connect(lambda on, p=port, ln=line: self._out_test(p.currentText(), ln.currentText(), on))
        for c, w in enumerate((st, port, line, test)):
            self.out_table.setCellWidget(r, c, w)

    def _out_test(self, port: str, line: str, on: bool) -> None:
        key = f"serial:{port}"
        self.ext.watch(key, True)
        QTimer.singleShot(300, lambda: self.ext.test_output(port, line, on) or (on and QMessageBox.information(
            self, "Test output", f"{port} could not be opened.")))

    def _out_remove(self) -> None:
        r = self.out_table.currentRow()
        if r < 0:
            r = self.out_table.rowCount() - 1
        if r >= 0:
            self.out_table.removeRow(r)

    def _out_get(self) -> list:
        out = []
        for r in range(self.out_table.rowCount()):
            port = self.out_table.cellWidget(r, 1).currentText().strip()
            if port:
                out.append({"state": self.out_table.cellWidget(r, 0).currentData(), "port": port,
                            "line": self.out_table.cellWidget(r, 2).currentText()})
        return out

    def _out_set(self, items: list) -> None:
        self.out_table.setRowCount(0)
        for o in items:
            self._out_add(o)

    # ---------------------------------------------------------- devices
    def _devices(self) -> None:
        f = self._page("Sound devices")
        self.api = QComboBox()
        self.in_dev = QComboBox()
        self.out_dev = QComboBox()
        self.ch_left = QComboBox()
        self.ch_right = QComboBox()
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self._dev_refresh)
        test = QPushButton("Test (record 3 s, play back)")
        test.clicked.connect(self._dev_test)
        self.dev_note = QLabel()
        self.dev_note.setWordWrap(True)
        f.addRow("Driver (host API):", _row(self.api, refresh))
        f.addRow("Recording device:", self.in_dev)
        f.addRow("Playback device:", self.out_dev)
        f.addRow("Left / mono from input channel:", self.ch_left)
        f.addRow("Right from input channel:", self.ch_right)
        f.addRow("", self._check("dev.exclusive", "WASAPI exclusive mode (lowest latency; other apps lose the "
                                                  "device)"))
        f.addRow("", self._check("dev.wait", "If the device is missing, wait for it (unattended use)"))
        f.addRow("If the device is lost for 60 s:", self._combo("dev.fallback", [
            ("Continue on the system default device", "default"), ("Stop recording", "stop")]))
        f.addRow("", test)
        f.addRow("", self.dev_note)
        self.api.currentIndexChanged.connect(self._dev_fill_lists)
        self.in_dev.currentIndexChanged.connect(self._dev_fill_channels)
        self._bind("dev.host_api", lambda: self.api.currentData() or "", self._dev_set_api)
        self._bind("dev.input", lambda: self.in_dev.currentData() or "",
                   lambda v: self.in_dev.setCurrentIndex(max(0, self.in_dev.findData(v))))
        self._bind("dev.output", lambda: self.out_dev.currentData() or "",
                   lambda v: self.out_dev.setCurrentIndex(max(0, self.out_dev.findData(v))))
        self._bind("dev.input_channels", self._ch_get, self._ch_set)
        self._dev_fill_apis()

    def _dev_fill_apis(self) -> None:
        self.api.blockSignals(True)
        self.api.clear()
        try:
            default = self.backend.default_host_api()
            for a in self.backend.host_apis():
                self.api.addItem(a + (" (default)" if a == default else ""), a)
        except Exception as exc:
            self.dev_note.setText(f"Sound devices could not be listed: {exc}")
        self.api.blockSignals(False)

    def _dev_set_api(self, v: str) -> None:
        if not v:
            try:
                v = self.backend.default_host_api()
            except Exception:
                v = ""
        self.api.setCurrentIndex(max(0, self.api.findData(v)))
        self._dev_fill_lists()

    def _dev_fill_lists(self) -> None:
        api = self.api.currentData()
        for combo, kind in ((self.in_dev, "input"), (self.out_dev, "output")):
            cur = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("System default", "")
            try:
                for d in self.backend.devices(kind, api):
                    ch = d.max_in if kind == "input" else d.max_out
                    combo.addItem(f"{d.name}  ({ch} ch, {d.default_rate / 1000:g} kHz)", d.name)
            except Exception:
                pass
            combo.setCurrentIndex(max(0, combo.findData(cur)))
            combo.blockSignals(False)
        self._dev_fill_channels()

    def _dev_fill_channels(self) -> None:
        api = self.api.currentData()
        dev = None
        try:
            dev = self.backend.find("input", api, self.in_dev.currentData() or "")
        except Exception:
            pass
        n = dev.max_in if dev else 2
        cur = (self.ch_left.currentData(), self.ch_right.currentData())
        for combo, label in ((self.ch_left, "Default"), (self.ch_right, "Default")):
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(label, -1)
            for i in range(max(1, n)):
                combo.addItem(f"Channel {i + 1}", i)
            combo.blockSignals(False)
        self.ch_left.setCurrentIndex(max(0, self.ch_left.findData(cur[0] if cur[0] is not None else -1)))
        self.ch_right.setCurrentIndex(max(0, self.ch_right.findData(cur[1] if cur[1] is not None else -1)))

    def _ch_get(self) -> list:
        left, right = self.ch_left.currentData(), self.ch_right.currentData()
        if left is None or left < 0:
            return []
        return [left] + ([right] if right is not None and right >= 0 else [])

    def _ch_set(self, v: list) -> None:
        self.ch_left.setCurrentIndex(max(0, self.ch_left.findData(v[0] if v else -1)))
        self.ch_right.setCurrentIndex(max(0, self.ch_right.findData(v[1] if len(v) > 1 else -1)))

    def _dev_refresh(self) -> None:
        if self.engine.recorder.input_open or self.engine.player.stream is not None:
            self.dev_note.setText("Stop recording, monitoring and playback first: refreshing closes all audio "
                                  "streams.")
            return
        try:
            self.backend.rescan()
        except Exception as exc:
            self.dev_note.setText(f"Refresh failed: {exc}")
        self._dev_fill_apis()
        self._dev_set_api(self.api.currentData() or "")
        self.dev_note.setText("Device list refreshed.")

    def _dev_test(self) -> None:
        """Record 3 s from the chosen input and play it back."""
        if self.engine.capturing or self.engine.recorder.input_open:
            self.dev_note.setText("Stop recording and monitoring first.")
            return
        api = self.api.currentData()
        din = self.backend.find("input", api, self.in_dev.currentData() or "")
        dout = self.backend.find("output", api, self.out_dev.currentData() or "")
        if din is None or dout is None:
            self.dev_note.setText("The selected device was not found.")
            return
        rate = int(din.default_rate) or 48000
        buf = []
        try:
            s = self.backend.open_input(din, rate, 1, 1024, "high", lambda d, *_: buf.append(d.copy()))
        except Exception as exc:
            self.dev_note.setText(f"Could not open {din.name}: {exc}")
            return
        self.dev_note.setText("Recording 3 seconds\u2026 speak now.")

        def finish():
            s.stop()
            s.close()
            data = np.concatenate(buf) if buf else np.zeros((0, 1), np.float32)
            peak = float(np.max(np.abs(data))) if data.size else 0.0
            pos = [0]

            def cb(out, frames, *_):
                n = min(frames, data.shape[0] - pos[0])
                out.fill(0)
                if n > 0:
                    out[:n, 0] = data[pos[0]:pos[0] + n, 0]
                    if out.shape[1] > 1:
                        out[:n, 1] = data[pos[0]:pos[0] + n, 0]
                pos[0] += n
            try:
                o = self.backend.open_output(dout, rate, min(2, dout.max_out or 2), 1024, "high", cb)
            except Exception as exc:
                self.dev_note.setText(f"Could not open {dout.name}: {exc}")
                return
            self.dev_note.setText(f"Playing back\u2026 peak level {peak * 100:.0f}.")
            warn = " \u2014 no signal!" if peak < 0.001 else ""
            done_text = f"Test finished. Peak level {peak * 100:.0f}{warn}."
            QTimer.singleShot(3300, lambda: (o.stop(), o.close(), self.dev_note.setText(done_text)))
        QTimer.singleShot(3000, finish)

    # --------------------------------------------------- live transcript (1.1.0)
    def _transcript(self) -> None:
        f = self._page("Transcript")
        self.lt_on = self._check("lt.enabled", "Transcribe while recording (live transcript)")
        f.addRow("", self.lt_on)
        self.lt_pack_label = QLabel()
        self.lt_pack_label.setWordWrap(True)
        self.lt_pack_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        check = QPushButton("Check pack files")
        check.setToolTip("Compare every file of the Live Transcript pack with its SHA-256 checksum")
        check.clicked.connect(self._lt_verify)
        f.addRow("Speech pack:", self.lt_pack_label)
        f.addRow("", _row(check))
        about = QLabel("Speech is recognised on this PC by the Live Transcript pack: nothing is sent over the "
                       "internet. English, Cantonese and Mandarin, also mixed in one sentence. Words appear a "
                       "moment after they are spoken; the text is corrected when each phrase ends.")
        about.setWordWrap(True)
        f.addRow(about)
        f.addRow("Language:", self._combo("lt.language", [(label, code) for code, label, _sv in LANGUAGES]))
        f.addRow("Chinese characters:", self._combo("lt.script", [(label, code) for code, label in SCRIPTS]))
        self._lt_names: list = []
        self.lt_names_label = QLabel()
        names_btn = QPushButton("Edit names…")
        names_btn.clicked.connect(self._lt_edit_names)
        f.addRow("Names and terms:", _row(self.lt_names_label, names_btn))

        def names_set(v):
            self._lt_names = [dict(heard=str(n.get("heard", "")), correct=str(n.get("correct", "")))
                              for n in (v or []) if isinstance(n, dict)]
            self._lt_names_text()
        self._bind("lt.names", lambda: list(self._lt_names), names_set)
        # what to save
        g = QGroupBox("What to save")
        gl = QVBoxLayout(g)
        self.lt_save_group = QButtonGroup(self)
        radios = {}
        for value, text, note in (
                ("audio_text", "Audio and text", "The recording and its transcript are both kept."),
                ("text_check", "Text, with audio kept for checking",
                 "The audio goes to the Recycle Bin automatically after the days set below, or when you choose "
                 "Transcript > Mark Checked and Delete Audio."),
                ("text_only", "Text only",
                 "No sound is saved at all: only the transcript (.txt). Vox, segments and editing need audio, "
                 "so they do not apply.")):
            r = QRadioButton(text)
            r.setToolTip(note)
            self.lt_save_group.addButton(r)
            radios[value] = r
            gl.addWidget(r)
            lab = QLabel(note)
            lab.setWordWrap(True)
            lab.setContentsMargins(24, 0, 0, 4)
            gl.addWidget(lab)
        self.lt_keep = self._spin("lt.keep_days", 1, 365, " days")
        gl.addWidget(_row(QLabel("Keep audio for checking for:"), self.lt_keep))
        gl.addWidget(self._check("lt.files_srt", "Also save subtitles (.srt) next to the audio"))

        def save_get():
            for v, r in radios.items():
                if r.isChecked():
                    return v
            return "text_check"

        def save_set(v):
            radios.get(v, radios["text_check"]).setChecked(True)
            self.lt_keep.setEnabled(save_get() == "text_check")
        for r in radios.values():
            r.toggled.connect(lambda _on: self.lt_keep.setEnabled(save_get() == "text_check"))
        self._bind("lt.save", save_get, save_set)
        f.addRow(g)
        # privacy
        g = QGroupBox("Privacy")
        gf = QFormLayout(g)
        enc = self._check("lt.encrypt", "Encrypt new recordings and transcripts with Windows encryption (EFS)")
        enc.setToolTip("Only your Windows account can open the files. Needs Windows Pro, Enterprise or Education "
                       "and an NTFS drive. Back up your encryption certificate (Windows: Manage file encryption "
                       "certificates), or the files cannot be opened after Windows is reinstalled.")
        gf.addRow(enc)
        if not winutil.IS_WINDOWS:
            enc.setEnabled(False)
        gf.addRow(self._check("lt.mask", "Mask phone, ID and card numbers and e-mail addresses in the transcript"))
        dd = self._spin("lt.delete_days", 0, 3650, " days", special="Never")
        dd.setToolTip("Transcripts (.txt and .srt) older than this go to the Recycle Bin.")
        gf.addRow("Delete transcripts after:", _row(dd))
        off = QLabel("Off the record: pause the recording (R, or the global hot-key Ctrl+Alt+R by default). "
                     "Nothing is recorded or transcribed until you resume; the transcript shows where the "
                     "pause was.")
        off.setWordWrap(True)
        gf.addRow(off)
        f.addRow(g)
        # performance
        g = QGroupBox("Performance")
        gf = QFormLayout(g)
        gf.addRow("", self._check("lt.partial", "Show words while they are spoken (uses more processor time)"))
        gf.addRow("Processor threads:", _row(self._spin("lt.threads", 1, 8)))
        gf.addRow("Priority:", _row(self._combo("lt.priority", [("Below normal (recording first)", "below"),
                                                                ("Normal", "normal")])))
        f.addRow(g)
        sot = self._folder("lt.sot_folder", "Secure Offline Transcriber folder (the one with run.bat)")
        sot.setToolTip("For Transcript > Open in Secure Offline Transcriber: proofreading, a larger model, "
                       "Word export and summaries.")
        f.addRow("Secure Offline Transcriber:", sot)
        self._lt_pack_text()

    def _lt_pack_text(self, refresh: bool = False) -> None:
        st = self.engine.lt_pack_status(refresh=refresh) if hasattr(self.engine, "lt_pack_status") else \
            lt_pack.status()
        if st.ok:
            self.lt_pack_label.setText(f"Installed: {st.model_name} (pack {st.version}) in {st.folder}")
        else:
            self.lt_pack_label.setText(f"{st.reason} Download the Live Transcript pack for this version and "
                                       "unzip it into the program folder (choose Replace when asked).")

    def _lt_verify(self) -> None:
        st = self.engine.lt_pack_status(refresh=True)
        if not st.ok:
            self._lt_pack_text()
            QMessageBox.warning(self, "Live Transcript pack", st.reason)
            return
        self.setCursor(Qt.WaitCursor)
        try:
            bad = lt_pack.verify(st)
        finally:
            self.unsetCursor()
        if bad:
            QMessageBox.warning(self, "Live Transcript pack", "These files do not match their checksums; unzip "
                                "the pack again:\n" + "\n".join(bad[:20]))
        else:
            QMessageBox.information(self, "Live Transcript pack", f"All {len(st.files)} files are complete.")

    def _lt_names_text(self) -> None:
        n = len(self._lt_names)
        self.lt_names_label.setText("None" if not n else f"{n} name{'s' if n != 1 else ''}")

    def _lt_edit_names(self) -> None:
        from vrp.ui.transcript_panel import NamesDialog
        d = NamesDialog(self._lt_names, self)
        if d.exec() == QDialog.Accepted:
            self._lt_names = d.names()
            self._lt_names_text()

    # --------------------------------------------------------- advanced
    def _advanced(self) -> None:
        f = self._page("Advanced")
        f.addRow("Audio block size:", self._combo("adv.blocksize", [(str(n), n) for n in
                                                                   (256, 512, 1024, 2048, 4096)]))
        f.addRow("Latency:", self._combo("adv.latency", [("High (most stable)", "high"), ("Low", "low")]))
        f.addRow("Write to disk every:", self._spin("rec.flush_s", 1, 30, " s"))
        f.addRow("Force to disk (fsync) every:", self._spin("rec.fsync_s", 1, 30, " s"))
        f.addRow("Log detail:", self._combo("adv.log_level", [(x, x) for x in ("DEBUG", "INFO", "WARNING", "ERROR")]))
        f.addRow("Keep logs for:", self._spin("adv.log_days", 1, 365, " days"))
        logs = QPushButton("Open log folder")
        logs.clicked.connect(lambda: winutil.open_folder(paths.log_dir()))
        cfg = QPushButton("Open settings folder")
        cfg.clicked.connect(lambda: winutil.open_folder(paths.config_dir()))
        f.addRow("", _row(logs, cfg))
        f.addRow("Late start window for schedules:", self._spin("sched.late_minutes", 0, 60, " min"))

    # ============================================================ load/apply
    def load(self) -> None:
        s = self.settings
        for key, setter in self.setters.items():
            if key == "__fmt__":
                setter(FormatSpec.from_settings(s))
            elif key == "__sources__":
                setter(s)
            else:
                setter(s.get(key))
        self._update_preview()
        if hasattr(self, "_keep_state"):
            self._keep_state()

    def values(self) -> dict:
        vals = {}
        for key, getter in self.getters.items():
            if key == "__fmt__":
                spec = getter()
                vals.update({"fmt.codec": spec.codec, "fmt.rate": spec.rate, "fmt.channels": spec.channels,
                             "fmt.layout": spec.layout,
                             "fmt.wav_subtype": spec.wav_subtype, "fmt.flac_bits": spec.flac_bits,
                             "fmt.flac_level": spec.flac_level, "fmt.mp3.mode": spec.mp3_mode,
                             "fmt.mp3.kbps": spec.mp3_kbps, "fmt.mp3.vbr_q": spec.mp3_vbr_q})
            elif key == "__sources__":
                vals.update(getter())
            else:
                vals[key] = getter()
        return vals

    def apply(self) -> bool:
        vals = self.values()
        err = naming.validate_pattern(vals["rec.pattern"])
        if err:
            self.tabs.setCurrentIndex(1)
            QMessageBox.warning(self, "File names", err)
            return False
        clock = vals.get("rec.autostop.clock", "")
        if clock:
            try:
                hh, mm = (int(x) for x in clock.split(":")[:2])
                if not (0 <= hh < 24 and 0 <= mm < 60):
                    raise ValueError(clock)
            except ValueError:
                QMessageBox.warning(self, "Stop time", "Enter the stop time as HH:MM, e.g. 17:30.")
                return False
        if vals["rec.disk_stop_mb"] >= vals["rec.disk_warn_mb"]:
            vals["rec.disk_warn_mb"] = vals["rec.disk_stop_mb"] + 50
        for folder_key in ("rec.folder", "rec.fallback_folder"):
            folder = vals.get(folder_key)
            if folder:
                try:
                    os.makedirs(folder, exist_ok=True)
                except OSError as exc:
                    QMessageBox.warning(self, "Folder", f"{folder} cannot be used: {exc}")
                    return False
        if vals.get("general.run_at_login") != self.settings.get("general.run_at_login"):
            winutil.set_run_at_login(vals["general.run_at_login"])
        if self.engine.capturing and any(vals.get(k) != self.settings.get(k) for k in vals if k.startswith("dev.")):
            QMessageBox.information(self, "Sound devices", "The device change applies when the recording stops.")
        self.settings.update(vals)
        return True

    def _ok(self) -> None:
        if self.apply():
            self.accept()

    def _restore(self) -> None:
        tab = self.tabs.tabText(self.tabs.currentIndex())
        prefixes = TAB_PREFIXES.get(tab)
        if not prefixes:
            return
        if QMessageBox.question(self, "Restore defaults", f"Restore the default {tab} settings?") != \
                QMessageBox.Yes:
            return
        self.settings.restore_defaults(prefixes)
        if tab == "Format":
            self.setters["__fmt__"](FormatSpec.from_settings(self.settings))
        self.load()

    def _export(self) -> None:
        p, _ = QFileDialog.getSaveFileName(self, "Export settings", "VRP settings.vrpsettings",
                                           "VRP settings (*.vrpsettings)")
        if p:
            self.apply()
            self.settings.export_to(p)

    def _import(self) -> None:
        p, _ = QFileDialog.getOpenFileName(self, "Import settings", "", "VRP settings (*.vrpsettings);;All (*)")
        if not p:
            return
        try:
            clamped = self.settings.import_from(p)
        except Exception as exc:
            QMessageBox.warning(self, "Import", f"The file could not be imported: {exc}")
            return
        self.load()
        if clamped:
            QMessageBox.information(self, "Import", "Some values were out of range and were corrected: "
                                    + ", ".join(clamped))

    def done(self, r) -> None:
        self.ext_timer.stop()
        if self._watched:
            self.ext.watch(self._watched, False)
        self.ext.stop_learn()
        try:
            self.ext.learned.disconnect(self._learned)
        except Exception:
            pass
        super().done(r)


__all__ = ["OptionsDialog"]
