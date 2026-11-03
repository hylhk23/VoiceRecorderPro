"""Main window (Section 4) and menus (Section 5)."""
from __future__ import annotations

import os
import time

from PySide6.QtCore import QSize, Qt, QUrl
from PySide6.QtGui import QAction, QActionGroup, QDesktopServices, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QCheckBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QInputDialog,
                               QLabel, QMainWindow, QMenu, QMessageBox, QProgressDialog, QPushButton,
                               QSizePolicy, QSlider, QSystemTrayIcon, QToolButton, QVBoxLayout, QWidget)

from vrp import APP_NAME, __version__, paths, winutil
from vrp.core.audiofile import UnsupportedFile, probe
from vrp.core.dsp import percent_to_dbfs
from vrp.core.editor import format_of
from vrp.core.engine import CAPTURE, PLAYBACK, STATE_LABEL, State
from vrp.core.model import FormatSpec, fmt_duration, fmt_size, fmt_time_precise
from vrp.settings import HOTKEY_ACTIONS
from vrp.ui import icons
from vrp.ui.dialogs import (FormatDialog, JoinDialog, PropertiesDialog, ScheduleDialog, ShortcutsDialog,
                            TimedDialog)
from vrp.ui.dialogs import AboutDialog
from vrp.ui.taskbar import TaskbarBadge
from vrp.ui.widgets import LevelMeter, PositionSlider

SPEEDS = (50, 75, 100, 125, 150, 200)


class InfoField(QLabel):
    def __init__(self, name: str):
        super().__init__()
        self.setFrameStyle(QFrame.StyledPanel | QFrame.Sunken)
        self.setMinimumWidth(124)
        self.setMinimumHeight(22)
        self.setContentsMargins(4, 0, 4, 0)
        self.setTextInteractionFlags(Qt.NoTextInteraction)
        self.setAccessibleName(name)


class MainWindow(QMainWindow):
    def __init__(self, app_ctl):
        super().__init__()
        self.ctl = app_ctl
        self.engine = app_ctl.engine
        self.settings = app_ctl.settings
        self.exiting = False
        self._flash_text = ""
        self._flash_until = 0.0
        self._progress: QProgressDialog | None = None
        self._tray_hint_shown = False
        self._window_shortcuts: list[QShortcut] = []
        self.setWindowIcon(icons.app_icon())
        self._badge_kind: str | None = None
        self.badge = TaskbarBadge(self, icons.badge_image)
        self._build_ui()
        self._build_menus()
        self._build_tray()
        self._connect()
        self.engine.ask_append = self._ask_append
        self._apply_settings(set(self.settings.snapshot()))
        self.settings.add_listener(self._apply_settings)
        self.refresh_all()
        geo = self.settings.get("general.window_geometry")
        if geo:
            try:
                x, y, w = (int(v) for v in geo.split(","))
                self.move(x, y)
                self.resize(w, self.height())
            except ValueError:
                pass

    # ===================================================================== UI
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 4, 10, 10)
        root.setSpacing(6)
        top = QHBoxLayout()
        top.addStretch()
        self.fmt_btn = QToolButton()
        self.fmt_btn.setAutoRaise(True)
        self.fmt_btn.setFocusPolicy(Qt.NoFocus)
        f = self.fmt_btn.font()
        f.setBold(True)
        self.fmt_btn.setFont(f)
        self.fmt_btn.setToolTip("Output format — click to change")
        self.fmt_btn.setAccessibleName("Output format")
        self.fmt_btn.clicked.connect(lambda: self.ctl.open_options("Format"))
        top.addWidget(self.fmt_btn)
        root.addLayout(top)

        grid = QGridLayout()
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(4)
        self.vox_value = QLabel("4")
        self.vox_value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.vox_value.setMinimumWidth(22)
        self.vox_slider = QSlider(Qt.Vertical)
        self.vox_slider.setRange(0, 100)
        self.vox_slider.setFocusPolicy(Qt.NoFocus)
        self.vox_slider.setAccessibleName("Vox threshold")
        self.vox_slider.setMinimumHeight(110)
        self.vox_btn = QPushButton("&Vox")
        self.vox_btn.setFlat(True)
        self.vox_btn.setCheckable(True)
        self.vox_btn.setFocusPolicy(Qt.NoFocus)
        self.vox_btn.setToolTip("Voice activation on/off (Alt+V)")
        self.vox_btn.setAccessibleName("Vox on or off")
        self.meter = LevelMeter()
        self.meter.setAccessibleName("Microphone level")
        self.meter.setToolTip("Microphone level (the playback level while playing)")
        self.meter_sys = LevelMeter()
        self.meter_sys.setAccessibleName("Computer audio level")
        self.meter_sys.setToolTip("Computer audio level: what plays through your speakers or headphones")
        # recording sources: tick or untick at any time, also while recording (v1.0.2)
        self.src_mic = QCheckBox("Mic")
        self.src_mic.setToolTip("Record the microphone.\nTick or untick at any time, also while recording.")
        self.src_mic.setAccessibleName("Record the microphone")
        self.src_sys = QCheckBox("Computer")
        self.src_sys.setToolTip("Record computer audio: what plays through your speakers or headphones\n"
                                "(calls, videos, music). Tick or untick at any time, also while recording.")
        self.src_sys.setAccessibleName("Record computer audio")
        for cb in (self.src_mic, self.src_sys):
            cb.setFocusPolicy(Qt.NoFocus)        # keep Space and R for the transport
        ok, why = self.engine.loopback_support()
        if not ok:
            self.src_sys.setEnabled(False)
            self.src_sys.setToolTip(f"Computer audio cannot be recorded here: {why}")
        meters = QGridLayout()
        meters.setHorizontalSpacing(8)
        meters.setVerticalSpacing(2)
        meters.addWidget(self.src_mic, 0, 0, Qt.AlignHCenter)
        meters.addWidget(self.src_sys, 0, 1, Qt.AlignHCenter)
        meters.addWidget(self.meter, 1, 0, Qt.AlignHCenter)
        meters.addWidget(self.meter_sys, 1, 1, Qt.AlignHCenter)
        meters.setRowStretch(1, 1)
        self.level_value = QLabel("0")
        self.level_value.setMinimumWidth(30)
        self.level_value.setAccessibleName("Level")
        info = QGridLayout()
        info.setVerticalSpacing(6)
        self.f_segment = InfoField("Segment")
        self.f_sample = InfoField("Sample")
        self.f_size = InfoField("Size")
        for r, (label, w) in enumerate((("Segment", self.f_segment), ("Sample", self.f_sample),
                                        ("Size", self.f_size))):
            lab = QLabel(label)
            lab.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            info.addWidget(lab, r, 0)
            info.addWidget(w, r, 1)
        self.disk = QLabel()
        self.disk.setAlignment(Qt.AlignCenter)
        self.disk.setAccessibleName("Disk status")
        grid.addWidget(self.vox_value, 0, 0, 2, 1)
        grid.addWidget(self.vox_slider, 0, 1, 2, 1, Qt.AlignHCenter)
        grid.addLayout(meters, 0, 2, 2, 1)
        grid.addWidget(self.level_value, 0, 3, 2, 1)
        grid.addLayout(info, 0, 4)
        grid.addWidget(self.disk, 1, 4)
        grid.addWidget(self.vox_btn, 2, 0, 1, 3, Qt.AlignHCenter)
        grid.setColumnStretch(4, 1)
        root.addLayout(grid)

        pos = QHBoxLayout()
        pos.addWidget(QLabel("Position"))
        self.position = PositionSlider()
        pos.addWidget(self.position, 1)
        root.addLayout(pos)

        frame = QFrame()
        frame.setFrameStyle(QFrame.StyledPanel | QFrame.Sunken)
        tl = QHBoxLayout(frame)
        tl.setContentsMargins(6, 6, 6, 6)
        tl.setSpacing(6)
        self.buttons = {}
        for key, tip in (("record", "Record"), ("play", "Play"), ("rewind", "Rewind"), ("stop", "Stop"),
                         ("ffwd", "Fast Forward")):
            b = QToolButton()
            b.setIconSize(QSize(22, 22))
            b.setMinimumSize(44, 32)
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            b.setFocusPolicy(Qt.NoFocus)
            b.setAccessibleName(tip)
            b.setToolTip(tip)
            tl.addWidget(b)
            self.buttons[key] = b
        self.buttons["record"].setCheckable(True)
        root.addWidget(frame)
        self._update_icons()
        self.setMinimumWidth(300)

    def _update_icons(self) -> None:
        ink = self.palette().windowText().color()
        playing = self.engine.state == State.PLAYING
        self.buttons["record"].setIcon(icons.transport_icon("record", ink))
        self.buttons["play"].setIcon(icons.transport_icon("pause" if playing else "play", ink))
        self.buttons["rewind"].setIcon(icons.transport_icon("rewind", ink))
        self.buttons["stop"].setIcon(icons.transport_icon("stop", ink))
        self.buttons["ffwd"].setIcon(icons.transport_icon("ffwd", ink))

    # ================================================================== menus
    def _act(self, menu: QMenu, text: str, fn=None, shortcut: str | None = None, checkable: bool = False,
             key: str | None = None) -> QAction:
        a = QAction(text, self)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
            a.setShortcutContext(Qt.WindowShortcut)
        a.setCheckable(checkable)
        if fn is not None:
            a.triggered.connect(fn)
        menu.addAction(a)
        self.addAction(a)
        if key:
            self.actions_by_key[key] = a
        return a

    def _build_menus(self) -> None:
        mb = self.menuBar()
        self.actions_by_key: dict[str, QAction] = {}
        e = self.engine
        # File
        m = mb.addMenu("&File")
        self._act(m, "&New Recording", lambda: e.new_recording(), "Ctrl+N", key="new")
        self._act(m, "&Open…", self.open_dialog, "Ctrl+O", key="open")
        self.recent_menu = m.addMenu("Open &Recent")
        self.recent_menu.aboutToShow.connect(self._fill_recent)
        self._act(m, "Open Recordings &Folder", self._open_rec_folder, "Ctrl+Shift+O")
        m.addSeparator()
        self._act(m, "Save Copy &As…", self.save_copy_as, "Ctrl+Shift+S", key="savecopy")
        self._act(m, "&Export Segments…", self.export_segments, key="export")
        self._act(m, "Re&name…", self.rename, "F2", key="rename")
        self._act(m, "File &Properties…", self.properties, "Alt+Return", key="props")
        self._act(m, "&Delete File…", self.delete_file, "Shift+Del", key="delete_file")
        m.addSeparator()
        self._act(m, "&Close", lambda: e.close_file(), "Ctrl+W", key="close")
        self._act(m, "E&xit", self.exit_app, "Ctrl+Q")
        # Edit
        m = mb.addMenu("&Edit")
        self._act(m, "&Undo Last Edit", lambda: e.edit_undo(), "Ctrl+Z", key="undo")
        m.addSeparator()
        self._act(m, "Set &In Mark", lambda: e.set_mark_in(), "[", key="mark_in")
        self._act(m, "Set &Out Mark", lambda: e.set_mark_out(), "]", key="mark_out")
        self._act(m, "Select Current &Segment", lambda: e.select_segment(), "Ctrl+G", key="sel_seg")
        self._act(m, "Select &All", lambda: e.select_all(), "Ctrl+A", key="sel_all")
        self._act(m, "&Clear Selection", lambda: e.clear_selection(), "Esc", key="sel_clear")
        self._act(m, "&Loop Selection", lambda on: e.set_loop(on), checkable=True, key="loop")
        m.addSeparator()
        self._act(m, "E&xtract Selection to File…", self.extract_to_file, "Ctrl+E", key="extract")
        self._act(m, "Extract and Op&en", lambda: e.edit_extract(open_after=True), "Ctrl+Shift+E",
                  key="extract_open")
        self._act(m, "&Delete Selection", lambda: e.edit_delete(), "Del", key="del_sel")
        self._act(m, "Cro&p to Selection", lambda: e.edit_crop(), "Ctrl+Shift+K", key="crop")
        self._act(m, "Spli&t at Position", self.split, "Ctrl+T", key="split")
        self._act(m, "&Join Files…", self.join_files, "Ctrl+J", key="join")
        self._act(m, "Insert Segment &Boundary", lambda: e.insert_boundary(), "M", key="boundary")
        # Control
        m = mb.addMenu("&Control")
        self._act(m, "&Record", lambda: e.record(), "R", key="record")
        self._act(m, "&Play", lambda: e.play_pause(), "Space", key="play")
        self._act(m, "&Stop", lambda: e.stop(), "S", key="stop")
        self._act(m, "Re&wind\tLeft (hold)", lambda: e.skip(-self.settings.get("play.skip_s")), key="rewind")
        self._act(m, "&Fast Forward\tRight (hold)", lambda: e.skip(self.settings.get("play.skip_s")), key="ffwd")
        self._act(m, "Skip &Back", lambda: e.skip(-self.settings.get("play.skip_s")), "Ctrl+Left", key="skipb")
        self._act(m, "Skip For&ward", lambda: e.skip(self.settings.get("play.skip_s")), "Ctrl+Right",
                  key="skipf")
        self._act(m, "Pre&vious Segment", lambda: e.prev_segment(), "PgUp", key="prevseg")
        self._act(m, "Ne&xt Segment", lambda: e.next_segment(), "PgDown", key="nextseg")
        self._act(m, "Go to S&tart", lambda: e.go_start(), "Home", key="start")
        self._act(m, "Go to &End", lambda: e.go_end(), "End", key="end")
        m.addSeparator()
        self._act(m, "&Vox On/Off\tAlt+V", lambda: e.toggle_vox(), checkable=True, key="vox")
        self._act(m, "&Monitor Input", lambda on: e.monitor(on), "Ctrl+M", checkable=True, key="monitor")
        self._act(m, "Record M&icrophone", lambda on: self.settings.set("rec.src_mic", on), checkable=True,
                  key="src_mic")
        self._act(m, "Record Computer A&udio", lambda on: self.settings.set("rec.src_system", on),
                  checkable=True, key="src_system")
        sm = m.addMenu("Playback S&peed")
        grp = QActionGroup(self)
        self.speed_actions = {}
        for sp in SPEEDS:
            a = QAction(f"{sp}%", self, checkable=True)
            a.triggered.connect(lambda _c, v=sp: e.set_speed(v))
            grp.addAction(a)
            sm.addAction(a)
            self.speed_actions[sp] = a
        m.addSeparator()
        self._act(m, "T&imed Recording…", self.timed_recording, "Ctrl+Shift+T", key="timed")
        self._act(m, "Sche&dule…", self.schedule, "Ctrl+K")
        # Options
        m = mb.addMenu("&Options")
        for text, tab in (("&Format…", "Format"), ("&Recording…", "Recording"), ("&Vox…", "Vox"),
                          ("&Hot-keys…", "Hot-keys"), ("E&xternal Devices…", "External devices"),
                          ("&Sound Devices…", "Sound devices")):
            self._act(m, text, lambda _c=False, t=tab: self.ctl.open_options(t))
        m.addSeparator()
        for text, key in (("&Disable Vox", "vox.disabled"), ("Hi&de Vox", "vox.hidden"),
                          ("&Time-stamp Segments", "rec.timestamps"), ("Always on &Top", "general.always_on_top"),
                          ("&Close to Tray", "general.close_to_tray"), ("Start &Minimised", "general.start_minimised"),
                          ("Run at &Login", "general.run_at_login")):
            self._act(m, text, lambda on, k=key: self._toggle_setting(k, on), checkable=True, key=key)
        m.addSeparator()
        self._act(m, "&All Settings…", lambda: self.ctl.open_options(None), "Ctrl+,")
        # Help
        m = mb.addMenu("&Help")
        self._act(m, "&Help Contents", self.help_contents, "F1")
        self._act(m, "&Keyboard Shortcuts", self.shortcuts, "Ctrl+/")
        self._act(m, "Open &Log Folder", lambda: winutil.open_folder(paths.log_dir()))
        self._act(m, "Check for &Updates", self.check_updates)
        m.addSeparator()
        self._act(m, "&About", self.about)

    def _toggle_setting(self, key: str, on: bool) -> None:
        if key == "general.run_at_login":
            winutil.set_run_at_login(on)
        self.settings.set(key, on)

    def _fill_recent(self) -> None:
        self.recent_menu.clear()
        files = self.settings.recent_files()
        if not files:
            a = self.recent_menu.addAction("(none)")
            a.setEnabled(False)
        for p in files:
            a = self.recent_menu.addAction(os.path.basename(p))
            a.setToolTip(p)
            a.triggered.connect(lambda _c=False, path=p: self.engine.open_file(path))

    # =================================================================== tray
    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(icons.tray_icon("idle"), self)
        menu = QMenu()
        e = self.engine
        self.tray_actions = {
            "record": menu.addAction("Record", e.record),
            "stop": menu.addAction("Stop", e.stop),
            "play": menu.addAction("Play", e.play_pause),
        }
        self.tray_vox = menu.addAction("Vox On/Off", e.toggle_vox)
        self.tray_vox.setCheckable(True)
        self.tray_src = {
            "rec.src_mic": menu.addAction("Microphone", lambda: e.toggle_source("mic")),
            "rec.src_system": menu.addAction("Computer Audio", lambda: e.toggle_source("system")),
        }
        for a in self.tray_src.values():
            a.setCheckable(True)
        menu.addSeparator()
        menu.addAction("Open Recordings Folder", self._open_rec_folder)
        self.tray_suspend = menu.addAction("Suspend Hot-keys", lambda: self.ctl.toggle_suspend())
        self.tray_suspend.setCheckable(True)
        menu.addSeparator()
        menu.addAction("Show Window", self.show_window)
        menu.addAction("Exit", self.exit_app)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

    def _tray_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            if self.isVisible() and not self.isMinimized():
                self.hide_to_tray()
            else:
                self.show_window()

    def show_window(self) -> None:
        self.show()
        self.setWindowState(self.windowState() & ~Qt.WindowMinimized)
        self.raise_()
        self.activateWindow()
        self.engine.window_visible = True

    def hide_to_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.showMinimized()
            return
        self.hide()
        self.engine.window_visible = False
        if not self._tray_hint_shown:
            self._tray_hint_shown = True
            self.tray.showMessage(APP_NAME, "Still running in the notification area. Click the icon to show the "
                                            "window.", QSystemTrayIcon.Information, 4000)

    def toggle_window(self) -> None:
        if self.isVisible() and not self.isMinimized():
            self.hide_to_tray()
        else:
            self.show_window()

    # ================================================================ signals
    def _connect(self) -> None:
        e = self.engine
        e.stateChanged.connect(lambda _s: self.refresh_all())
        e.fileChanged.connect(self.refresh_all)
        e.selectionChanged.connect(self.refresh_all)
        e.ticked.connect(self.on_tick)
        e.message.connect(self.show_message)
        e.notify.connect(lambda t, m: self.tray.showMessage(t, m, QSystemTrayIcon.Information, 5000))
        e.editStarted.connect(self._edit_started)
        e.editProgress.connect(self._edit_progress)
        e.editFinished.connect(self._edit_finished)
        self.vox_slider.valueChanged.connect(self._threshold_changed)
        self.vox_btn.clicked.connect(lambda: e.toggle_vox())
        self.src_mic.clicked.connect(lambda on: self.settings.set("rec.src_mic", bool(on)))
        self.src_sys.clicked.connect(lambda on: self.settings.set("rec.src_system", bool(on)))
        self.position.seekRequested.connect(e.seek)
        self.position.selectionRequested.connect(e.set_selection)
        b = self.buttons
        b["record"].clicked.connect(lambda: e.record())
        b["play"].clicked.connect(lambda: e.play_pause())
        b["stop"].clicked.connect(lambda: e.stop())
        b["rewind"].pressed.connect(lambda: e.rewind(True))
        b["rewind"].released.connect(lambda: e.rewind(False))
        b["ffwd"].pressed.connect(lambda: e.ffwd(True))
        b["ffwd"].released.connect(lambda: e.ffwd(False))

    def _threshold_changed(self, v: int) -> None:
        if v != self.settings.get("vox.threshold"):
            self.engine.set_threshold(v)
        self.meter.threshold = self.meter_sys.threshold = v
        self._update_vox_value(v)

    def _update_vox_value(self, v: int) -> None:
        if self.settings.get("ui.meter_scale") == "db":
            self.vox_value.setText(f"{percent_to_dbfs(v):.0f}" if v else "-∞")
        else:
            self.vox_value.setText(str(v))
        self.vox_slider.setToolTip(f"Vox threshold {v} ({percent_to_dbfs(v):.1f} dBFS)" if v else
                                   "Vox threshold 0 (any sound)")

    def _apply_settings(self, keys: set) -> None:
        s = self.settings
        if "vox.threshold" in keys:
            v = s.get("vox.threshold")
            if self.vox_slider.value() != v:
                self.vox_slider.blockSignals(True)
                self.vox_slider.setValue(v)
                self.vox_slider.blockSignals(False)
            self.meter.threshold = self.meter_sys.threshold = v
            self._update_vox_value(v)
        if keys & {"vox.hidden", "ui.meter_scale", "vox.disabled", "vox.enabled"}:
            hidden = s.get("vox.hidden")
            for w in (self.vox_value, self.vox_slider, self.vox_btn):
                w.setVisible(not hidden)
            for mtr in (self.meter, self.meter_sys):
                mtr.setMinimumWidth(46 if hidden else 26)
                mtr.db_scale = s.get("ui.meter_scale") == "db"
            self._update_vox_value(s.get("vox.threshold"))
        if keys & {"rec.src_mic", "rec.src_system"}:
            for key, cb in (("rec.src_mic", self.src_mic), ("rec.src_system", self.src_sys)):
                on = bool(s.get(key))
                if cb.isChecked() != on:
                    cb.blockSignals(True)
                    cb.setChecked(on)
                    cb.blockSignals(False)
                self.tray_src[key].setChecked(on)
                self.actions_by_key["src_mic" if key == "rec.src_mic" else "src_system"].setChecked(on)
        if "general.always_on_top" in keys:
            on = s.get("general.always_on_top")
            if bool(self.windowFlags() & Qt.WindowStaysOnTopHint) != on:
                vis = self.isVisible()
                self.setWindowFlag(Qt.WindowStaysOnTopHint, on)
                if vis:
                    self.show()
        if any(k.startswith("hotkeys.") for k in keys):
            self._rebuild_window_shortcuts()
            self._update_tooltips()
        for key in ("vox.disabled", "vox.hidden", "rec.timestamps", "general.always_on_top",
                    "general.close_to_tray", "general.start_minimised", "general.run_at_login"):
            if key in keys and key in self.actions_by_key:
                self.actions_by_key[key].setChecked(bool(s.get(key)))
        if "play.speed" in keys:
            sp = s.get("play.speed")
            for v, a in self.speed_actions.items():
                a.setChecked(v == sp)
        self.refresh_all()

    def _rebuild_window_shortcuts(self) -> None:
        for sc in self._window_shortcuts:
            sc.setEnabled(False)
            sc.deleteLater()
        self._window_shortcuts = []
        b = self.settings.get("hotkeys.bindings")
        for action, _label, _d in HOTKEY_ACTIONS:
            e = b.get(action) or {}
            if e.get("keys") and e.get("scope") == "window":
                sc = QShortcut(QKeySequence(e["keys"]), self)
                sc.activated.connect(lambda a=action: self.ctl.dispatch(a, True))
                self._window_shortcuts.append(sc)

    def _update_tooltips(self) -> None:
        b = self.settings.get("hotkeys.bindings")

        def g(action):
            k = (b.get(action) or {}).get("keys")
            return f"; global {k}" if k else ""
        self.buttons["record"].setToolTip(f"Record / pause (R{g('record')})")
        self.buttons["play"].setToolTip(f"Play / pause (Space{g('play')})")
        self.buttons["stop"].setToolTip(f"Stop (S{g('stop')})")
        self.buttons["rewind"].setToolTip(f"Rewind: click jumps back, hold scans (Left{g('rewind')})")
        self.buttons["ffwd"].setToolTip(f"Fast forward: click jumps, hold scans (Right{g('ffwd')})")

    # ================================================================ refresh
    def refresh_all(self) -> None:
        e = self.engine
        st = e.state
        f = e.file
        has_file = f is not None
        cap = st in CAPTURE
        busy = e.busy
        editable = has_file and not cap and not busy and st in (State.STOPPED, State.PLAY_PAUSED, State.PLAYING)
        sel = e.selection is not None
        b = self.buttons
        b["record"].setEnabled(st != State.SEEKING and not busy)
        b["record"].setChecked(st in (State.RECORDING, State.ARMED))
        b["play"].setEnabled(has_file and not cap and not busy)
        b["rewind"].setEnabled(has_file and not cap and not busy)
        b["ffwd"].setEnabled(has_file and not cap and not busy)
        b["stop"].setEnabled(cap or st in PLAYBACK or (has_file and e.position > 0))
        self._update_icons()
        A = self.actions_by_key
        for k in ("savecopy", "export", "rename", "props", "delete_file", "close"):
            A[k].setEnabled(has_file and not cap and not busy)
        A["undo"].setEnabled(editable and e.can_undo())
        for k in ("mark_in", "mark_out", "sel_seg", "sel_all", "sel_clear", "loop"):
            A[k].setEnabled(has_file and not cap)
        for k in ("extract", "extract_open"):
            A[k].setEnabled(editable and sel)
        for k in ("del_sel", "crop"):
            A[k].setEnabled(editable and sel and not f.readonly)
        A["split"].setEnabled(editable and 0 < e.position < (f.frames if f else 0))
        A["join"].setEnabled(not cap and not busy)
        A["boundary"].setEnabled(st in (State.ARMED, State.RECORDING) or (editable and not f.readonly))
        A["open"].setEnabled(not cap and not busy)
        A["new"].setEnabled(not cap and not busy)
        for k in ("play", "rewind", "ffwd", "skipb", "skipf", "prevseg", "nextseg", "start", "end"):
            A[k].setEnabled(has_file and not cap and not busy)
        A["record"].setEnabled(st != State.SEEKING and not busy)
        A["stop"].setEnabled(cap or st in PLAYBACK or has_file)
        A["vox"].setChecked(e.vox_enabled())
        A["vox"].setEnabled(not self.settings.get("vox.disabled"))
        A["monitor"].setChecked(e.monitoring)
        A["monitor"].setEnabled(not cap)
        A["loop"].setChecked(e.loop_selection)
        self.vox_btn.setChecked(e.vox_enabled())
        vf = self.vox_btn.font()
        vf.setStrikeOut(not e.vox_enabled())
        self.vox_btn.setFont(vf)
        self.vox_btn.setEnabled(not self.settings.get("vox.disabled"))
        self.vox_slider.setEnabled(not self.settings.get("vox.disabled"))
        self.tray_vox.setChecked(e.vox_enabled())
        self.tray_actions["play"].setEnabled(has_file and not cap)
        # title (FR-UI-001 element 1)
        name = os.path.basename(f.path) if f else "(no file)"
        mark = " [REC]" if st == State.RECORDING else " [VOX]" if st == State.ARMED else \
            " [PAUSED]" if st == State.REC_PAUSED else ""
        ro = " [Read-only]" if f and f.readonly else ""
        self.setWindowTitle(f"{name} - {APP_NAME}{mark}{ro}")
        self.tray.setIcon(icons.tray_icon(st.value))
        self._update_taskbar_badge(st)
        fmt = (f.codec.upper() if f and f.codec != "other" else
               (os.path.splitext(f.path)[1][1:].upper() if f else self.settings.get("fmt.codec").upper()))
        self.fmt_btn.setText(fmt)
        self.on_tick()

    def _update_taskbar_badge(self, st: State) -> None:
        """Red dot on the taskbar button while recording; amber while Vox waits or recording is paused.

        The window icon gets the same dot, so it still shows on the taskbar when Windows' taskbar
        badges are turned off (and in Alt+Tab and the title bar).
        """
        kind = icons.BADGE_FOR_STATE.get(st.value)
        if kind != self._badge_kind:
            self._badge_kind = kind
            self.setWindowIcon(icons.tray_icon(st.value) if kind else icons.app_icon())
        self.badge.set(kind)

    def nativeEvent(self, eventType, message):
        badge = getattr(self, "badge", None)
        if badge is not None and badge.msg_id:
            badge.on_native_message(message)      # re-apply the badge when the taskbar button is re-created
        return super().nativeEvent(eventType, message)

    def on_tick(self) -> None:
        e = self.engine
        f = e.file
        st = e.state
        now = time.monotonic()
        # meter
        levels = e.level_percent()
        input_mode = st not in PLAYBACK and e.recorder.input_open
        clip = (e.recorder.clip_until > now) if input_mode else bool(levels and max(levels) >= 99)
        show_thr = input_mode and e.vox_enabled() and not self.settings.get("vox.hidden")
        self.meter.show_threshold = show_thr
        self.meter.set_levels(levels, clip)
        self.meter_sys.show_threshold = show_thr
        self.meter_sys.set_levels(e.system_level_percent(), input_mode and e.recorder.sys_clip_until > now)
        rec_levels = e.record_level_percent()
        peak = max(rec_levels) if rec_levels else 0.0
        if self.settings.get("ui.meter_scale") == "db":
            self.level_value.setText(f"{percent_to_dbfs(peak):.0f}" if peak > 0.1 else "-∞")
        else:
            self.level_value.setText(f"{peak:.0f}")
        # info fields (FR-UI-007)
        if f is not None:
            n = len(f.segments)
            idx = n if st in CAPTURE else f.segment_index_at(e.position) + 1
            self.f_segment.setText(f"{idx} of {n}")
            self.f_sample.setText(f.sample_text())
            self.f_size.setText(f"{fmt_size(f.size)}  {fmt_duration(f.duration)}")
        else:
            spec = FormatSpec.from_settings(self.settings)
            self.f_segment.setText("")
            self.f_sample.setText(f"{spec.rate / 1000:g} kHz {'ST' if spec.channels == 2 else 'MO'}")
            self.f_size.setText("")
        tr = e.timed_remaining
        self.f_size.setToolTip(f"Timed recording: {fmt_duration(tr)} left" if tr is not None else "")
        # position (FR-UI-009)
        if f is not None:
            self.position.set_state(f.frames, e.position, f.rate, f.segment_starts(), e.selection, st in CAPTURE)
        else:
            self.position.set_state(0, 0, 16000, [], None, False)
        # disk line (FR-UI-008, FR-EDIT-002, FR-REC-011)
        self._update_disk_line(now)
        if st == State.PLAYING or st == State.SEEKING:
            self.tray.setToolTip(f"{APP_NAME}: {STATE_LABEL[st]} {os.path.basename(f.path) if f else ''} "
                                 f"{fmt_duration(e.position / f.rate) if f else ''}")
        else:
            self.tray.setToolTip(f"{APP_NAME}: {STATE_LABEL[st]}"
                                 + (f" — {os.path.basename(f.path)} {fmt_duration(f.duration)}" if f else ""))

    def _update_disk_line(self, now: float) -> None:
        e = self.engine
        color = ""
        if now < self._flash_until:
            text = self._flash_text
        elif e.selection and e.file and e.disk_status == "ok":
            a, b = e.selection
            r = e.file.rate
            text = f"Sel {fmt_time_precise(a / r)}–{fmt_time_precise(b / r)} ({(b - a) / r:.1f} s)"
        else:
            free = e.disk_free
            drive = e.disk_label or ""
            if free is None:
                text = "Free space unknown"
            elif free > 1024 ** 3:
                text = f">1GB free on {drive}"
            else:
                text = f"{free / 1024 / 1024:.0f} MB free on {drive}"
            color = {"warn": "#e69500", "stop": "#d93025"}.get(e.disk_status, "")
        if e.capturing and not any(e.sources()):
            text, color = "No source ticked — recording silence", "#e69500"
        if e.overflow_count and e.capturing:
            text = "⚠ " + text
        if e.recorder.resampled and e.recorder.input_open:
            self.f_sample.setToolTip("Captured at the device rate and resampled")
        else:
            self.f_sample.setToolTip("")
        self.disk.setText(text)
        self.disk.setStyleSheet(f"color: {color};" if color else "")
        tip = []
        if e.disk_seconds_left is not None:
            tip.append(f"About {fmt_duration(e.disk_seconds_left)} of recording left at the current format")
        if e.overflow_count:
            tip.append(f"{e.overflow_count} audio blocks were lost (the computer was too busy)")
        self.disk.setToolTip("\n".join(tip))

    def flash(self, text: str, seconds: float = 5.0) -> None:
        self._flash_text = text
        self._flash_until = time.monotonic() + seconds

    # ================================================================ keys
    def keyPressEvent(self, ev) -> None:
        if ev.isAutoRepeat():
            return
        if ev.key() == Qt.Key_Left and not ev.modifiers():
            self.engine.rewind(True)
        elif ev.key() == Qt.Key_Right and not ev.modifiers():
            self.engine.ffwd(True)
        else:
            super().keyPressEvent(ev)

    def keyReleaseEvent(self, ev) -> None:
        if ev.isAutoRepeat():
            return
        if ev.key() == Qt.Key_Left and not ev.modifiers():
            self.engine.rewind(False)
        elif ev.key() == Qt.Key_Right and not ev.modifiers():
            self.engine.ffwd(False)
        else:
            super().keyReleaseEvent(ev)

    # ================================================================ events
    def closeEvent(self, ev) -> None:
        self._save_geometry()
        if not self.exiting and self.settings.get("general.close_to_tray") and QSystemTrayIcon.isSystemTrayAvailable():
            ev.ignore()
            self.hide_to_tray()
            return
        if not self.exiting and not self._confirm_exit():
            ev.ignore()
            return
        self.exiting = True
        ev.accept()
        self.ctl.quit()

    def _save_geometry(self) -> None:
        g = self.geometry()
        self.settings.set("general.window_geometry", f"{g.x()},{g.y()},{g.width()}")

    def _confirm_exit(self) -> bool:
        if self.engine.capturing:
            r = QMessageBox.question(self, "Exit", "A recording is in progress. Stop it, save the file and exit?")
            return r == QMessageBox.Yes
        return True

    def exit_app(self) -> None:
        if not self._confirm_exit():
            return
        self._save_geometry()
        self.exiting = True
        self.ctl.quit()

    # ================================================================ messages
    def show_message(self, level: str, title: str, text: str) -> None:
        if not self.isVisible() and QSystemTrayIcon.isSystemTrayAvailable():
            icon = {"error": QSystemTrayIcon.Critical, "warning": QSystemTrayIcon.Warning}.get(
                level, QSystemTrayIcon.Information)
            self.tray.showMessage(title, text, icon, 8000)
            return
        icon = {"error": QMessageBox.Critical, "warning": QMessageBox.Warning}.get(level, QMessageBox.Information)
        box = QMessageBox(icon, title, text, QMessageBox.Ok, self)
        box.setAttribute(Qt.WA_DeleteOnClose)
        if title == "Cannot open the microphone" and winutil.IS_WINDOWS:
            btn = box.addButton("Open privacy settings", QMessageBox.ActionRole)
            btn.clicked.connect(winutil.open_mic_privacy_settings)
        box.open()

    def _ask_append(self, f) -> str | None:
        box = QMessageBox(QMessageBox.Question, "Record", f"{os.path.basename(f.path)} is open. Add the new "
                          "recording to it as a new segment, or start a new file?", QMessageBox.NoButton, self)
        a = box.addButton("Append", QMessageBox.AcceptRole)
        n = box.addButton("New file", QMessageBox.ActionRole)
        box.addButton(QMessageBox.Cancel)
        box.exec()
        if box.clickedButton() is a:
            return "append"
        if box.clickedButton() is n:
            return "new"
        return None

    # ================================================================ edits
    def _edit_started(self, label: str) -> None:
        self._progress = QProgressDialog(label + "…", "Cancel", 0, 1000, self)
        self._progress.setWindowTitle(label)
        self._progress.setMinimumDuration(1000)        # FR-EDIT-009: only for longer operations
        self._progress.setWindowModality(Qt.WindowModal)
        self._progress.canceled.connect(self.engine.cancel_edit)
        self._progress.setValue(0)
        self.refresh_all()

    def _edit_progress(self, frac: float) -> None:
        if self._progress is not None:
            self._progress.setValue(int(frac * 1000))

    def _edit_finished(self, label: str, status: str, msg: str) -> None:
        if self._progress is not None:
            self._progress.reset()
            self._progress.deleteLater()
            self._progress = None
        if status == "ok":
            self.flash(msg or f"{label}: done")
        elif status == "cancelled":
            self.flash(msg)
        else:
            self.show_message("error", label, msg)
        self.refresh_all()

    # ================================================================ commands
    def _start_dir(self) -> str:
        if self.engine.file:
            return os.path.dirname(self.engine.file.path)
        return self.settings.get("general.last_open_dir") or str(self.settings.recordings_folder())

    def open_dialog(self) -> None:
        p, _ = QFileDialog.getOpenFileName(self, "Open", self._start_dir(),
                                           "Audio files (*.mp3 *.wav *.flac *.ogg *.aiff *.aif);;All files (*)")
        if p:
            self.settings.set("general.last_open_dir", os.path.dirname(p))
            self.engine.open_file(p)

    def _open_rec_folder(self) -> None:
        folder = self.settings.recordings_folder()
        folder.mkdir(parents=True, exist_ok=True)
        winutil.open_folder(folder, self.engine.file.path if self.engine.file else None)

    def save_copy_as(self) -> None:
        f = self.engine.file
        if f is None:
            return
        base = os.path.splitext(f.path)[0] + "_copy"
        p, filt = QFileDialog.getSaveFileName(self, "Save Copy As", base + os.path.splitext(f.path)[1],
                                              "MP3 (*.mp3);;WAV (*.wav);;FLAC (*.flac)",
                                              {"mp3": "MP3 (*.mp3)", "wav": "WAV (*.wav)",
                                               "flac": "FLAC (*.flac)"}.get(f.codec, "MP3 (*.mp3)"))
        if not p:
            return
        codec = os.path.splitext(p)[1].lower().lstrip(".")
        if codec not in ("mp3", "wav", "flac"):
            codec = {"MP3": "mp3", "WAV": "wav", "FLAC": "flac"}[filt.split()[0]]
            p += "." + codec
        spec = format_of(f)
        spec.codec = codec
        spec = spec.normalised()
        d = FormatDialog(spec, "Format of the copy", self)
        if d.exec() != d.Accepted:
            return
        self.engine.save_copy_as(p, d.form.spec())

    def extract_to_file(self) -> None:
        f = self.engine.file
        if f is None or self.engine.selection is None:
            self.flash("Set In and Out marks first ([ and ])")
            return
        base, ext = os.path.splitext(f.path)
        k = 1
        while os.path.exists(f"{base}_x{k}{ext}"):
            k += 1
        p, _ = QFileDialog.getSaveFileName(self, "Extract Selection to File", f"{base}_x{k}{ext}",
                                           f"Same format (*{ext});;MP3 (*.mp3);;WAV (*.wav);;FLAC (*.flac)")
        if not p:
            return
        new_ext = os.path.splitext(p)[1].lower()
        fmt = None
        if new_ext and new_ext != ext.lower():
            spec = format_of(f)
            spec.codec = new_ext.lstrip(".")
            if spec.codec not in ("mp3", "wav", "flac"):
                return
            d = FormatDialog(spec.normalised(), "Format of the extracted file", self)
            if d.exec() != d.Accepted:
                return
            fmt = d.form.spec()
        self.engine.edit_extract(p, fmt)

    def export_segments(self) -> None:
        f = self.engine.file
        if f is None:
            return
        d = QFileDialog.getExistingDirectory(self, "Export segments to folder", os.path.dirname(f.path))
        if d:
            self.engine.edit_export_segments(d)

    def rename(self) -> None:
        f = self.engine.file
        if f is None:
            return
        name, ok = QInputDialog.getText(self, "Rename", "New name:", text=os.path.splitext(os.path.basename(f.path))[0])
        if ok and name.strip():
            self.engine.rename_file(name.strip())

    def properties(self) -> None:
        if self.engine.file:
            PropertiesDialog(self.engine.file, self).exec()

    def delete_file(self) -> None:
        f = self.engine.file
        if f is None:
            return
        if QMessageBox.question(self, "Delete File", f"Move {os.path.basename(f.path)} to the Recycle Bin?") == \
                QMessageBox.Yes:
            self.engine.delete_file()

    def split(self) -> None:
        f = self.engine.file
        if f is None:
            return
        box = QMessageBox(QMessageBox.Question, "Split at Position",
                          f"Split {os.path.basename(f.path)} at {fmt_time_precise(self.engine.position / f.rate)} "
                          "into two new files?", QMessageBox.NoButton, self)
        keep = box.addButton("Split, keep original", QMessageBox.AcceptRole)
        remove = box.addButton("Split, original to Recycle Bin", QMessageBox.DestructiveRole)
        box.addButton(QMessageBox.Cancel)
        box.exec()
        if box.clickedButton() is keep:
            self.engine.edit_split(False)
        elif box.clickedButton() is remove:
            self.engine.edit_split(True)

    def join_files(self) -> None:
        d = JoinDialog(self._start_dir(), self.engine.file.path if self.engine.file else None, self)
        if d.exec() != d.Accepted:
            return
        files = d.files()
        convert = False
        try:
            models = [probe(p) for p in files]
        except UnsupportedFile as exc:
            self.show_message("error", "Join Files", str(exc))
            return
        first = models[0]
        if any((m.codec, m.rate, m.channels) != (first.codec, first.rate, first.channels) or
               (m.codec == "wav" and m.subtype != first.subtype) for m in models):
            if QMessageBox.question(self, "Join Files", "The files have different formats. Convert them all to the "
                                    f"format of {os.path.basename(first.path)}?") != QMessageBox.Yes:
                return
            convert = True
        self.engine.edit_join(files, d.out.text().strip(), convert)

    def timed_recording(self) -> None:
        d = TimedDialog(self)
        if d.exec() == d.Accepted and d.seconds() > 0:
            self.engine.timed_record(d.seconds())

    def schedule(self) -> None:
        try:
            devices = [x.name for x in self.ctl.backend.devices("input", self.settings.get("dev.host_api") or
                                                                self.ctl.backend.default_host_api())]
        except Exception:
            devices = []
        ScheduleDialog(self.settings, devices, self).exec()

    def shortcuts(self) -> None:
        ShortcutsDialog(self.settings, self.ctl.hotkeys.status, self).exec()

    def help_contents(self) -> None:
        for cand in (paths.resource_dir() / "docs" / "HELP.html", paths.app_dir() / "docs" / "HELP.html",
                     paths.resource_dir() / "README.md"):
            if cand.exists():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(cand)))
                return
        self.show_message("info", "Help", "The help file was not found next to the program.")

    def check_updates(self) -> None:
        QMessageBox.information(self, "Check for Updates",
                                f"You are running {APP_NAME} {__version__}. This build has no update server "
                                "configured; new versions are installed by running the new installer.")

    def about(self) -> None:
        libs = []
        for name in ("PySide6", "numpy", "soundfile", "sounddevice", "pyaudiowpatch", "lameenc"):
            try:
                libs.append(f"{name} {__import__(name).__version__}")
            except Exception:
                pass
        dlg = AboutDialog(", ".join(libs), icons.app_icon(), self)
        dlg.setAttribute(Qt.WA_DeleteOnClose)
        self._about = dlg
        dlg.open()


def apply_theme(app: QApplication, theme: str) -> None:
    """Follow Windows, or force light/dark (FR-UI-015)."""
    try:
        hints = app.styleHints()
        scheme = {"light": Qt.ColorScheme.Light, "dark": Qt.ColorScheme.Dark}.get(theme, Qt.ColorScheme.Unknown)
        hints.setColorScheme(scheme)
    except AttributeError:
        pass
    font = app.font()
    if font.pointSize() < 9:
        font.setPointSize(9)
        app.setFont(font)


__all__ = ["MainWindow", "apply_theme", "QFont"]
