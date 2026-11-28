"""Recording pipeline.

PortAudio callback -> queue -> processor thread. The callback only copies the
block. The processor thread applies channel mapping, resampling,
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

Channel layout (v1.0.3): "mix" sums the ticked sources into every channel of
the file; "split" writes a stereo file with the microphone alone on the left
and computer audio alone on the right. In split mode each source is processed
as one channel and the two are placed side by side, never summed.

Clean-up (v1.0.4): the microphone can pass through noise
suppression and an automatic leveller (vrp.core.cleanup). Three versions of the
recorded signal then exist side by side, all with the same sample numbers:
  detect  - after noise suppression only; Vox, the meter and the threshold use it
  out     - after the leveller too; this is what the recording gets
  raw     - before clean-up; written to an untouched copy "<name>-original.<ext>"
Noise suppression delays the microphone by a fixed amount, so computer audio and
the untouched copy are delayed by the same amount and every file stays aligned.
Vox hands out write operations by sample number; the recorder takes the same
range of `out` and `raw` from short histories.

Live transcript (1.1.0): every block written to the file is also handed to `tap`
(file position, wall-clock time, file path), and `tap_close` is called when a file
is finished, so the transcript follows the recording exactly, Vox pauses included.
With save_audio off ("Text only"), a NullWriter stands in for the file: nothing is
written to disk, but positions, segments and rollovers work as before.
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

from vrp import paths
from vrp.core import dsp, metadata
from vrp.core.audio_backend import AudioError, BaseBackend, DeviceInfo
from vrp.core.cleanup import CleanChain, DelayLine, History
from vrp.core.encoders import BaseWriter, NullWriter, WriterError, create_writer
from vrp.core.journal import Journal
from vrp.core.model import SPLIT_ROLES, FormatSpec, Segment, iso_now, original_path
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
    denoise: bool = False                 # 1.0.4: noise suppression on the microphone
    level: bool = False                   # 1.0.4: automatic level on the microphone
    keep_original: bool = True            # 1.0.4: also write an untouched copy when cleaning
    save_audio: bool = True               # 1.1.0: False = live transcript "Text only"

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
            denoise=bool(s.get("fmt.denoise")), level=bool(s.get("fmt.level")),
            keep_original=bool(s.get("fmt.keep_original")),
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
        self.levels = np.zeros(1, dtype=np.float32)       # microphone as Vox hears it (after noise suppression)
        self.in_levels = np.zeros(1, dtype=np.float32)    # microphone input (after gain / filter, before clean-up)
        self.sys_levels = np.zeros(1, dtype=np.float32)   # computer audio
        self.mix_levels = np.zeros(1, dtype=np.float32)   # what is recorded (both, as ticked; before the leveller)
        self.clean: CleanChain | None = None
        self.clip_until = 0.0
        self.sys_clip_until = 0.0
        self.status: dict = {"mode": MONITOR}
        self.session_open = False
        self.sys_stream = None
        self.sys_q: queue.SimpleQueue = queue.SimpleQueue()
        self._sys_gen = 0
        self.blocksize, self.latency, self.exclusive = 1024, "high", False
        self.tap: Callable | None = None          # 1.1.0: (block, file frame, rate, wall time, path)
        self.tap_close: Callable | None = None    # 1.1.0: (path) when a file is finished
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
        want = (max(cmap) + 1) if cmap else self._src_ch()
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
        """Re-open capture on a device after it was lost."""
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
        self.in_levels = np.zeros(1, dtype=np.float32)
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
        self.orig_writer: BaseWriter | None = None        # 1.0.4: untouched copy
        self.orig_journal: Journal | None = None
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
            except Exception as exc:          # Emergency finalise first
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
        ch = self._src_ch()
        self.resampler = dsp.Resampler(self.capture_rate or fmt.rate, fmt.rate, ch)
        self.hpf = dsp.HighPass(fmt.rate, ch) if self.cfg.highpass else None
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
        self._setup_clean()

    def _setup_clean(self) -> None:
        """Build the clean-up chain for the next recording."""
        if self.clean is not None:
            self.clean.close()
        cfg, fmt = self.cfg, self.cfg.fmt
        ch = self._src_ch()
        self.clean = None
        self.clean_error = ""
        if cfg.denoise or cfg.level:
            chain = CleanChain(fmt.rate, ch, cfg.denoise, cfg.level)
            self.clean_error = chain.error
            if chain.active:
                self.clean = chain
        d = self.clean.delay if self.clean is not None else 0
        self._sys_delay = DelayLine(d, ch) if d else None
        self._keep_raw = self.clean is not None and cfg.keep_original and cfg.save_audio
        self._raw_delay = DelayLine(d, fmt.channels) if (self._keep_raw and d) else None
        # `out` differs from `detect` only with the leveller, `raw` only with noise suppression
        lev = self.clean is not None and self.clean.level is not None
        den = self.clean is not None and self.clean.denoise is not None
        self._hist_out = History(fmt.rate, fmt.channels) if lev else None
        self._hist_raw = History(fmt.rate, fmt.channels) if (self._keep_raw and den) else None

    # ----------------------------------------------------------- two sources
    def _src_ch(self) -> int:
        """Channels each source is processed in: 1 when split (each fills one side), else the file's."""
        return 1 if self.cfg.fmt.split else self.cfg.fmt.channels

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
            out_ch = self._src_ch()
            if self._sys_fmt != (rate, ch, fmt.rate, out_ch) or self._sys_rs is None:
                self._sys_fmt = (rate, ch, fmt.rate, out_ch)
                self._sys_rs = dsp.Resampler(rate, fmt.rate, out_ch)
            y = self._sys_rs.process(dsp.to_channels(block, out_ch))
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
        out = np.zeros((n, self._src_ch()), dtype=np.float32)
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
        self.sys_levels = np.zeros(self._src_ch(), dtype=np.float32)

    # ---------------------------------------------------------------- control
    def _control(self, msg) -> None:
        kind = msg[0]
        if kind == "mic":
            self._mic_live, self._mic_lost = bool(msg[1]), bool(msg[2])
            if not self._mic_live:
                self._clock_t0 = None             # the wall clock takes over from now
                self.levels = np.zeros(self._src_ch(), dtype=np.float32)
            return
        if kind == "system":
            self._sys_live = bool(msg[1])
            self._clear_system()
            return
        if kind == "stream_opened":
            self.capture_rate, self.capture_channels = msg[1], msg[2]
            self.resampler = dsp.Resampler(self.capture_rate, self.cfg.fmt.rate, self._src_ch())
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
                self.hpf = dsp.HighPass(cfg.fmt.rate, self._src_ch())
            elif not cfg.highpass:
                self.hpf = None
            # clean-up changes apply from the next recording; while idle, at once (meter)
            if self.mode == MONITOR and (cfg.denoise, cfg.level, cfg.keep_original) != \
                    (old.denoise, old.level, old.keep_original):
                self._setup_clean()
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
        # a format change applies from the next file
        fmt = self.cfg.fmt
        if self.gate.rate != fmt.rate or self.gate.channels != fmt.channels:
            self.gate = VoxGate(fmt.rate, fmt.channels, self.cfg.vox)
        self.gate.set_params(self.cfg.vox)
        self.gate.reset()
        self._setup_dsp()
        self._clock_t0 = None
        if self.cfg.denoise and self.clean_error:
            self._emit("info", f"Noise suppression is not available, so this recording is not cleaned of "
                               f"background noise: {self.clean_error}.")
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
        """Microphone block -> file rate and source channels (1 when split), with gain and filter."""
        ch = self._src_ch()
        if x.ndim == 1:
            x = x[:, None]
        if self.channel_map:
            x = x[:, [c for c in self.channel_map if c < x.shape[1]] or [0]]
        if ch == 1 and x.shape[1] > 1:
            x = x[:, :2].mean(axis=1, keepdims=True)
        elif ch == 2 and x.shape[1] == 1:
            x = np.repeat(x, 2, axis=1)
        elif x.shape[1] > ch:
            x = x[:, :ch]
        x = np.ascontiguousarray(x, dtype=np.float32)
        x = self.resampler.process(x)
        if x.shape[0] == 0:
            return x
        if self.gain != 1.0:
            x = x * self.gain
        if self.hpf is not None:
            x = self.hpf.process(x)
        peaks = dsp.peak_levels(x)
        self.in_levels = peaks
        if peaks.size and float(np.max(peaks)) >= 0.99:     # clipping is about the input
            self.clip_until = time.monotonic() + 2.0
        return x

    def _mix(self, mic: np.ndarray | None, sys_: np.ndarray | None, n: int) -> np.ndarray:
        """Combine the sources as the layout says (an unticked source records silence)."""
        fmt = self.cfg.fmt
        if fmt.split:                                              # microphone left, computer audio right
            silent = np.zeros((n, 1), dtype=np.float32)
            left = mic if mic is not None else silent
            right = sys_ if sys_ is not None else silent
            return np.clip(np.hstack([left, right]), -1.0, 1.0)
        if mic is None and sys_ is None:
            return np.zeros((n, fmt.channels), dtype=np.float32)
        if sys_ is None:
            return mic
        if mic is None:
            return sys_
        return np.clip(mic + sys_, -1.0, 1.0)

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
        mic_det = mic_out = mic
        if self.clean is not None:
            # the chain runs on silence while the microphone is off, so its delay stays constant
            src = mic if mic is not None else np.zeros((n, self._src_ch()), dtype=np.float32)
            mic_det, mic_out = self.clean.process(src)
        if mic is not None:
            self.levels = dsp.peak_levels(mic_det)
        sys_ = self._sys_take(n)
        if sys_ is not None:
            sp = dsp.peak_levels(sys_)
            self.sys_levels = sp
            if sp.size and float(np.max(sp)) >= 0.99:
                self.sys_clip_until = time.monotonic() + 2.0
        sys_d = sys_
        if self._sys_delay is not None:                            # keep step with the cleaned microphone
            sys_d = self._sys_delay.process(sys_ if sys_ is not None else
                                            np.zeros((n, self._src_ch()), dtype=np.float32))
        x = self._mix(mic_det, sys_d, n)
        x_out = x if mic_out is mic_det else self._mix(mic_out, sys_d, n)
        start = self.in_pos
        if self._hist_out is not None:
            self._hist_out.push(x_out, start)
        if self._hist_raw is not None:
            raw = self._mix(mic, sys_, n)
            self._hist_raw.push(self._raw_delay.process(raw) if self._raw_delay is not None else raw, start)
        self.mix_levels = dsp.peak_levels(x)
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
        # writer falling behind
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
                data, s = op[1], op[2]
                out = self._hist_out.get(s, data.shape[0]) if self._hist_out is not None else data
                raw = None
                if self._keep_raw:
                    raw = self._hist_raw.get(s, data.shape[0]) if self._hist_raw is not None else data
                self._write(out, s, raw)
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

    def _write(self, x: np.ndarray, sample: int, raw: np.ndarray | None = None) -> None:
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
        file_frame, path = self.writer.segment_start(), self.writer.path
        try:
            self.writer.write(x)
        except OSError as exc:
            self._write_failed(exc, sample)
            if self.writer is not None:
                try:
                    self.writer.write(x)
                except OSError:
                    pass
        if raw is not None and self.orig_writer is not None:
            try:
                self.orig_writer.write(raw)
            except OSError as exc:
                self._drop_original(exc)
        tap = self.tap
        if tap is not None and self.writer is not None and self.writer.path == path:
            try:
                tap(x, file_frame, self.cfg.fmt.rate, self._sample_time(sample), path)
            except Exception:
                log.exception("live transcript hand-over failed")
                self.tap = None

    # -------------------------------------------------------------- files
    def _folder(self) -> str | None:
        return self.folder_override

    def _in_folder(self, folder: str) -> bool:
        return bool(self.writer) and os.path.normcase(os.path.dirname(os.path.abspath(self.writer.path))) == \
            os.path.normcase(os.path.abspath(folder))

    def _open_file(self, path: str, append: bool = False) -> None:
        if not self.cfg.save_audio:
            append = False
        w = create_writer(path, self.cfg.fmt, append=append) if self.cfg.save_audio else NullWriter(path, self.cfg.fmt)
        self.writer = w
        self.segments = list(w.existing_segments)
        if append and w.frames > 0 and not self.segments:
            self.segments = [Segment(0)]
        self.seg_open = False
        now = time.time() if self.t0_wall is None else self._sample_time(self.in_pos)
        self.file_t0 = now
        self.file_key = self._time_key(now)
        self.file_started_iso = iso_now(datetime.fromtimestamp(now))
        self.journal = Journal(path) if self.cfg.save_audio else None
        if self._keep_raw:
            self._open_original(path, append, w.frames)
        self._write_journal()
        self._emit("file_opened", path, self.cfg.fmt, list(self.segments), w.frames, append)

    def _open_original(self, path: str, append: bool, main_frames: int) -> None:
        """Open the untouched copy next to the recording."""
        try:
            if append:
                op = original_path(path)
                if not os.path.exists(paths.longpath(op)):
                    self._emit("info", f"No untouched copy is kept for {os.path.basename(path)}: it was "
                                       "recorded without one.")
                    return
                ow = create_writer(op, self.cfg.fmt, append=True)
                if ow.frames != main_frames:          # the two files no longer line up
                    ow.abort()
                    self._emit("info", f"The untouched copy of {os.path.basename(path)} no longer matches "
                                       "it, so no more is added to it.")
                    return
            else:
                ow = None
                for k in range(1, 100):
                    try:
                        ow = create_writer(original_path(path, k), self.cfg.fmt)
                        break
                    except FileExistsError:
                        continue
                if ow is None:
                    raise OSError("no free file name for the untouched copy")
        except (OSError, WriterError) as exc:
            log.warning("untouched copy not created for %s: %s", path, exc)
            self._emit("info", f"The untouched copy could not be created ({exc}); the cleaned recording "
                               "continues.")
            return
        self.orig_writer = ow
        self.orig_journal = Journal(ow.path)

    def _drop_original(self, exc: Exception) -> None:
        """Writing the untouched copy failed: close it; the cleaned recording goes on."""
        ow = self.orig_writer
        log.error("untouched copy %s failed: %s", ow.path if ow else "", exc)
        self._finalize_original()
        self._emit("info", f"The untouched copy stopped because it could not be written ({exc}); "
                           "the cleaned recording continues.")

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
                continue                       # Another writer took the name
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
        if self.cfg.fmt.split:
            t["channels"] = SPLIT_ROLES
        else:
            t.pop("channels", None)
        if self.clean is not None:
            t["processing"] = self.clean.describe()       # 1.0.4
        return t

    def _orig_tags(self) -> dict:
        """Tags of the untouched copy: the recording's, marked as the unprocessed original."""
        t = self._tags()
        t.pop("processing", None)
        ow = self.orig_writer
        if ow is not None:
            t["title"] = os.path.splitext(os.path.basename(ow.path))[0]
        main = os.path.basename(self.writer.path) if self.writer else ""
        note = f"Untouched copy of {main}: no noise suppression or automatic level"
        t["comment"] = f"{t['comment']} | {note}" if t.get("comment") else note
        return t

    def _finalize_original(self) -> None:
        ow, oj = self.orig_writer, self.orig_journal
        if ow is None:
            return
        segs = [s for s in self.segments if s.start < ow.frames] or [Segment(0)]
        try:
            ow.finalize(segs, self._orig_tags())
            if self.cfg.sidecar:
                metadata.write_sidecar(ow.path, segs, self.cfg.fmt.rate, self._orig_tags())
            if oj:
                oj.close()
            self._emit("orig_closed", ow.path)
        except Exception:
            log.exception("finalise failed for the untouched copy %s", ow.path)
            ow.abort()                     # its journal stays: it is offered for recovery
        self.orig_writer, self.orig_journal = None, None

    def _finalize_file(self) -> None:
        w, j = self.writer, self.journal
        if w is None:
            return
        self._finalize_original()
        end = w.segment_start()
        segs = list(self.segments)
        while len(segs) > 1 and segs[-1].start >= end:     # drop empty trailing segments
            segs.pop()
        segs = segs or [Segment(0)]
        frames = w.frames
        if self.tap_close is not None:
            try:
                self.tap_close(w.path)
            except Exception:
                log.exception("live transcript close failed")
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
        """Finish what we can, continue in the fallback folder."""
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
        """Close this file and continue seamlessly in a new one."""
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
        ow = self.orig_writer
        if self.orig_journal is not None and ow is not None:
            self.orig_journal.write({
                "version": 1, "path": ow.path, "format": self.cfg.fmt.to_json(),
                "segments": [s.to_json() for s in self.segments if s.start < ow.frames] or [Segment(0).to_json()],
                "frames": ow.frames, "tags": self._orig_tags(), "writer": ow.journal_state(),
                "updated": time.time(),
            })

    def _periodic(self) -> None:
        if self.writer is None:
            return
        now = time.monotonic()
        try:
            if now - self.last_flush >= self.cfg.flush_s:
                self.writer.flush()
                self._flush_original()
                self._write_journal()
                self.last_flush = now
            if now - self.last_fsync >= self.cfg.fsync_s:
                self.writer.fsync()
                if self.orig_writer is not None:
                    try:
                        self.orig_writer.fsync()
                    except OSError as exc:
                        self._drop_original(exc)
                self.last_fsync = now
        except OSError as exc:
            self._write_failed(exc, self.in_pos)

    def _flush_original(self) -> None:
        if self.orig_writer is not None:
            try:
                self.orig_writer.flush()
            except OSError as exc:
                self._drop_original(exc)

    def _idle(self) -> None:
        """No audio for 50 ms: keep flushing, detect a lost device."""
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
            "original": self.orig_writer.path if self.orig_writer is not None else None,
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
