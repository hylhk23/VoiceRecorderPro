"""The application engine: one state machine every input talks to (Section 4.3, 19).

Buttons, menus, hot-keys, external devices, the command line and the
scheduler all call Engine.do(action, pressed). The engine owns the recorder
and the player and polls them from a 50 ms timer on the GUI thread.
"""
from __future__ import annotations

import logging
import math
import os
import queue
import shlex
import subprocess
import threading
import time
from datetime import datetime, timedelta
from enum import Enum

from PySide6.QtCore import QObject, QTimer, Signal

from vrp import __version__, winutil
from vrp.core import editor, journal, naming, scheduler
from vrp.core.audio_backend import AudioError, BaseBackend, DeviceInfo
from vrp.core.audiofile import UnsupportedFile, probe
from vrp.core.dsp import dbfs_to_percent
from vrp.core.encoders import can_append
from vrp.core.loopback import LoopbackError
from vrp.core.model import FileModel, FormatSpec, Segment, fmt_duration, fmt_size
from vrp.core.player import Player
from vrp.core.recorder import ARMED as R_ARMED
from vrp.core.recorder import PAUSED as R_PAUSED
from vrp.core.recorder import RECORDING as R_RECORDING
from vrp.core.recorder import RecConfig, Recorder, open_error_message
from vrp.settings import Settings

log = logging.getLogger(__name__)


class State(str, Enum):
    IDLE = "idle"
    STOPPED = "stopped"
    ARMED = "armed"
    RECORDING = "recording"
    REC_PAUSED = "rec_paused"
    PLAYING = "playing"
    PLAY_PAUSED = "play_paused"
    SEEKING = "seeking"


CAPTURE = {State.ARMED, State.RECORDING, State.REC_PAUSED}
PLAYBACK = {State.PLAYING, State.PLAY_PAUSED, State.SEEKING}
STATE_LABEL = {
    State.IDLE: "Idle", State.STOPPED: "Stopped", State.ARMED: "Armed (Vox waiting)",
    State.RECORDING: "Recording", State.REC_PAUSED: "Recording paused", State.PLAYING: "Playing",
    State.PLAY_PAUSED: "Playback paused", State.SEEKING: "Seeking",
}
TAP_HOLD_MS = 300


class Engine(QObject):
    stateChanged = Signal(str)
    fileChanged = Signal()
    selectionChanged = Signal()
    ticked = Signal()
    message = Signal(str, str, str)        # level, title, text
    notify = Signal(str, str)              # title, text (tray balloon)
    uiAction = Signal(str)                 # show_hide, suspend_hotkeys, ...
    editProgress = Signal(float)
    editStarted = Signal(str)
    editFinished = Signal(str, str, str)   # label, status (ok|error|cancelled), message
    _editDone = Signal(object)

    def __init__(self, settings: Settings, backend: BaseBackend, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.backend = backend
        self.events: queue.SimpleQueue = queue.SimpleQueue()
        self.recorder = Recorder(backend, self.events, self._next_path, self._fallback_folder)
        self.player = Player(backend)
        self.state = State.IDLE
        self.file: FileModel | None = None
        self._pos = 0
        self.mark_in: int | None = None
        self.mark_out: int | None = None
        self.loop_selection = False
        self.monitoring = False
        self.window_visible = True
        self.busy = False
        self.ask_append = None          # callable(FileModel) -> 'append' | 'new' | None, set by the UI
        self._cancel_edit = threading.Event()
        self._editDone.connect(self._on_edit_done)
        # recording session state
        self._overrides: dict = {}
        self._active_schedule: scheduler.Due | None = None
        self._sched_done: set[str] = set()
        self._pending_schedule: scheduler.Due | None = None
        self._timed_end: float | None = None
        self._clock_stop: datetime | None = None
        self._rec_started = 0.0
        self._overflow_base = 0
        self._last_signal = 0.0
        self._last_loud = 0.0
        self._clip_since: float | None = None
        self._ptt_active = False
        self._stopping = False
        # devices
        self._device_lost_at: float | None = None
        self._last_retry = 0.0
        # transport hold
        self._hold_dir = 0
        self._hold_timer = QTimer(self)
        self._hold_timer.setSingleShot(True)
        self._hold_timer.timeout.connect(self._begin_scan)
        self._scan_dir = 0
        self._scan_started = 0.0
        self._scan_prev_state = State.STOPPED
        # disk
        self.disk_free: int | None = None
        self.disk_status = "ok"
        self.disk_label = ""
        self.disk_seconds_left: float | None = None
        self._disk_blocked = False
        # notifications throttle (FR-ERR-023)
        self._notified: dict[str, float] = {}
        # timers
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._tick)
        self._last_1s = 0.0
        self._last_5s = 0.0
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(1000)
        self._save_timer.timeout.connect(self.settings.save)
        settings.add_listener(self._on_settings)
        self._apply_player_settings()

    # ================================================================ lifecycle
    def start(self) -> None:
        self._timer.start()
        self._check_disk()
        try:
            editor.cleanup_undo(str(self.settings.recordings_folder()))
        except Exception:
            pass

    def shutdown(self, timeout: float = 5.0) -> None:
        """Stop everything and finalise any open file (exit, log-off, shutdown)."""
        self._timer.stop()
        if self.recorder.thread is not None:
            self.recorder.stop("exit")
            t0 = time.monotonic()
            while time.monotonic() - t0 < timeout and self.recorder.status.get("path"):
                time.sleep(0.05)
            self.recorder.close_input()
            self._drain_events()
        self.player.close()
        winutil.keep_awake(False)
        if self._save_timer.isActive():
            self.settings.save()

    # ================================================================== helpers
    @property
    def capturing(self) -> bool:
        return self.state in CAPTURE

    @property
    def position(self) -> int:
        return int(self._pos)

    @property
    def selection(self) -> tuple[int, int] | None:
        if self.mark_in is None or self.mark_out is None:
            return None
        a, b = sorted((self.mark_in, self.mark_out))
        return (a, b) if b > a else None

    def _set_state(self, st: State) -> None:
        if st != self.state:
            log.info("state %s -> %s", self.state.value, st.value)
            self.state = st
            winutil.keep_awake(st in CAPTURE)
            self.stateChanged.emit(st.value)

    def _say(self, level: str, title: str, text: str) -> None:
        log.log(logging.ERROR if level == "error" else logging.WARNING if level == "warning" else logging.INFO,
                "%s: %s", title, text)
        self.message.emit(level, title, text)

    def _tell(self, key: str, title: str, text: str, every_s: float = 600) -> None:
        """Tray notification, at most once every 10 minutes per kind (always logged)."""
        log.warning("%s: %s", title, text)
        now = time.monotonic()
        if now - self._notified.get(key, -1e9) < every_s:
            return
        self._notified[key] = now
        if self.settings.get("general.notifications"):
            self.notify.emit(title, text)

    def fmt(self) -> FormatSpec:
        f = FormatSpec.from_settings(self.settings)
        o = self._overrides
        if o.get("codec") or o.get("rate") or o.get("channels"):
            f.codec = o.get("codec") or f.codec
            f.rate = int(o.get("rate") or f.rate)
            f.channels = int(o.get("channels") or f.channels)
            f = f.normalised()
        return f

    def vox_enabled(self) -> bool:
        return bool(self.settings.get("vox.enabled")) and not self.settings.get("vox.disabled")

    # ------------------------------------------------------------ sources
    def sources(self) -> tuple[bool, bool]:
        """(microphone, computer audio) as ticked."""
        return bool(self.settings.get("rec.src_mic")), bool(self.settings.get("rec.src_system"))

    def toggle_source(self, kind: str) -> None:
        key = "rec.src_mic" if kind == "mic" else "rec.src_system"
        self.settings.set(key, not self.settings.get(key))   # applied by _on_settings, also mid-recording

    def loopback_support(self) -> tuple[bool, str]:
        try:
            return self.backend.loopback_support()
        except Exception as exc:
            return False, str(exc)

    def _untick_later(self, key: str) -> None:
        """Untick a source that could not be used (outside the settings listener that asked for it)."""
        QTimer.singleShot(0, lambda: self.settings.set(key, False))

    def _loopback_info(self, text: str) -> None:
        self.events.put(("loopback_info", text))     # called from the capture thread

    def _source_label(self, dev: DeviceInfo | None, sys_on: bool) -> str:
        parts = ([dev.name] if dev is not None else []) + (["Computer audio"] if sys_on else [])
        return " + ".join(parts)

    def _open_system(self, required: bool) -> bool:
        """Start computer audio for the open session. False (and a message) when it cannot."""
        try:
            self.recorder.set_system(True, self._loopback_info)
            log.info("computer audio from %s", self.recorder.system_name or "the default output")
            return True
        except LoopbackError as exc:
            if required:
                self._say("error", "Cannot record computer audio", str(exc))
            else:
                self._say("warning", "Computer audio not available",
                          f"{exc}\n\nThe microphone is still being recorded.")
            self._untick_later("rec.src_system")
            return False

    def _apply_sources(self) -> None:
        """Make the open session follow the ticked sources (any time, also while recording)."""
        rec = self.recorder
        if not rec.input_open:
            return
        mic_on, sys_on = self.sources()
        if mic_on and not rec.mic_open and self._device_lost_at is None:
            dev = self.input_device()
            if dev is None:
                self._say("warning", "No input device", "The microphone was not found. Check Options > "
                                                        "Sound Devices.")
                self._untick_later("rec.src_mic")
            else:
                try:
                    rec.set_mic(dev)
                    log.info("microphone on: %s", dev.name)
                except AudioError as exc:
                    self._say("error", "Cannot open the microphone", open_error_message(exc))
                    self._untick_later("rec.src_mic")
        elif not mic_on:
            self._device_lost_at = None
            if rec.mic_open:
                rec.set_mic(None)
                log.info("microphone off")
        if sys_on and not rec.system_open:
            self._open_system(required=False)
        elif not sys_on and rec.system_open:
            rec.set_system(False)
            log.info("computer audio off")

    def input_device(self) -> DeviceInfo | None:
        name = self._overrides.get("device") or self.settings.get("dev.input")
        try:
            return self.backend.find("input", self.settings.get("dev.host_api"), name)
        except Exception:
            log.exception("device lookup failed")
            return None

    def output_device(self) -> DeviceInfo | None:
        try:
            return self.backend.find("output", self.settings.get("dev.host_api"), self.settings.get("dev.output"))
        except Exception:
            log.exception("device lookup failed")
            return None

    def _next_path(self, folder: str | None) -> str:
        out = self._overrides.pop("out", None)
        if out and not folder:
            out = os.path.abspath(out)
            ext = self.recorder.cfg.fmt.ext
            if not out.lower().endswith(ext):
                out = os.path.splitext(out)[0] + ext
            os.makedirs(os.path.dirname(out), exist_ok=True)
            return editor.unique_path(out)
        folder = folder or str(self.settings.recordings_folder())
        os.makedirs(folder, exist_ok=True)
        pattern = self._overrides.get("pattern") or self.settings.get("rec.pattern")
        if naming.validate_pattern(pattern):
            pattern = "{Mon}{dd}_{n}"
        rec = self.recorder
        dev = rec.device.name if (rec.mic_open and rec.device) else ("Computer audio" if rec.system_open else "")
        p = naming.next_path(folder, pattern, self.recorder.cfg.fmt.ext, host=winutil.computer_name(),
                             device=dev, tag=self.settings.get("rec.tag"),
                             name=self._overrides.get("name", "Schedule"))
        os.makedirs(p.parent, exist_ok=True)
        return str(p)

    def _fallback_folder(self) -> str:
        return str(self.settings.fallback_folder())

    # ================================================================ dispatcher
    def do(self, action: str, pressed: bool = True) -> None:
        """Entry point for hot-keys, external devices, the tray and the CLI."""
        hold = {"ptt": self.ptt, "rewind": self.rewind, "ffwd": self.ffwd,
                "play_held": self.play_held, "record_active": self.ptt}
        if action in hold:
            hold[action](pressed)
            return
        if not pressed:
            return
        simple = {
            "record": self.record, "stop": self.stop, "play": self.play_pause,
            "skip_back": lambda: self.skip(-self.settings.get("play.skip_s")),
            "skip_fwd": lambda: self.skip(self.settings.get("play.skip_s")),
            "prev_segment": self.prev_segment, "next_segment": self.next_segment,
            "go_start": self.go_start, "go_end": self.go_end, "vox_toggle": self.toggle_vox,
            "source_mic": lambda: self.toggle_source("mic"),
            "source_system": lambda: self.toggle_source("system"),
            "mark_in": self.set_mark_in, "mark_out": self.set_mark_out, "extract": lambda: self.edit_extract(),
            "insert_boundary": self.insert_boundary, "new_recording": self.new_recording,
            "speed_down": lambda: self.change_speed(-5), "speed_up": lambda: self.change_speed(5),
            "volume_down": lambda: self.change_volume(-10), "volume_up": lambda: self.change_volume(10),
        }
        if action in simple:
            simple[action]()
        else:
            self.uiAction.emit(action)

    # ================================================================== recording
    def _compatible(self, f: FileModel, fmt: FormatSpec) -> bool:
        if not can_append(f.codec) or f.codec != fmt.codec or f.rate != fmt.rate or f.channels != fmt.channels:
            return False
        if f.codec == "wav" and f.subtype != fmt.wav_subtype:
            return False
        return not f.readonly

    def record(self) -> None:
        st = self.state
        if st in (State.ARMED, State.RECORDING):
            self.recorder.pause()
            self._set_state(State.REC_PAUSED)
            self._tone(False)
            return
        if st == State.REC_PAUSED:
            self.recorder.resume()
            self._set_state(State.ARMED if self.recorder.cfg.vox_enabled else State.RECORDING)
            self._tone(True)
            return
        if self.busy:
            return
        if st in PLAYBACK:
            self._stop_playback()
        append = None
        if self.file is not None and not self.file.recording:
            action = self.settings.get("rec.open_file_action")
            if action == "ask" and self.ask_append is not None:
                action = self.ask_append(self.file)
                if action is None:
                    return
            if action == "append" and self._compatible(self.file, self.fmt()):
                append = self.file.path
        self._start_capture(append)

    def _start_capture(self, append: str | None = None, vox: bool | None = None) -> bool:
        if self._disk_blocked:
            self._check_disk()
            if self._disk_blocked:
                self._say("warning", "Low disk space",
                          f"Only {fmt_size(self.disk_free or 0)} free on {self.disk_label}. "
                          "Recording will resume when free space is above the warning level.")
                return False
        folder = str(self.settings.recordings_folder())
        if winutil.is_network_path(folder) and not self.settings.get("rec.network_warned"):
            self.settings.set("rec.network_warned", True)
            self._say("info", "Network folder",
                      "You are recording to a network folder. Dropouts are more likely; if the share "
                      "disappears, recording continues in the fallback folder.")
        mic_on, sys_on = self.sources()
        if not mic_on and not sys_on:
            self._say("warning", "Nothing to record",
                      "Tick Microphone or Computer audio (or both) in the main window, then press Record.")
            return False
        if sys_on:
            ok, why = self.loopback_support()
            if not ok:
                if not mic_on:
                    self._say("error", "Cannot record computer audio", why)
                    return False
                self._say("warning", "Computer audio not available", f"{why}\n\nRecording the microphone only.")
                self._untick_later("rec.src_system")
                sys_on = False
        dev = None
        if mic_on:
            dev = self.input_device()
            if dev is None:
                self._say("error", "No input device",
                          "The selected recording device was not found. Check Options > Sound Devices, or "
                          "untick Microphone to record computer audio only.")
                return False
        self.player.close()          # release the file before the recorder writes to it
        cfg = RecConfig.from_settings(self.settings, self._source_label(dev, sys_on))
        cfg.fmt = self.fmt()
        cfg.tags["format"] = cfg.fmt.describe()
        if vox is not None:
            cfg.vox_enabled = vox
        elif self._overrides.get("vox") in ("on", "off"):
            cfg.vox_enabled = self._overrides["vox"] == "on" and not self.settings.get("vox.disabled")
        rec = self.recorder
        if rec.input_open:
            same = rec.cfg.fmt == cfg.fmt and list(rec.cfg.channel_map) == list(cfg.channel_map)
            if not same:
                rec.close_input()
        try:
            if not rec.input_open:
                rec.open_input(dev, cfg, self.settings.get("adv.blocksize"), self.settings.get("adv.latency"),
                               self.settings.get("dev.exclusive"))
            else:
                rec.update_config(cfg)
                rec.set_vox_enabled(cfg.vox_enabled)
                if dev is not None and not (rec.mic_open and rec.device and rec.device.name == dev.name):
                    rec.set_mic(dev)
                elif dev is None and rec.mic_open:
                    rec.set_mic(None)
        except AudioError as exc:
            if rec.input_open and not self.monitoring:
                rec.close_input()
            self._say("error", "Cannot open the microphone", open_error_message(exc))
            if exc.privacy:
                self.uiAction.emit("mic_privacy")
            return False
        if sys_on and not rec.system_open:
            if not self._open_system(required=not mic_on):
                if not mic_on:
                    if not self.monitoring:
                        rec.close_input()
                    return False
        elif not sys_on and rec.system_open:
            rec.set_system(False)
        rec.cfg.vox_enabled = cfg.vox_enabled
        rec.start(append)
        self._rec_started = time.monotonic()
        self._overflow_base = rec.overflows
        self._last_signal = self._last_loud = time.monotonic()
        self._clip_since = None
        self._stopping = False
        self._clock_stop = self._clock_target()
        self._set_state(State.ARMED if cfg.vox_enabled else State.RECORDING)
        self._tone(True)
        self._check_disk()
        return True

    def _clock_target(self) -> datetime | None:
        text = (self.settings.get("rec.autostop.clock") or "").strip()
        if not text:
            return None
        try:
            hh, mm = (int(x) for x in text.split(":")[:2])
        except ValueError:
            return None
        now = datetime.now()
        t = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        return t if t > now else t + timedelta(days=1)

    def stop(self) -> None:
        st = self.state
        if st in CAPTURE:
            if not self._stopping:
                self._stopping = True
                self.recorder.stop("user")
                self._tone(False)
            return
        if st in PLAYBACK:
            self._end_scan_silently()
            self._stop_playback()
            return
        if st == State.STOPPED:
            self.seek(0)         # a second Stop returns to the start

    def record_with(self, overrides: dict) -> bool:
        """Start a new recording with one-off overrides (command line)."""
        if self.capturing:
            return False
        if self.state in PLAYBACK:
            self._stop_playback()
        o = {k: v for k, v in overrides.items() if v not in (None, "")}
        self._overrides = {"vox": o.get("vox", "current"), "device": o.get("device", ""),
                           "codec": o.get("format", ""), "rate": o.get("rate"), "channels": o.get("channels"),
                           "pattern": o.get("pattern", ""), "out": o.get("out")}
        self.close_file()
        if not self._start_capture(None):
            self._overrides = {}
            return False
        if o.get("duration"):
            self._timed_end = time.monotonic() + float(o["duration"])
        return True

    def timed_record(self, seconds: float) -> None:
        if self.capturing:
            self._timed_end = time.monotonic() + seconds
            return
        self.record()
        if self.capturing:
            self._timed_end = time.monotonic() + seconds

    @property
    def timed_remaining(self) -> float | None:
        if self._timed_end is None or not self.capturing:
            return None
        return max(0.0, self._timed_end - time.monotonic())

    def ptt(self, pressed: bool) -> None:
        """Push-to-talk / record while an external input is active."""
        if pressed:
            self._ptt_active = True
            if self.state == State.REC_PAUSED:
                self.record()
            elif not self.capturing:
                if self.state in PLAYBACK:
                    self._stop_playback()
                append = self.file.path if (self.file and not self.file.recording and
                                            self.settings.get("rec.open_file_action") != "new" and
                                            self._compatible(self.file, self.fmt())) else None
                self._start_capture(append, vox=False)
        else:
            self._ptt_active = False
            if self.state in (State.ARMED, State.RECORDING):
                self.record()        # pause

    def toggle_vox(self) -> None:
        if self.settings.get("vox.disabled"):
            return
        on = not self.settings.get("vox.enabled")
        self.settings.set("vox.enabled", on)
        if self.capturing:
            self.recorder.set_vox_enabled(on)
            if self.state != State.REC_PAUSED:
                self._set_state(State.ARMED if on else State.RECORDING)

    def set_threshold(self, value: int) -> None:
        self.settings.set("vox.threshold", int(value), save=False)
        self._save_timer.start()
        self.recorder.set_threshold(float(value))

    def monitor(self, on: bool) -> None:
        self.monitoring = on
        if on and not self.recorder.input_open:
            mic_on, sys_on = self.sources()
            sys_on = sys_on and self.loopback_support()[0]
            dev = self.input_device() if mic_on else None
            if mic_on and dev is None:
                self._say("error", "No input device", "The selected recording device was not found.")
                self.monitoring = False
                return
            cfg = RecConfig.from_settings(self.settings, self._source_label(dev, sys_on))
            try:
                self.recorder.open_input(dev, cfg, self.settings.get("adv.blocksize"),
                                         self.settings.get("adv.latency"), self.settings.get("dev.exclusive"))
            except AudioError as exc:
                self.recorder.close_input()
                self.monitoring = False
                self._say("error", "Cannot open the microphone", open_error_message(exc))
                return
            if sys_on:
                self._open_system(required=False)
        elif not on and not self.capturing and self.recorder.input_open:
            self.recorder.close_input()

    def insert_boundary(self) -> None:
        if self.state in (State.ARMED, State.RECORDING):
            self.recorder.insert_boundary()
        elif self.file is not None and not self.capturing:
            pos = self.position
            self._run_edit("Insert segment boundary", lambda progress, cancel: editor.insert_boundary(self.file, pos),
                           self._after_inplace_edit, needs_close=True)

    # ================================================================== playback
    def _ensure_player(self) -> bool:
        if self.file is None:
            return False
        if self.player.path != self.file.path:
            try:
                self.player.open(self.file.path)
            except Exception as exc:
                self._say("error", "Cannot play the file", str(exc))
                return False
            self.player.set_gap(self.settings.get("vox.gap_ms"), self.file.segment_starts())
        self.player.set_device(self.output_device())
        self._apply_player_settings()
        return True

    def _apply_player_settings(self) -> None:
        self.player.set_speed(self.settings.get("play.speed"))
        self.player.set_volume(self.settings.get("play.volume"))
        self.player.set_selection(self.selection, self.loop_selection)

    def play_pause(self) -> None:
        st = self.state
        if st in CAPTURE or self.busy:
            return
        if st == State.PLAYING:
            self.player.pause()
            self._pos = self.player.position
            self._set_state(State.PLAY_PAUSED)
            return
        if not self._ensure_player():
            return
        sel = self.selection
        pos = self.position
        if st == State.PLAY_PAUSED:
            back = int(self.settings.get("play.backspace_s") * self.file.rate)   # FR-PLAY-004
            pos = max(sel[0] if sel else 0, pos - back)
        elif sel and not (sel[0] <= pos < sel[1]):
            pos = sel[0]
        elif pos >= self.file.frames - 1:
            pos = 0
        self.player.seek(pos)
        try:
            self.player.play()
        except AudioError as exc:
            self._say("error", "Cannot open the speakers", str(exc))
            return
        self._pos = pos
        self._set_state(State.PLAYING)

    def play_held(self, pressed: bool) -> None:
        if pressed and self.state != State.PLAYING:
            self.play_pause()
        elif not pressed and self.state == State.PLAYING:
            self.play_pause()

    def _stop_playback(self) -> None:
        self._pos = self.player.position if self.player.is_open else self._pos
        self.player.stop()
        self._set_state(State.STOPPED if self.file else State.IDLE)

    def seek(self, frame: float) -> None:
        if self.file is None or self.capturing:
            return
        frame = int(max(0, min(self.file.frames, frame)))
        self._pos = frame
        if self.player.is_open:
            self.player.seek(frame)
        self.ticked.emit()

    def skip(self, seconds: float) -> None:
        if self.file is None or self.capturing:
            return
        self.seek(self.position + seconds * self.file.rate)

    def go_start(self) -> None:
        self.seek(0)

    def go_end(self) -> None:
        if self.file:
            self.seek(self.file.frames)

    def prev_segment(self) -> None:
        if self.file is None or self.capturing:
            return
        starts = self.file.segment_starts()
        i = self.file.segment_index_at(self.position)
        if self.position - starts[i] > self.file.rate and i < len(starts):
            self.seek(starts[i])
        else:
            self.seek(starts[max(0, i - 1)])

    def next_segment(self) -> None:
        if self.file is None or self.capturing:
            return
        starts = self.file.segment_starts()
        i = self.file.segment_index_at(self.position)
        if i + 1 < len(starts):
            self.seek(starts[i + 1])

    def rewind(self, pressed: bool) -> None:
        self._transport_hold(-1, pressed)

    def ffwd(self, pressed: bool) -> None:
        self._transport_hold(1, pressed)

    def _transport_hold(self, direction: int, pressed: bool) -> None:
        if self.file is None or self.capturing or self.busy:
            return
        if pressed:
            self._hold_dir = direction
            self._hold_timer.start(TAP_HOLD_MS)
        elif self._hold_timer.isActive():          # released quickly: a click jumps
            self._hold_timer.stop()
            self.skip(direction * self.settings.get("play.skip_s"))
        elif self._scan_dir:
            self._end_scan()

    def _begin_scan(self) -> None:
        if self.file is None or self.capturing:
            return
        self._scan_dir = self._hold_dir
        self._scan_started = time.monotonic()
        self._scan_prev_state = self.state
        if self.settings.get("play.audible_scan") and self._ensure_player():
            try:
                self.player.seek(self.position)
                self.player.scan(self._scan_dir, 4.0)
            except AudioError:
                pass
        else:
            if self.player.is_open:
                self.player.pause()
        self._set_state(State.SEEKING)

    def _end_scan(self) -> None:
        d, self._scan_dir = self._scan_dir, 0
        if not d:
            return
        prev = self._scan_prev_state
        if self.player.is_open:
            self._pos = self.player.position if self.player.scan_dir else self._pos
            self.player.end_scan()
            self.player.seek(self._pos)
        if prev == State.PLAYING and self.player.is_open:
            self.player.play()
            self._set_state(State.PLAYING)
        else:
            if self.player.is_open:
                self.player.pause()
            self._set_state(State.PLAY_PAUSED if prev == State.PLAY_PAUSED else State.STOPPED)

    def _end_scan_silently(self) -> None:
        self._hold_timer.stop()
        self._scan_dir = 0
        if self.player.is_open:
            self.player.end_scan()

    def set_speed(self, pct: int) -> None:
        self.settings.set("play.speed", int(max(50, min(200, pct))))

    def change_speed(self, delta: int) -> None:
        self.set_speed(self.settings.get("play.speed") + delta)

    def set_volume(self, pct: int) -> None:
        self.settings.set("play.volume", int(max(0, min(100, pct))))

    def change_volume(self, delta: int) -> None:
        self.set_volume(self.settings.get("play.volume") + delta)

    def set_loop(self, on: bool) -> None:
        self.loop_selection = on
        self.player.set_selection(self.selection, on)

    # ================================================================ selection
    def _snap(self, frame: int) -> int:
        """MP3 marks snap to frame boundaries (FR-EDIT-003)."""
        if self.file is None:
            return frame
        frame = int(max(0, min(self.file.frames, frame)))
        if self.file.codec == "mp3":
            spf = 1152 if self.file.rate >= 32000 else 576
            k = round((frame + 529) / spf)
            frame = int(max(0, min(self.file.frames, k * spf - 529)))
        return frame

    def set_mark_in(self, frame: int | None = None) -> None:
        if self.file is None or self.capturing:
            return
        self.mark_in = self._snap(self.position if frame is None else frame)
        self._selection_changed()

    def set_mark_out(self, frame: int | None = None) -> None:
        if self.file is None or self.capturing:
            return
        self.mark_out = self._snap(self.position if frame is None else frame)
        self._selection_changed()

    def set_selection(self, a: int, b: int) -> None:
        if self.file is None or self.capturing:
            return
        self.mark_in, self.mark_out = self._snap(a), self._snap(b)
        self._selection_changed()

    def clear_selection(self) -> None:
        self.mark_in = self.mark_out = None
        self._selection_changed()

    def select_all(self) -> None:
        if self.file:
            self.set_selection(0, self.file.frames)

    def select_segment(self) -> None:
        if self.file is None:
            return
        starts = self.file.segment_starts() + [self.file.frames]
        i = self.file.segment_index_at(self.position)
        self.set_selection(starts[i], starts[i + 1])

    def _selection_changed(self) -> None:
        self.player.set_selection(self.selection, self.loop_selection)
        self.selectionChanged.emit()

    # ===================================================================== files
    def open_file(self, path: str) -> bool:
        if self.capturing:
            self._say("warning", "Recording in progress", "Stop the recording before opening another file.")
            return False
        if self.state in PLAYBACK:
            self._stop_playback()
        try:
            model = probe(path)
        except UnsupportedFile as exc:
            self._say("error", "Cannot open file", str(exc))
            return False
        self.player.close()
        self.file = model
        self._pos = 0
        self.mark_in = self.mark_out = None
        self.settings.add_recent(path)
        self._set_state(State.STOPPED)
        self.fileChanged.emit()
        self._selection_changed()
        if model.readonly:
            self._tell("readonly", "Read-only", f"{os.path.basename(path)} is in use by another program; "
                                                "editing is disabled.")
        return True

    def close_file(self) -> None:
        if self.capturing:
            return
        if self.state in PLAYBACK:
            self._stop_playback()
        self.player.close()
        old = self.file
        self.file = None
        self._pos = 0
        self.mark_in = self.mark_out = None
        self._set_state(State.IDLE)
        self.fileChanged.emit()
        self._selection_changed()
        if old:
            try:
                editor.cleanup_undo(os.path.dirname(old.path))
            except Exception:
                pass

    def new_recording(self) -> None:
        if not self.capturing:
            self.close_file()

    def reload_file(self) -> None:
        if self.file is None:
            return
        path = self.file.path
        try:
            self.file = probe(path)
        except UnsupportedFile as exc:
            self._say("error", "Cannot open file", str(exc))
            self.close_file()
            return
        self._pos = min(self._pos, self.file.frames)
        if self.mark_in is not None:
            self.mark_in = min(self.mark_in, self.file.frames)
        if self.mark_out is not None:
            self.mark_out = min(self.mark_out, self.file.frames)
        self.fileChanged.emit()
        self._selection_changed()

    def rename_file(self, new_name: str) -> bool:
        if self.file is None or self.capturing:
            return False
        old = self.file.path
        ext = os.path.splitext(old)[1]
        name = naming.safe_component(new_name)
        if not name:
            return False
        if not name.lower().endswith(ext.lower()):
            name += ext
        new = os.path.join(os.path.dirname(old), name)
        if os.path.exists(new):
            self._say("warning", "Rename", f"{name} already exists.")
            return False
        if self.state in PLAYBACK:
            self._stop_playback()
        self.player.close()
        try:
            os.rename(old, new)
        except OSError as exc:
            self._say("error", "Rename failed", str(exc))
            return False
        self.file.path = new
        recent = [new if p == old else p for p in self.settings.get("general.recent_files")]
        self.settings.set("general.recent_files", recent)
        self.reload_file()
        return True

    def delete_file(self) -> bool:
        if self.file is None or self.capturing:
            return False
        path = self.file.path
        self.close_file()
        ok = winutil.send_to_recycle_bin(path)
        if not ok:
            self._say("error", "Delete failed", f"{os.path.basename(path)} could not be moved to the Recycle Bin.")
        return ok

    # ===================================================================== edits
    def _run_edit(self, label: str, fn, on_done, needs_close: bool = False) -> None:
        if self.busy or self.capturing:
            return
        if self.file is not None and self.file.readonly and needs_close:
            self._say("warning", label, "The file is read-only or in use by another program.")
            return
        if self.state in PLAYBACK:
            self._end_scan_silently()
            self._stop_playback()
        if needs_close:
            self.player.close()
        self.busy = True
        self._cancel_edit.clear()
        self.editStarted.emit(label)

        def work():
            try:
                res = fn(progress=lambda f: self.editProgress.emit(float(f)), cancel=self._cancel_edit.is_set)
                out = ("ok", res)
            except InterruptedError:
                out = ("cancelled", None)
            except Exception as exc:
                log.exception("%s failed", label)
                out = ("error", str(exc))
            self._editDone.emit((label, out, on_done))

        threading.Thread(target=work, name="vrp-edit", daemon=True).start()

    def cancel_edit(self) -> None:
        self._cancel_edit.set()

    def _on_edit_done(self, payload) -> None:
        label, (status, res), on_done = payload
        self.busy = False
        msg = ""
        if status == "ok":
            try:
                msg = on_done(res) or ""
            except Exception as exc:
                status, msg = "error", str(exc)
        elif status == "cancelled":
            msg = "Cancelled; the original file is unchanged."
            if self.file:
                self.reload_file()
        else:
            msg = f"{res} The original file is unchanged."
            if self.file:
                self.reload_file()
        self.editFinished.emit(label, status, msg)

    def _after_inplace_edit(self, model: FileModel) -> str:
        self.file = model
        self._pos = min(self._pos, model.frames)
        self.mark_in = self.mark_out = None
        self.fileChanged.emit()
        self._selection_changed()
        return ""

    def edit_extract(self, dst: str | None = None, fmt: FormatSpec | None = None, open_after: bool = False) -> None:
        sel = self.selection
        if self.file is None or sel is None:
            self._say("info", "Extract", "Set In and Out marks first ([ and ]).")
            return
        model = self.file

        def done(new: FileModel) -> str:
            if open_after:
                self.open_file(new.path)
            return f"Saved {os.path.basename(new.path)} ({fmt_duration(new.duration)})."
        self._run_edit("Extract selection", lambda progress, cancel: editor.extract(
            model, sel[0], sel[1], dst, fmt, progress, cancel), done)

    def edit_delete(self) -> None:
        sel = self.selection
        if self.file is None or sel is None:
            return
        model = self.file
        self._run_edit("Delete selection", lambda progress, cancel: editor.delete_range(
            model, sel[0], sel[1], progress, cancel), self._after_inplace_edit, needs_close=True)

    def edit_crop(self) -> None:
        sel = self.selection
        if self.file is None or sel is None:
            return
        model = self.file
        self._run_edit("Crop to selection", lambda progress, cancel: editor.crop(
            model, sel[0], sel[1], progress, cancel), self._after_inplace_edit, needs_close=True)

    def edit_split(self, remove_original: bool = False) -> None:
        if self.file is None:
            return
        model, at = self.file, self.position

        def done(res) -> str:
            p1, p2 = res
            if remove_original:
                self.file = None
                self.open_file(p1)
            return f"Split into {os.path.basename(p1)} and {os.path.basename(p2)}."
        self._run_edit("Split at position", lambda progress, cancel: editor.split(
            model, at, remove_original, progress, cancel), done, needs_close=remove_original)

    def edit_join(self, files: list[str], dst: str, convert: bool = False) -> None:
        def done(new: FileModel) -> str:
            return f"Joined {len(files)} files into {os.path.basename(new.path)}."
        self._run_edit("Join files", lambda progress, cancel: editor.join(files, dst, convert, progress, cancel), done)

    def edit_export_segments(self, folder: str | None = None) -> None:
        if self.file is None:
            return
        model = self.file
        self._run_edit("Export segments", lambda progress, cancel: editor.export_segments(
            model, folder, progress, cancel), lambda res: f"Wrote {len(res)} files.")

    def save_copy_as(self, dst: str, fmt: FormatSpec | None) -> None:
        if self.file is None:
            return
        model = self.file
        self._run_edit("Save copy", lambda progress, cancel: editor.write_ranges(
            model, [(0, model.frames)], dst, fmt, progress, cancel),
            lambda new: f"Saved {os.path.basename(new.path)}.")

    def edit_undo(self) -> None:
        if self.file is None or not editor.can_undo(self.file.path):
            return
        path = self.file.path

        def work(progress, cancel):
            editor.undo(path)
            return probe(path)
        self._run_edit("Undo last edit", work, self._after_inplace_edit, needs_close=True)

    def can_undo(self) -> bool:
        return bool(self.file) and editor.can_undo(self.file.path)

    # ============================================================ recovery
    def pending_recoveries(self) -> list[journal.Pending]:
        return journal.find_pending([str(self.settings.recordings_folder()), self._fallback_folder()])

    # ================================================================ tick
    def _tick(self) -> None:
        self._drain_events()
        now = time.monotonic()
        rec = self.recorder
        if self.state in CAPTURE and self.file is not None and self.file.recording:
            st = rec.status
            if st.get("path") == self.file.path:
                self.file.frames = st.get("frames", self.file.frames)
                self.file.size = st.get("size", self.file.size)
                self._pos = self.file.frames
        if self.state in PLAYBACK:
            if self._scan_dir and not self.player.scan_dir:
                # silent scan: move the position ourselves
                rate = 4.0 if now - self._scan_started < 3 else 10.0
                self.seek(self._pos + self._scan_dir * rate * 0.05 * (self.file.rate if self.file else 0))
                if self.file and (self._pos <= 0 or self._pos >= self.file.frames):
                    pass
            elif self.player.is_open:
                if self._scan_dir:
                    self.player.scan_rate = 4.0 if now - self._scan_started < 3 else 10.0
                self._pos = self.player.position
                if self.player.finished and self.state == State.PLAYING:
                    self._on_play_end()
        if now - self._last_1s >= 1.0:
            self._last_1s = now
            self._every_second()
        if now - self._last_5s >= 5.0:
            self._last_5s = now
            self._check_disk()
        self.ticked.emit()

    def _on_play_end(self) -> None:
        self.player.pause()
        if self.settings.get("play.at_end") == "start":
            self.seek(self.selection[0] if self.selection else 0)
        else:
            self._pos = self.selection[1] if self.selection else (self.file.frames if self.file else 0)
        self._set_state(State.STOPPED)

    def _drain_events(self) -> None:
        while True:
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                return
            try:
                self._handle_event(ev)
            except Exception:
                log.exception("event handling failed: %r", ev[:2])

    def _handle_event(self, ev) -> None:
        kind = ev[0]
        if kind == "mode":
            if self.state in CAPTURE and not self._stopping:
                m = ev[1]
                if m == R_ARMED:
                    self._set_state(State.ARMED)
                elif m == R_RECORDING:
                    self._set_state(State.RECORDING)
                elif m == R_PAUSED:
                    self._set_state(State.REC_PAUSED)
        elif kind == "file_opened":
            _, path, fmt, segs, frames, appended = ev
            self.player.close()
            self.file = FileModel(path=path, codec=fmt.codec, rate=fmt.rate, channels=fmt.channels, frames=frames,
                                  size=os.path.getsize(path) if os.path.exists(path) else 0,
                                  subtype=fmt.wav_subtype if fmt.codec == "wav" else
                                  ("PCM_24" if fmt.codec == "flac" and fmt.flac_bits == 24 else
                                   "PCM_16" if fmt.codec == "flac" else ""),
                                  kbps=fmt.mp3_kbps if fmt.codec == "mp3" else None,
                                  segments=list(segs) or [], recording=True)
            self.mark_in = self.mark_out = None
            self.settings.add_recent(path)
            self.fileChanged.emit()
            self._selection_changed()
        elif kind == "segment":
            if self.file is not None and self.file.recording:
                self.file.segments = list(self.recorder.segments) or [Segment(0)]
                self.fileChanged.emit()
        elif kind == "file_closed":
            _, path, frames, segs = ev
            if self.file is not None and self.file.path == path:
                try:
                    self.file = probe(path)
                except UnsupportedFile:
                    self.file.recording = False
                    self.file.segments = list(segs)
                self._pos = self.file.frames
                self.fileChanged.emit()
            self._after_command(path, frames, segs)
        elif kind == "stopped":
            _, reason, path = ev
            self._stopping = False
            self._timed_end = None
            self._active_schedule = None
            self._overrides = {}
            if not self.monitoring:
                self.recorder.close_input()
            self._drain_after_stop()
            if self.file is not None and self.file.recording:
                self.file.recording = False
            self._set_state(State.STOPPED if self.file else State.IDLE)
            if self.file:
                self._pos = self.file.frames
            self.fileChanged.emit()
            if self._pending_schedule is not None:
                d, self._pending_schedule = self._pending_schedule, None
                self._start_schedule(d)
        elif kind == "error":
            self._say("error", "Recording problem", ev[2])
        elif kind == "write_error":
            _, path, err, fb = ev
            self._tell("write_error", "Write failed",
                       f"Could not write {os.path.basename(path)} ({err}). Recording continues in {fb}.", 60)
        elif kind == "device_lost":
            self._on_device_lost(ev[1])
        elif kind == "backlog":
            self._tell("backlog", "Disk too slow", f"The disk is {ev[1]:.0f} s behind; switching to the fallback "
                                                   "folder.")
        elif kind == "autostop":
            self._tell("autostop_" + ev[1], "Recording stopped", f"Recording stopped: {ev[1]} limit reached.", 1)
        elif kind == "rollover":
            log.info("rollover (%s)", ev[1])
        elif kind == "info":
            self._tell("info", "Voice Recorder Pro", ev[1], 1)
        elif kind == "loopback_info":
            log.info("%s", ev[1])
            if self.capturing:
                self._tell("loopback", "Computer audio", ev[1], 20)

    def _drain_after_stop(self) -> None:
        # events emitted while closing (e.g. final file_closed) are handled now
        while True:
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                return
            if ev[0] != "stopped":
                self._handle_event(ev)

    def _after_command(self, path: str, frames: int, segs) -> None:
        """FR-CLI-005: run the user's command for each finalised file."""
        cmd = (self.settings.get("rec.after_command") or "").strip()
        if not cmd:
            return
        rate = self.file.rate if self.file else 1
        start_iso = segs[0].timestamp if segs and segs[0].timestamp else ""
        values = {"{path}": f'"{path}"', "{duration_s}": f"{frames / max(1, rate):.1f}",
                  "{segments}": str(len(segs)), "{start_iso}": start_iso}
        for k, v in values.items():
            cmd = cmd.replace(k, v)

        def run():
            try:
                flags = 0x08000000 if winutil.IS_WINDOWS else 0     # CREATE_NO_WINDOW
                args = cmd if winutil.IS_WINDOWS else shlex.split(cmd)
                r = subprocess.run(args, capture_output=True, text=True, timeout=300, creationflags=flags,
                                   shell=False)
                log.info("after-command exit %s: %s %s", r.returncode, r.stdout[-500:], r.stderr[-500:])
            except Exception as exc:
                log.warning("after-command failed: %s", exc)
        threading.Thread(target=run, name="vrp-after-cmd", daemon=True).start()

    # ----------------------------------------------------------- 1 s checks
    def _every_second(self) -> None:
        now = time.monotonic()
        if self.capturing and not self._stopping:
            if self._timed_end is not None and now >= self._timed_end:
                self._tell("timed", "Timed recording", "The timed recording finished.", 1)
                self.stop()
                return
            if self._clock_stop is not None and datetime.now() >= self._clock_stop:
                self._tell("clock", "Recording stopped", "The stop time was reached.", 1)
                self.stop()
                return
            rec = self.recorder
            levels = rec.mix_levels                 # what is being recorded
            peak = float(levels.max()) if levels.size else 0.0
            mic_live = rec.mic_open
            if not mic_live or (rec.levels.size and float(rec.levels.max()) > 1e-5):
                self._last_signal = now             # "no signal" is about the microphone only
            thr = self.settings.get("vox.threshold") / 100.0
            if peak > thr:
                self._last_loud = now
            vox = self.recorder.cfg.vox_enabled
            silence_min = self.settings.get("rec.autostop.silence_min")
            if (not vox and silence_min and self.state == State.RECORDING
                    and now - self._last_loud > silence_min * 60):
                self._tell("silence", "Recording stopped", f"No sound for {silence_min} minutes.", 1)
                self.stop()
                return
            if not vox and mic_live and self.state == State.RECORDING and now - self._last_signal > 10:
                dev = self.recorder.device.name if self.recorder.device else "the input"
                self._tell("nosignal", "No signal", f"No signal from {dev} — is it muted?")
            if self.recorder.clip_until > now:
                self._clip_since = self._clip_since or now
                if now - self._clip_since >= 5:
                    self._tell("clip", "Input too loud", "The input is clipping. Lower the input gain or the "
                                                         "microphone level.")
            else:
                self._clip_since = None
        if self._device_lost_at is not None:
            self._retry_device()
        self._check_schedules()

    @property
    def overflow_count(self) -> int:
        return max(0, self.recorder.overflows - self._overflow_base)

    # ------------------------------------------------------------- devices
    def _on_device_lost(self, name: str) -> None:
        if not self.capturing and not self.monitoring:
            return
        log.warning("input device lost: %s", name)
        self._device_lost_at = time.monotonic()
        self._last_retry = 0.0
        self.recorder.suspend_capture()
        also = ("Computer audio is still being recorded; trying" if self.recorder.system_open else
                "The segment was closed; trying")
        self._tell("device_lost", "Microphone disconnected",
                   f"{name} stopped sending audio. {also} to reconnect for 60 s.", 30)

    def _retry_device(self) -> None:
        now = time.monotonic()
        if now - self._last_retry < 2.0:
            return
        self._last_retry = now
        if not (self.capturing or self.monitoring):
            self._device_lost_at = None
            return
        try:
            self.backend.rescan()
        except Exception:
            log.debug("rescan failed", exc_info=True)
        dev = self.input_device()
        give_up = now - self._device_lost_at > 60
        if dev is None and give_up and self.settings.get("dev.fallback") == "default":
            dev = self.backend.find("input", self.settings.get("dev.host_api"), "")
        if dev is not None:
            try:
                self.recorder.replace_device(dev)
                self._device_lost_at = None
                self._tell("device_back", "Recording resumed", f"Recording continues on {dev.name}.", 1)
                return
            except AudioError:
                log.info("device not ready yet")
        if give_up:
            self._device_lost_at = None
            if self.recorder.system_open and self.capturing:
                self._say("warning", "Microphone lost", "The microphone did not come back. Recording continues "
                                                        "with computer audio only.")
                self._untick_later("rec.src_mic")
                return
            self._say("error", "Microphone lost", "The recording device did not come back. Recording stopped.")
            self.stop()

    # ------------------------------------------------------------- schedules
    def schedule_entries(self) -> list[scheduler.Entry]:
        return [scheduler.Entry.from_json(d) for d in self.settings.get("sched.entries")]

    def _check_schedules(self) -> None:
        entries = self.schedule_entries()
        if not entries:
            return
        now = datetime.now()
        start_now, missed = scheduler.check(entries, now, self._sched_done, self.settings.get("sched.late_minutes"))
        for d in missed:
            self._sched_done.add(d.key)
            log.warning("schedule %s missed (late by %.0f s)", d.entry.name, d.late_s)
            self._tell("missed_" + d.key, "Scheduled recording missed",
                       f"{d.entry.name} was due at {d.start:%H:%M} and was missed.", 1)
        for d in start_now:
            self._sched_done.add(d.key)
            if self.capturing:
                if d.entry.take_over and self._active_schedule is None:
                    self._pending_schedule = d
                    self.stop()
                else:
                    log.info("schedule %s skipped — busy", d.entry.name)
                    self._tell("busy_" + d.key, "Scheduled recording skipped",
                               f"{d.entry.name} was skipped because a recording is in progress.", 1)
                continue
            self._start_schedule(d)
        # open the device 10 s early (FR-SCHED-003)
        if not self.capturing and not self.recorder.input_open:
            if scheduler.upcoming(entries, now, 10):
                self.monitor(True)
                self.monitoring = False      # opened for the schedule, not by the user

    def _start_schedule(self, d: scheduler.Due) -> None:
        e = d.entry
        if self.state in PLAYBACK:
            self._stop_playback()
        self._overrides = {"vox": e.vox, "device": e.device, "codec": e.codec, "pattern": e.pattern,
                           "name": e.name}
        self.close_file()            # a schedule always starts a new file
        if self._start_capture(None):
            self._active_schedule = d
            self._timed_end = time.monotonic() + max(1.0, (d.end - datetime.now()).total_seconds())
            late = f" ({d.late_s / 60:.0f} min late)" if d.late_s > 30 else ""
            log.info("schedule %s started%s", e.name, late)
            self._tell("sched_start", "Scheduled recording", f"{e.name} started{late}.", 1)
        else:
            self._overrides = {}

    # ------------------------------------------------------------------ disk
    def _check_disk(self) -> None:
        folder = self.recorder.status.get("path")
        folder = os.path.dirname(folder) if folder else str(self.settings.recordings_folder())
        free = winutil.free_bytes(folder)
        self.disk_free = free
        self.disk_label = winutil.drive_label(winutil.existing_parent(folder))
        warn = self.settings.get("rec.disk_warn_mb") * 1024 * 1024
        stop = self.settings.get("rec.disk_stop_mb") * 1024 * 1024
        if free is None:
            self.disk_status = "ok"
            self.disk_seconds_left = None
            return
        bps = self.fmt().bytes_per_second() or 1
        self.disk_seconds_left = max(0.0, (free - stop) / bps)
        if free < stop:
            self.disk_status = "stop"
        elif free < warn:
            self.disk_status = "warn"
        else:
            self.disk_status = "ok"
            self._disk_blocked = False
        if self.capturing and not self._stopping:
            if self.disk_status == "stop":
                self._disk_blocked = True
                self._say("error", "Disk almost full",
                          f"Recording stopped: only {fmt_size(free)} free on {self.disk_label}. The file was saved.")
                self.stop()
            elif self.disk_status == "warn":
                self._tell("disk_warn", "Low disk space", f"{fmt_size(free)} free on {self.disk_label}.")

    # -------------------------------------------------------------- settings
    def _on_settings(self, keys: set) -> None:
        if keys & {"play.speed", "play.volume"}:
            self._apply_player_settings()
        if "vox.gap_ms" in keys and self.file:
            self.player.set_gap(self.settings.get("vox.gap_ms"), self.file.segment_starts())
        if self.recorder.input_open and any(k.startswith(("vox.", "fmt.gain", "fmt.highpass",
                                                          "rec.system_gain_db")) for k in keys):
            rec = self.recorder
            cfg = RecConfig.from_settings(self.settings, rec.cfg.tags.get("device", ""))
            self.recorder.update_config(cfg)
        if keys & {"rec.src_mic", "rec.src_system"} and self.recorder.input_open:
            self._apply_sources()
        if any(k.startswith("dev.") for k in keys):
            self.player.set_device(self.output_device())
            if self.recorder.input_open and not self.capturing:
                was = self.monitoring
                self.recorder.close_input()
                if was:
                    self.monitor(True)

    # ------------------------------------------------------------------ tones
    def _tone(self, start: bool) -> None:
        mode = self.settings.get("hotkeys.tones")
        if mode == "never" or (mode == "hidden" and self.window_visible):
            return
        vol = self.settings.get("hotkeys.tone_volume") / 100.0 * 0.5
        self.backend.play_tone(self.output_device(), 1320 if start else 660, 90, vol)

    # ------------------------------------------------------------------ status
    def level_percent(self) -> list[float]:
        """Microphone meter (the playback level while playing)."""
        if self.state in PLAYBACK:
            lv = self.player.levels
        elif self.recorder.input_open and self.recorder.mic_open:
            lv = self.recorder.levels
        else:
            return []
        return [min(100.0, float(v) * 100.0) for v in lv]

    def system_level_percent(self) -> list[float]:
        """Computer audio meter."""
        rec = self.recorder
        if self.state in PLAYBACK or not (rec.input_open and rec.system_open):
            return []
        return [min(100.0, float(v) * 100.0) for v in rec.sys_levels]

    def record_level_percent(self) -> list[float]:
        """Level of what is recorded (the ticked sources mixed); the playback level while playing."""
        if self.state in PLAYBACK:
            lv = self.player.levels
        elif self.recorder.input_open:
            lv = self.recorder.mix_levels
        else:
            return []
        return [min(100.0, float(v) * 100.0) for v in lv]

    def status_dict(self) -> dict:
        f = self.file
        seg_i = f.segment_index_at(self.position) + 1 if f else 0
        return {
            "state": self.state.value,
            "file": f.path if f else None,
            "segment": seg_i,
            "segments": len(f.segments) if f else 0,
            "duration_s": round(f.duration, 2) if f else 0,
            "position_s": round(self.position / f.rate, 2) if f and f.rate else 0,
            "size_bytes": f.size if f else 0,
            "level": round(max(self.record_level_percent() or [0]), 1),
            "level_mic": round(max(self.level_percent() or [0]), 1),
            "level_computer": round(max(self.system_level_percent() or [0]), 1),
            "sources": {"microphone": self.sources()[0], "computer_audio": self.sources()[1]},
            "version": __version__,
            "free_bytes": self.disk_free,
            "vox": self.vox_enabled(),
            "threshold": self.settings.get("vox.threshold"),
            "threshold_dbfs": round(20 * math.log10(max(1e-6, self.settings.get("vox.threshold") / 100)), 1),
        }


__all__ = ["Engine", "State", "CAPTURE", "PLAYBACK", "STATE_LABEL", "dbfs_to_percent"]
