"""Recording pipeline (Section 6).

PortAudio callback -> queue -> processor thread. The callback only copies the
block (FR-REC-002). The processor thread applies channel mapping, resampling,
gain, the high-pass filter, levels, Vox, segment bookkeeping, rollover,
flushing, journals and write-error fallback. It talks to the engine through a
control queue (in) and an event queue (out) so neither side blocks the other.

Two sources (v1.0.2): the microphone and computer audio (WASAPI loopback).
The microphone stream is the clock: each microphone block is mixed with the
same number of computer-audio frames taken from a small buffer. Loopback sends
nothing while nothing plays, so the buffer is padded with silence, and it keeps
~150 ms in reserve so that uneven delivery never punches holes in the sound.
When the microphone is off, a wall clock drives the processor instead. Either
source can be switched on or off at any time; the change applies from that
moment on.
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

import numpy as np

from vrp.core import dsp, metadata
from vrp.core.audio_backend import AudioError, BaseBackend, DeviceInfo
from vrp.core.encoders import BaseWriter, WriterError, create_writer
from vrp.core.journal import Journal
from vrp.core.model import FormatSpec, Segment, iso_now
from vrp.core.vox import VoxGate, VoxParams

log = logging.getLogger(__name__)

MONITOR, ARMED, RECORDING, PAUSED = "monitor", "armed", "recording", "paused"


@dataclass
class RecConfig:
    fmt: FormatSpec = field(default_factory=FormatSpec)
    vox_enabled: bool = True
    vox: VoxParams = field(default_factory=VoxParams)
    vox_mode: str = "segment"             # segment | file
    gain_db: int = 0
    highpass: bool = False
    timestamps: bool = True
    flush_s: float = 1.0
    fsync_s: float = 1.0
    rollover_bytes: int = 0
    rollover_seconds: int = 0
    rollover_midnight: bool = False
    rollover_every_hours: int = 0
    size_action: str = "rollover"         # rollover | stop
    sidecar: bool = False
    channel_map: list = field(default_factory=list)
    tags: dict = field(default_factory=dict)
    system_gain_db: int = 0               # computer audio level trim

    @staticmethod
    def from_settings(s, device_name: str = "") -> "RecConfig":
        fmt = FormatSpec.from_settings(s)
        vox_on = bool(s.get("vox.enabled")) and not s.get("vox.disabled")
        return RecConfig(
            fmt=fmt, vox_enabled=vox_on, vox=VoxParams.from_settings(s), vox_mode=s.get("vox.mode"),
            gain_db=s.get("fmt.gain_db"), highpass=s.get("fmt.highpass"), timestamps=s.get("rec.timestamps"),
            flush_s=s.get("rec.flush_s"), fsync_s=s.get("rec.fsync_s"),
            rollover_bytes=s.get("rec.rollover.size_mb") * 1024 * 1024,
            rollover_seconds=s.get("rec.rollover.minutes") * 60,
            rollover_midnight=s.get("rec.rollover.midnight"),
            rollover_every_hours=s.get("rec.rollover.every_hours"),
            size_action=s.get("rec.size_action"), sidecar=s.get("rec.sidecar_json"),
            channel_map=list(s.get("dev.input_channels") or []),
            system_gain_db=s.get("rec.system_gain_db"),
            tags={"artist": s.get("general.user_name"), "comment": s.get("rec.comment"),
                  "device": device_name, "format": fmt.describe()},
        )


_GAP = object()
SYS_PREBUFFER_S = 0.15     # computer audio held back to absorb uneven delivery
SYS_MAX_BUFFER_S = 0.6     # beyond this the buffer is trimmed (clock drift between devices)


class Recorder:
    """Owns the input stream, the processor thread and the open file."""

    def __init__(self, backend: BaseBackend, events: queue.SimpleQueue,
                 path_provider: Callable[[str | None], str], fallback_folder: Callable[[], str]):
        self.backend = backend
        self.events = events
        self.path_provider = path_provider
        self.fallback_folder = fallback_folder
        self.cfg = RecConfig()
        self.stream = None
        self.device: DeviceInfo | None = None
        self.capture_rate = 0
        self.capture_channels = 0
        self.q: queue.SimpleQueue = queue.SimpleQueue()
        self.ctl: queue.SimpleQueue = queue.SimpleQueue()
        self.thread: threading.Thread | None = None
        self.overflows = 0
        self.last_block = 0.0
        self.levels = np.zeros(1, dtype=np.float32)       # microphone (after gain / filter)
        self.sys_levels = np.zeros(1, dtype=np.float32)   # computer audio
        self.mix_levels = np.zeros(1, dtype=np.float32)   # what is recorded (both, as ticked)
        self.clip_until = 0.0
        self.sys_clip_until = 0.0
        self.status: dict = {"mode": MONITOR}
        self.session_open = False
        self.sys_stream = None
        self.sys_q: queue.SimpleQueue = queue.SimpleQueue()
        self._sys_gen = 0
        self.blocksize, self.latency, self.exclusive = 1024, "high", False
        self._reset_file_state()
        self.mode = MONITOR

    # ================================================================ public API
    def open_input(self, dev: DeviceInfo | None, cfg: RecConfig, blocksize: int = 1024, latency: str = "high",
                   exclusive: bool = False) -> None:
        """Start the processor thread (monitor mode) and open the microphone (dev) if given.

        dev None: no microphone; the wall clock drives the processor (computer audio only).
        """
        self.cfg = cfg
        self.blocksize, self.latency, self.exclusive = blocksize, latency, exclusive
        if self.thread is None or not self.thread.is_alive():
            self.mode = MONITOR
            self.thread = threading.Thread(target=self._run, name="vrp-writer", daemon=True)
            self.thread.start()
        self.session_open = True
        self.set_mic(dev)

    def _open_stream(self, dev, blocksize, latency, exclusive) -> None:
        fmt = self.cfg.fmt
        cmap = [c for c in self.cfg.channel_map if isinstance(c, int) and c >= 0]
        want = (max(cmap) + 1) if cmap else fmt.channels
        if dev.max_in and want > dev.max_in:
            want = dev.max_in
            cmap = [c for c in cmap if c < want]
        self.channel_map = cmap
        rate = fmt.rate
        if not self.backend.check_input(dev, rate, want, exclusive):
            rate = int(dev.default_rate) or 48000
            log.info("device does not support %d Hz natively; capturing at %d Hz and resampling",
                     fmt.rate, rate)
        self.capture_rate, self.capture_channels = rate, want
        self.blocksize, self.latency, self.exclusive = blocksize, latency, exclusive
        self.stream = self.backend.open_input(dev, rate, want, blocksize, latency, self._callback, exclusive)
        self.last_block = time.monotonic()
        self.ctl.put(("stream_opened", rate, want))
        log.info("input opened: %s [%s] %d Hz x%d", dev.name, dev.host_api, rate, want)

    @property
    def resampled(self) -> bool:
        return self.capture_rate != self.cfg.fmt.rate

    def set_mic(self, dev: DeviceInfo | None) -> None:
        """Switch the microphone on (open dev) or off (None) while the session runs.

        Raises AudioError when the device cannot be opened; the microphone is then off.
        """
        self._close_stream()
        if dev is None:
            self.ctl.put(("mic", False, False))
            return
        self.device = dev
        self.ctl.put(("mic", True, False))
        try:
            self._open_stream(dev, self.blocksize, self.latency, self.exclusive)
        except Exception:
            self.ctl.put(("mic", False, False))
            raise

    def set_system(self, on: bool, on_info=None) -> None:
        """Switch computer audio on or off. Raises LoopbackError when it cannot be captured."""
        if on:
            if self.sys_stream is None:
                self._sys_gen += 1
                gen, q = self._sys_gen, self.sys_q

                def on_block(block, rate, channels, _gen=gen):
                    q.put((_gen, rate, channels, block))
                self.sys_stream = self.backend.open_loopback(on_block, on_info)
            self.ctl.put(("system", True))
            return
        s, self.sys_stream = self.sys_stream, None
        self.ctl.put(("system", False))
        if s is not None:
            try:
                s.close()
            except Exception:
                log.debug("loopback close failed", exc_info=True)

    @property
    def mic_open(self) -> bool:
        return self.stream is not None

    @property
    def system_open(self) -> bool:
        return self.sys_stream is not None

    @property
    def system_name(self) -> str:
        return getattr(self.sys_stream, "name", "") if self.sys_stream is not None else ""

    def replace_device(self, dev: DeviceInfo) -> None:
        """Re-open capture on a device after it was lost (FR-DEV-007)."""
        self._close_stream()
        self.device = dev
        self.ctl.put(("mic", True, False))
        self._open_stream(dev, self.blocksize, self.latency, self.exclusive)
        self.ctl.put(("gap",))

    def suspend_capture(self) -> None:
        """Microphone lost: close the stream, keep the file open (computer audio continues)."""
        self._close_stream()
        self.ctl.put(("mic", False, True))
        self.ctl.put(("gap",))

    def close_input(self) -> None:
        self._close_stream()
        if self.sys_stream is not None:
            self.set_system(False)
        if self.thread and self.thread.is_alive():
            self.ctl.put(("quit",))
            self.thread.join(timeout=5)
        self.thread = None
        self.session_open = False
        self.levels = np.zeros(1, dtype=np.float32)
        self.sys_levels = np.zeros(1, dtype=np.float32)
        self.mix_levels = np.zeros(1, dtype=np.float32)

    def _close_stream(self) -> None:
        s, self.stream = self.stream, None
        if s is not None:
            try:
                s.stop()
                s.close()
            except Exception:
                log.debug("stream close failed", exc_info=True)

    @property
    def input_open(self) -> bool:
        """A capture session is running (microphone, computer audio or both)."""
        return self.session_open

    # control messages (thread-safe)
    def start(self, append_path: str | None = None) -> None:
        self.ctl.put(("start", append_path))

    def pause(self) -> None:
        self.ctl.put(("pause",))

    def resume(self) -> None:
        self.ctl.put(("resume",))

    def stop(self, reason: str = "user") -> None:
        self.ctl.put(("stop", reason))

    def insert_boundary(self) -> None:
        self.ctl.put(("boundary",))

    def set_threshold(self, pct: float) -> None:
        self.ctl.put(("threshold", pct))

    def set_vox_enabled(self, on: bool) -> None:
        self.ctl.put(("vox_enabled", on))

    def update_config(self, cfg: RecConfig) -> None:
        """Apply settings that can change on the fly (Vox, gain, filter)."""
        self.ctl.put(("config", cfg))

    def switch_folder(self, folder: str | None) -> None:
        self.ctl.put(("folder", folder))

    # =============================================================== callback
    def _callback(self, indata, frames, time_info, status):
        if status is not None and getattr(status, "input_overflow", False):
            self.overflows += 1
        self.q.put(indata.copy())
        self.last_block = time.monotonic()

    # ========================================================== processor thread
    def _reset_file_state(self) -> None:
        self.writer: BaseWriter | None = None
        self.journal: Journal | None = None
        self.segments: list[Segment] = []
        self.seg_open = False
        self.file_t0 = 0.0
        self.file_key = None

    def _emit(self, *ev) -> None:
        self.events.put(ev)

    def _run(self) -> None:
        self.gate = VoxGate(self.cfg.fmt.rate, self.cfg.fmt.channels, self.cfg.vox)
        self._mic_live = False
        self._mic_lost = False
        self._sys_live = False
        self._clock_t0: float | None = None
        self._clock_done = 0
        self._setup_dsp()
        self.in_pos = 0
        self.t0_wall = None
        self.folder_override: str | None = None
        self.last_flush = self.last_fsync = time.monotonic()
        self.backlog_warned = False
        self._lost_reported = False
        quit_ = False
        while not quit_:
            try:
                while True:
                    msg = self.ctl.get_nowait()
                    if msg[0] == "quit":
                        quit_ = True
                        break
                    self._control(msg)
            except queue.Empty:
                pass
            if quit_:
                break
            self._pump_system()
            try:
                x = self.q.get(timeout=0.02)
            except queue.Empty:
                x = None
            n_clock = self._clock_due() if x is None else 0
            if x is None and not n_clock:
                self._idle()
                continue
            try:
                self._process(x, n_clock)
            except Exception as exc:          # FR-ERR-021: emergency finalise first
                log.exception("processor failure")
                self._emergency_finalize()
                self.mode = MONITOR
                self._emit("error", "internal", f"Recording stopped after an internal error: {exc}")
                self._emit("stopped", "error", None)
            self._publish_status()
        # leaving: make sure nothing is left open
        if self.writer is not None:
            self._finalize_file()
        self._publish_status()

    def _setup_dsp(self) -> None:
        fmt = self.cfg.fmt
        self.resampler = dsp.Resampler(self.capture_rate or fmt.rate, fmt.rate, fmt.channels)
        self.hpf = dsp.HighPass(fmt.rate, fmt.channels) if self.cfg.highpass else None
        self.gain = dsp.db_to_gain(self.cfg.gain_db)
        # computer audio path
        self._sys_gain = dsp.db_to_gain(self.cfg.system_gain_db)
        self._sys_rs: dsp.Resampler | None = None
        self._sys_fmt: tuple | None = None
        self._fifo: list[np.ndarray] = []
        self._fifo_n = 0
        self._fifo_since = 0.0
        self._sys_primed = False
        self._prebuf = int(SYS_PREBUFFER_S * fmt.rate)
        self._fifo_cap = int(SYS_MAX_BUFFER_S * fmt.rate)

    # ----------------------------------------------------------- two sources
    def _clock_factor(self) -> float:
        try:
            return float(self.backend.clock_factor()) or 1.0
        except Exception:
            return 1.0

    def _clock_due(self) -> int:
        """Frames the wall clock asks for while the microphone is off (0 = nothing yet)."""
        if self._mic_live or (self._mic_lost and not self._sys_live):
            return 0                      # microphone drives, or it was lost with nothing else to record
        rate = self.cfg.fmt.rate
        now = time.monotonic()
        if self._clock_t0 is None:
            self._clock_t0, self._clock_done = now, 0
            return 0
        due = int((now - self._clock_t0) * rate * self._clock_factor()) - self._clock_done
        if due > 2 * rate:                # the PC slept or the thread stalled: do not invent silence
            self._clock_t0, self._clock_done = now, 0
            self._control(("gap",))
            return 0
        return due if due >= rate // 100 else 0

    def _pump_system(self) -> None:
        """Move delivered computer audio into the buffer at the file's rate and channel count."""
        fmt = self.cfg.fmt
        while True:
            try:
                gen, rate, ch, block = self.sys_q.get_nowait()
            except queue.Empty:
                break
            if gen != self._sys_gen or not self._sys_live or block.size == 0:
                continue
            if self._sys_fmt != (rate, ch, fmt.rate, fmt.channels) or self._sys_rs is None:
                self._sys_fmt = (rate, ch, fmt.rate, fmt.channels)
                self._sys_rs = dsp.Resampler(rate, fmt.rate, fmt.channels)
            y = self._sys_rs.process(dsp.to_channels(block, fmt.channels))
            if y.shape[0] == 0:
                continue
            if self._sys_gain != 1.0:
                y = y * self._sys_gain
            if not self._fifo_n:
                self._fifo_since = time.monotonic()
            self._fifo.append(y)
            self._fifo_n += y.shape[0]
        if self._fifo_n > self._fifo_cap:        # device clocks drift apart: drop the oldest audio
            self._fifo_pop(self._fifo_n - self._prebuf)

    def _fifo_pop(self, k: int) -> np.ndarray:
        data = np.concatenate(self._fifo) if len(self._fifo) > 1 else self._fifo[0]
        out, rest = data[:k], data[k:]
        self._fifo = [rest] if rest.shape[0] else []
        self._fifo_n = rest.shape[0]
        return out

    def _sys_take(self, n: int) -> np.ndarray | None:
        """n frames of computer audio (silence where nothing was delivered); None when off."""
        if not self._sys_live:
            return None
        out = np.zeros((n, self.cfg.fmt.channels), dtype=np.float32)
        if not self._sys_primed:
            waited = time.monotonic() - self._fifo_since
            if self._fifo_n and (self._fifo_n >= self._prebuf or
                                 waited * self._clock_factor() >= SYS_PREBUFFER_S):
                self._sys_primed = True
            else:
                return out
        k = min(n, self._fifo_n)
        if k:
            out[:k] = self._fifo_pop(k)
        if k < n:
            self._sys_primed = False              # nothing playing any more
        return out

    def _clear_system(self) -> None:
        self._fifo, self._fifo_n, self._sys_primed = [], 0, False
        self._sys_rs, self._sys_fmt = None, None
        self.sys_levels = np.zeros(self.cfg.fmt.channels, dtype=np.float32)

    # ---------------------------------------------------------------- control
    def _control(self, msg) -> None:
        kind = msg[0]
        if kind == "mic":
            self._mic_live, self._mic_lost = bool(msg[1]), bool(msg[2])
            if not self._mic_live:
                self._clock_t0 = None             # the wall clock takes over from now
                self.levels = np.zeros(self.cfg.fmt.channels, dtype=np.float32)
            return
        if kind == "system":
            self._sys_live = bool(msg[1])
            self._clear_system()
            return
        if kind == "stream_opened":
            self.capture_rate, self.capture_channels = msg[1], msg[2]
            self.resampler = dsp.Resampler(self.capture_rate, self.cfg.fmt.rate, self.cfg.fmt.channels)
            self.t0_wall = None
            self._lost_reported = False
        elif kind == "start":
            self._start(msg[1])
        elif kind == "pause":
            if self.mode in (ARMED, RECORDING):
                self._apply(self._close_ops())
                self.mode = PAUSED
                self._emit("mode", PAUSED)
        elif kind == "resume":
            if self.mode == PAUSED:
                self.gate.reset()
                self.mode = ARMED if self.cfg.vox_enabled else RECORDING
                self._emit("mode", self.mode)
        elif kind == "stop":
            self._stop(msg[1])
        elif kind == "boundary":
            if self.seg_open and self.writer is not None:
                self.segments.append(Segment(self.writer.segment_start(), self._ts(self.in_pos)))
                self._emit("segment", len(self.segments), self.segments[-1].timestamp)
        elif kind == "threshold":
            self.cfg.vox.threshold = msg[1]
            self.gate.set_threshold(msg[1])
        elif kind == "vox_enabled":
            on = msg[1]
            if on == self.cfg.vox_enabled:
                return
            self.cfg.vox_enabled = on
            if self.mode in (ARMED, RECORDING):
                if on:   # continuous -> Vox: close the segment, let the gate decide
                    if self.seg_open:
                        self._end_segment()
                    self.gate.reset()
                    self.mode = ARMED
                else:    # Vox -> continuous: close any Vox segment, record from now on
                    self._apply(self.gate.flush_end())
                    self.mode = RECORDING
                self._emit("mode", self.mode)
        elif kind == "config":
            cfg: RecConfig = msg[1]
            old = self.cfg
            cfg.fmt = old.fmt                       # format changes apply to the next file only
            cfg.vox_enabled = old.vox_enabled if self.mode in (ARMED, RECORDING, PAUSED) else cfg.vox_enabled
            self.cfg = cfg
            self.gate.set_params(cfg.vox)
            self.gain = dsp.db_to_gain(cfg.gain_db)
            self._sys_gain = dsp.db_to_gain(cfg.system_gain_db)
            if cfg.highpass and self.hpf is None:
                self.hpf = dsp.HighPass(cfg.fmt.rate, cfg.fmt.channels)
            elif not cfg.highpass:
                self.hpf = None
        elif kind == "folder":
            self.folder_override = msg[1]
        elif kind == "gap":
            # capture interrupted: close the current segment, keep the file
            if self.mode in (ARMED, RECORDING):
                self._apply(self._close_ops())
                self.gate.reset()
                if not self.cfg.vox_enabled:
                    self.mode = RECORDING
            self.t0_wall = None

    def _start(self, append_path: str | None) -> None:
        if self.mode in (ARMED, RECORDING, PAUSED):
            return
        # a format change applies from the next file (FR-REC-014)
        fmt = self.cfg.fmt
        if self.gate.rate != fmt.rate or self.gate.channels != fmt.channels:
            self.gate = VoxGate(fmt.rate, fmt.channels, self.cfg.vox)
        self.gate.set_params(self.cfg.vox)
        self.gate.reset()
        self._setup_dsp()
        self._clock_t0 = None
        if append_path:
            try:
                self._open_file(append_path, append=True)
            except Exception as exc:
                log.warning("append to %s failed (%s); starting a new file", append_path, exc)
                self._emit("info", f"Could not append to {os.path.basename(append_path)} ({exc}); "
                                   "a new file was started instead.")
        self.mode = ARMED if self.cfg.vox_enabled else RECORDING
        self._emit("mode", self.mode)

    def _stop(self, reason: str) -> None:
        path = self.writer.path if self.writer else None
        if self.mode in (ARMED, RECORDING):
            self._apply(self._close_ops())
        self.mode = MONITOR
        if self.writer is not None:
            self._finalize_file()
        self.gate.reset()
        self._emit("mode", MONITOR)
        self._emit("stopped", reason, path)

    def _close_ops(self) -> list:
        if self.cfg.vox_enabled:
            return self.gate.flush_end()
        if self.seg_open:
            return [("end", None)]
        return []

    # ----------------------------------------------------------------- audio
    def _ts(self, sample: int) -> str | None:
        if not self.cfg.timestamps:
            return None
        return iso_now(datetime.fromtimestamp(self._sample_time(sample)))

    def _sample_time(self, sample: int) -> float:
        if self.t0_wall is None:
            return time.time()
        return self.t0_wall + (sample - self.t0_sample) / self.cfg.fmt.rate

    def _mic_frames(self, x: np.ndarray) -> np.ndarray:
        """Microphone block -> file rate and channels, with gain and filter."""
        fmt = self.cfg.fmt
        if x.ndim == 1:
            x = x[:, None]
        if self.channel_map:
            x = x[:, [c for c in self.channel_map if c < x.shape[1]] or [0]]
        if fmt.channels == 1 and x.shape[1] > 1:
            x = x[:, :2].mean(axis=1, keepdims=True)
        elif fmt.channels == 2 and x.shape[1] == 1:
            x = np.repeat(x, 2, axis=1)
        elif x.shape[1] > fmt.channels:
            x = x[:, :fmt.channels]
        x = np.ascontiguousarray(x, dtype=np.float32)
        x = self.resampler.process(x)
        if x.shape[0] == 0:
            return x
        if self.gain != 1.0:
            x = x * self.gain
        if self.hpf is not None:
            x = self.hpf.process(x)
        peaks = dsp.peak_levels(x)
        self.levels = peaks
        if peaks.size and float(np.max(peaks)) >= 0.99:
            self.clip_until = time.monotonic() + 2.0
        return x

    def _process(self, x: np.ndarray | None, n_clock: int = 0) -> None:
        """One microphone block (x), or n_clock frames of wall-clock time; mixed with computer audio."""
        fmt = self.cfg.fmt
        mic = None
        if x is not None:
            x = self._mic_frames(x)
            if x.shape[0] == 0:
                return
            n = x.shape[0]
            if self._mic_live:
                mic = x
        else:
            n = n_clock
        if not self._mic_live:
            self._clock_done += n
        sys_ = self._sys_take(n)
        if sys_ is not None:
            sp = dsp.peak_levels(sys_)
            self.sys_levels = sp
            if sp.size and float(np.max(sp)) >= 0.99:
                self.sys_clip_until = time.monotonic() + 2.0
        if mic is None and sys_ is None:
            x = np.zeros((n, fmt.channels), dtype=np.float32)      # nothing ticked: silence
        elif sys_ is None:
            x = mic
        elif mic is None:
            x = sys_
        else:
            x = np.clip(mic + sys_, -1.0, 1.0)
        self.mix_levels = dsp.peak_levels(x)
        start = self.in_pos
        self.in_pos += n
        if self.t0_wall is None:
            self.t0_wall = time.time() - n / fmt.rate
            self.t0_sample = start
        if self.mode in (ARMED, RECORDING):
            if self.cfg.vox_enabled:
                ops = self.gate.process(x, start)
                new_mode = RECORDING if self.gate.active else ARMED
                if new_mode != self.mode:
                    self.mode = new_mode
                    self._emit("mode", new_mode)
            else:
                ops = [] if self.seg_open else [("begin", start)]
                ops.append(("write", x, start))
            self._apply(ops)
        self._periodic()
        # writer falling behind (FR-ERR-008)
        backlog = self.q.qsize() * (self.blocksize / max(1, self.capture_rate)) if hasattr(self, "blocksize") else 0
        if backlog > 30 and not self.backlog_warned:
            self.backlog_warned = True
            self._emit("backlog", backlog)
            fb = self.fallback_folder()
            if self.writer is not None and fb and not self._in_folder(fb):
                self.folder_override = fb
                self._rollover(self.in_pos, reason="backlog")

    def _apply(self, ops: list) -> None:
        for op in ops:
            if op[0] == "begin":
                self._begin_segment(op[1])
            elif op[0] == "write":
                self._write(op[1], op[2])
            elif op[0] == "end":
                self._end_segment()

    def _begin_segment(self, sample: int) -> None:
        if self.writer is None:
            if not self._open_new_file(sample):
                return
        self.segments.append(Segment(self.writer.segment_start(), self._ts(sample)))
        self.seg_open = True
        self._emit("segment", len(self.segments), self.segments[-1].timestamp)

    def _end_segment(self) -> None:
        self.seg_open = False
        if self.cfg.vox_enabled and self.cfg.vox_mode == "file" and self.writer is not None:
            self._finalize_file()

    def _write(self, x: np.ndarray, sample: int) -> None:
        if self.writer is None:
            self._begin_segment(sample)
            if self.writer is None:
                return
        roll = self._rollover_reason(sample)
        if roll == "stop":
            self._stop("size")
            self._emit("autostop", "size")
            return
        if roll:
            self._rollover(sample, roll)
            if self.writer is None:
                return
        try:
            self.writer.write(x)
        except OSError as exc:
            self._write_failed(exc, sample)
            if self.writer is not None:
                try:
                    self.writer.write(x)
                except OSError:
                    pass

    # -------------------------------------------------------------- files
    def _folder(self) -> str | None:
        return self.folder_override

    def _in_folder(self, folder: str) -> bool:
        return bool(self.writer) and os.path.normcase(os.path.dirname(os.path.abspath(self.writer.path))) == \
            os.path.normcase(os.path.abspath(folder))

    def _open_file(self, path: str, append: bool = False) -> None:
        w = create_writer(path, self.cfg.fmt, append=append)
        self.writer = w
        self.segments = list(w.existing_segments)
        if append and w.frames > 0 and not self.segments:
            self.segments = [Segment(0)]
        self.seg_open = False
        now = time.time() if self.t0_wall is None else self._sample_time(self.in_pos)
        self.file_t0 = now
        self.file_key = self._time_key(now)
        self.file_started_iso = iso_now(datetime.fromtimestamp(now))
        self.journal = Journal(path)
        self._write_journal()
        self._emit("file_opened", path, self.cfg.fmt, list(self.segments), w.frames, append)

    def _open_new_file(self, sample: int) -> bool:
        last_exc = None
        for _ in range(5):
            path = self.path_provider(self._folder())
            try:
                self._open_file(path)
                self.file_t0 = self._sample_time(sample)
                self.file_key = self._time_key(self.file_t0)
                self.file_started_iso = iso_now(datetime.fromtimestamp(self.file_t0))
                return True
            except FileExistsError:
                continue                       # FR-ERR-016: another writer took the name
            except (OSError, WriterError) as exc:
                last_exc = exc
                fb = self.fallback_folder()
                if fb and self.folder_override != fb:
                    log.warning("cannot create %s (%s); switching to %s", path, exc, fb)
                    self._emit("write_error", path, str(exc), fb)
                    self.folder_override = fb
                    continue
                break
        self._emit("error", "create", f"Could not create a recording file: {last_exc}")
        self.mode = MONITOR
        self._emit("mode", MONITOR)
        self._emit("stopped", "error", None)
        return False

    def _tags(self) -> dict:
        t = dict(self.cfg.tags)
        if self.writer:
            t["title"] = os.path.splitext(os.path.basename(self.writer.path))[0]
        t["date"] = getattr(self, "file_started_iso", "")
        return t

    def _finalize_file(self) -> None:
        w, j = self.writer, self.journal
        if w is None:
            return
        end = w.segment_start()
        segs = list(self.segments)
        while len(segs) > 1 and segs[-1].start >= end:     # drop empty trailing segments
            segs.pop()
        segs = segs or [Segment(0)]
        frames = w.frames
        try:
            w.finalize(segs, self._tags())
            if self.cfg.sidecar:
                metadata.write_sidecar(w.path, segs, self.cfg.fmt.rate, self._tags())
            if j:
                j.close()
            self._emit("file_closed", w.path, frames, segs)
        except Exception as exc:
            log.exception("finalise failed for %s", w.path)
            w.abort()
            self._emit("error", "finalize", f"{os.path.basename(w.path)} could not be finalised ({exc}). "
                                            "It will be offered for recovery at the next start.")
            self._emit("file_closed", w.path, frames, segs)
        self._reset_file_state()

    def _emergency_finalize(self) -> None:
        if self.writer is not None:
            try:
                self._finalize_file()
            except Exception:
                log.exception("emergency finalise failed")

    def _write_failed(self, exc: Exception, sample: int) -> None:
        """FR-ERR-003: finish what we can, continue in the fallback folder."""
        path = self.writer.path if self.writer else ""
        log.error("write failed on %s: %s", path, exc)
        try:
            self._finalize_file()
        except Exception:
            if self.writer:
                self.writer.abort()
            self._reset_file_state()
        fb = self.fallback_folder()
        if fb and self.folder_override != fb:
            self.folder_override = fb
            self._emit("write_error", path, str(exc), fb)
            self._begin_segment(sample)
        else:
            self._emit("error", "write", f"Recording stopped: the file could not be written ({exc}).")
            self.mode = MONITOR
            self._emit("mode", MONITOR)
            self._emit("stopped", "error", path)

    def _time_key(self, t: float):
        lt = time.localtime(t)
        n = self.cfg.rollover_every_hours
        return (lt.tm_year, lt.tm_yday, (lt.tm_hour // n) if n else 0)

    def _rollover_reason(self, sample: int) -> str | None:
        w = self.writer
        if w is None:
            return None
        cfg = self.cfg
        if cfg.rollover_bytes and w.size >= cfg.rollover_bytes:
            return "stop" if cfg.size_action == "stop" else "size"
        if getattr(w, "near_limit", False):
            return "wav_limit"
        if cfg.rollover_seconds and w.frames >= cfg.rollover_seconds * cfg.fmt.rate:
            return "duration"
        if cfg.rollover_midnight or cfg.rollover_every_hours:
            key = self._time_key(self._sample_time(sample))
            if cfg.rollover_midnight and key[:2] != self.file_key[:2]:
                return "midnight"
            if cfg.rollover_every_hours and key != self.file_key:
                return "hourly"
        return None

    def _rollover(self, sample: int, reason: str) -> None:
        """Close this file and continue seamlessly in a new one (FR-FILE-007)."""
        was_open = self.seg_open
        self._finalize_file()
        self._emit("rollover", reason)
        if was_open and self._open_new_file(sample):
            self.segments.append(Segment(0, self._ts(sample)))
            self.seg_open = True
            self._emit("segment", 1, self.segments[-1].timestamp)

    def _write_journal(self) -> None:
        if self.journal is None or self.writer is None:
            return
        self.journal.write({
            "version": 1, "path": self.writer.path, "format": self.cfg.fmt.to_json(),
            "segments": [s.to_json() for s in self.segments], "frames": self.writer.frames,
            "tags": self._tags(), "writer": self.writer.journal_state(), "updated": time.time(),
        })

    def _periodic(self) -> None:
        if self.writer is None:
            return
        now = time.monotonic()
        try:
            if now - self.last_flush >= self.cfg.flush_s:
                self.writer.flush()
                self._write_journal()
                self.last_flush = now
            if now - self.last_fsync >= self.cfg.fsync_s:
                self.writer.fsync()
                self.last_fsync = now
        except OSError as exc:
            self._write_failed(exc, self.in_pos)

    def _idle(self) -> None:
        """No audio for 50 ms: keep flushing, detect a lost device (FR-DEV-007)."""
        self._periodic()
        if self.stream is not None and not self._lost_reported:
            if time.monotonic() - self.last_block > 2.0:
                self._lost_reported = True
                self._emit("device_lost", self.device.name if self.device else "")
        self._publish_status()

    def _publish_status(self) -> None:
        w = self.writer
        self.status = {
            "mode": self.mode,
            "mic": getattr(self, "_mic_live", False),
            "system": getattr(self, "_sys_live", False),
            "path": w.path if w else None,
            "frames": w.frames if w else 0,
            "position": w.segment_start() if w else 0,
            "size": w.size if w else 0,
            "segments": len(self.segments),
            "seg_open": self.seg_open,
            "rate": self.cfg.fmt.rate,
        }


def open_error_message(exc: AudioError) -> str:
    if exc.privacy:
        return ("Windows did not allow access to the microphone. Check Settings > Privacy & security > "
                "Microphone and allow desktop apps to use it.")
    return str(exc)
