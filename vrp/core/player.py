"""Playback (Section 9): decoder thread + output callback.

The decoder thread reads the file through libsndfile (WAV, MP3, FLAC, ...),
applies time-stretch, segment gaps, selection limits and audible scanning, and
queues chunks. The output callback only pops chunks, applies the volume and
tracks the source position, so it never waits on the disk.
"""
from __future__ import annotations

import bisect
import collections
import logging
import threading
import time

import numpy as np

from vrp import paths
from vrp.core import dsp
from vrp.core.audio_backend import AudioError, BaseBackend, DeviceInfo
from vrp.core.tsm import Wsola

log = logging.getLogger(__name__)

CHUNK = 2048
BUFFER_SECONDS = 0.35
SCAN_CHUNK_S = 0.08


class Player:
    def __init__(self, backend: BaseBackend):
        self.backend = backend
        self.sf = None
        self.path: str | None = None
        self.rate = 0
        self.channels = 0
        self.frames = 0
        self.stream = None
        self.device: DeviceInfo | None = None
        self.volume = 1.0
        self.speed = 1.0
        self.out_rate = 0
        self.out_channels = 2
        self.levels = np.zeros(2, dtype=np.float32)
        self._q: collections.deque = collections.deque()
        self._q_frames = 0
        self._gen = 0
        self._cur = None
        self._cur_idx = 0
        self._pos = 0.0            # source frame being heard
        self._read_pos = 0         # next source frame to decode
        self.playing = False
        self.finished = False
        self.sel: tuple[int, int] | None = None
        self.loop = False
        self.gap_frames = 0
        self.seg_starts: list[int] = []
        self.scan_dir = 0
        self.scan_rate = 4.0
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._quit = False
        self._thread: threading.Thread | None = None
        self.underruns = 0
        self.resampler = None
        self.tsm = Wsola(16000, 1)
        self._seek_preroll = 0

    # ------------------------------------------------------------------ file
    def open(self, path: str) -> None:
        import soundfile as sf
        self.close()
        self.sf = sf.SoundFile(paths.longpath(path))
        self.path = path
        self.rate = self.sf.samplerate
        self.channels = self.sf.channels
        self.frames = self.sf.frames
        # MP3: decode a few frames before a seek target so the bit reservoir is filled
        self._seek_preroll = 4 * 1152 if (self.sf.format or "").upper() in ("MP3", "MPEG") else 0
        self._pos = 0.0
        self._read_pos = 0
        self.finished = False
        self.resampler = None
        self.tsm = Wsola(self.rate, self.channels, self.speed)
        self._quit = False
        self._thread = threading.Thread(target=self._decode_loop, name="vrp-decoder", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._close_stream()
        self._quit = True
        self._wake.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._thread = None
        if self.sf is not None:
            try:
                self.sf.close()
            except Exception:
                pass
        self.sf = None
        self.path = None
        self.playing = False

    @property
    def is_open(self) -> bool:
        return self.sf is not None

    # --------------------------------------------------------------- stream
    def set_device(self, dev: DeviceInfo | None) -> None:
        if dev is not None and self.device is not None and dev.index == self.device.index:
            return
        self.device = dev
        if self.stream is not None:
            was = self.playing
            self._close_stream()
            if was:
                self._ensure_stream()

    def _ensure_stream(self) -> None:
        if self.stream is not None:
            return
        if self.device is None:
            raise AudioError("No output device")
        ch = max(1, min(2, self.device.max_out or 2))
        rate = self.rate
        if not self.backend.check_output(self.device, rate, ch):
            rate = int(self.device.default_rate) or 48000
        self.out_rate, self.out_channels = rate, ch
        self.resampler = dsp.Resampler(self.rate, rate, self.channels) if rate != self.rate else None
        self.stream = self.backend.open_output(self.device, rate, ch, 1024, "high", self._callback)

    def _close_stream(self) -> None:
        s, self.stream = self.stream, None
        if s is not None:
            try:
                s.stop()
                s.close()
            except Exception:
                log.debug("output close failed", exc_info=True)

    # -------------------------------------------------------------- control
    @property
    def position(self) -> int:
        return int(self._pos)

    def play(self) -> None:
        if self.sf is None:
            return
        end = self._end_limit()
        if self._pos >= end - 1:
            self.seek(self.sel[0] if self.sel else 0)
        self.finished = False
        self._ensure_stream()
        self.playing = True
        self._wake.set()

    def pause(self) -> None:
        self.playing = False

    def stop(self) -> None:
        self.playing = False
        self.scan_dir = 0
        self._close_stream()

    def seek(self, frame: float) -> None:
        if self.sf is None:
            return
        frame = int(max(0, min(self.frames, frame)))
        with self._lock:
            self._gen += 1
            self._q.clear()
            self._q_frames = 0
            self._cur = None
            self._read_pos = frame
            self._pos = float(frame)
            self.finished = False
            self.tsm.reset()
            if self.resampler is not None:
                self.resampler = dsp.Resampler(self.rate, self.out_rate, self.channels)
        self._wake.set()

    def set_speed(self, pct: float) -> None:
        self.speed = max(0.5, min(2.0, pct / 100.0))
        self.tsm.set_speed(self.speed)

    def set_volume(self, pct: float) -> None:
        self.volume = max(0.0, min(1.0, pct / 100.0))

    def set_selection(self, sel: tuple[int, int] | None, loop: bool = False) -> None:
        self.sel = sel
        self.loop = loop and sel is not None

    def set_gap(self, ms: int, seg_starts: list[int]) -> None:
        self.gap_frames = int(self.rate * ms / 1000) if self.rate else 0
        self.seg_starts = sorted(seg_starts)

    def scan(self, direction: int, rate: float) -> None:
        """Audible scan while Rewind/FF is held (FR-PLAY-005)."""
        self.scan_rate = rate
        if direction != self.scan_dir:
            self.scan_dir = direction
            self.seek(self._pos)
        if direction:
            self.finished = False
            self._ensure_stream()
            self.playing = True
            self._wake.set()

    def end_scan(self) -> None:
        if self.scan_dir:
            self.scan_dir = 0
            self.seek(self._pos)

    def _end_limit(self) -> int:
        if self.sel:
            return min(self.frames, self.sel[1])
        return self.frames

    # ------------------------------------------------------------ decoder
    def _decode_loop(self) -> None:
        while not self._quit:
            if self.sf is None:
                break
            need = (self.playing and self._q_frames < BUFFER_SECONDS * max(1, self.out_rate or self.rate))
            if not need:
                self._wake.wait(0.02)
                self._wake.clear()
                continue
            try:
                self._decode_once()
            except Exception:
                log.exception("decoder error")
                self.playing = False
                self.finished = True

    def _push(self, data: np.ndarray, src_start: float, src_len: float, gen: int) -> None:
        if data.shape[0] == 0:
            return
        if self.resampler is not None:
            data = self.resampler.process(data)
            if data.shape[0] == 0:
                return
        with self._lock:
            if gen != self._gen:
                return
            self._q.append((data, src_start, src_len, gen))
            self._q_frames += data.shape[0]

    def _seek_to(self, pos: int) -> None:
        if self.sf.tell() == pos:
            return
        pre = min(pos, self._seek_preroll)
        self.sf.seek(pos - pre)
        if pre:
            self.sf.read(pre, dtype="float32", always_2d=True)

    def _decode_once(self) -> None:
        with self._lock:
            gen = self._gen
            pos = self._read_pos
        end = self._end_limit()
        if self.scan_dir:
            n = int(self.rate * SCAN_CHUNK_S)
            if (self.scan_dir > 0 and pos >= self.frames) or (self.scan_dir < 0 and pos <= 0):
                self._push(np.zeros((n, self.channels), np.float32), pos, 0, gen)
                return
            self._seek_to(pos)
            data = self.sf.read(n, dtype="float32", always_2d=True)
            self._push(data, pos, n * self.scan_rate * self.scan_dir, gen)
            nxt = pos + int(n * self.scan_rate) if self.scan_dir > 0 else pos - int(n * self.scan_rate)
            with self._lock:
                if gen == self._gen:
                    self._read_pos = max(0, min(self.frames, nxt))
            return
        if pos >= end:
            if self.loop and self.sel:
                self.seek(self.sel[0])
                return
            if self.speed != 1.0:
                tail = self.tsm.process(np.zeros((0, self.channels), np.float32), final=True)
                self._push(tail, pos, 0, gen)
            with self._lock:
                if gen == self._gen:
                    self._q.append((None, end, 0, gen))      # end-of-file marker
                    self._read_pos = end + 1
            return
        if pos > end:
            return
        n = min(CHUNK, end - pos)
        # stop at the next segment boundary to insert a playback gap (FR-PLAY-010)
        gap_here = False
        if self.gap_frames and self.seg_starts:
            i = bisect.bisect_right(self.seg_starts, pos)
            if i < len(self.seg_starts):
                b = self.seg_starts[i]
                if pos < b <= pos + n:
                    n = b - pos
                    gap_here = True
        self._seek_to(pos)
        data = self.sf.read(n, dtype="float32", always_2d=True)
        got = data.shape[0]
        if got == 0:
            with self._lock:
                if gen == self._gen:
                    self._q.append((None, pos, 0, gen))
                    self._read_pos = end + 1
            return
        if self.speed != 1.0:
            out = self.tsm.process(data)
            self._push(out, pos, got, gen)
        else:
            self._push(data, pos, got, gen)
        if gap_here:
            self._push(np.zeros((self.gap_frames, self.channels), np.float32), pos + got, 0, gen)
        with self._lock:
            if gen == self._gen:
                self._read_pos = pos + got

    # ------------------------------------------------------------ callback
    def _callback(self, outdata, frames, time_info, status):
        out = outdata
        out.fill(0)
        if not self.playing:
            self.levels = np.zeros(self.out_channels, dtype=np.float32)
            return
        filled = 0
        gen = self._gen
        while filled < frames:
            if self._cur is None:
                if not self._q:
                    self.underruns += 1
                    break
                item = self._q.popleft()
                if item[3] != gen:
                    continue
                if item[0] is None:       # end of file
                    self._pos = float(item[1])
                    self.playing = False
                    self.finished = True
                    break
                self._cur, self._cur_idx = item, 0
            data, src_start, src_len, _ = self._cur
            take = min(frames - filled, data.shape[0] - self._cur_idx)
            block = data[self._cur_idx:self._cur_idx + take]
            if block.shape[1] == 1 and self.out_channels == 2:
                out[filled:filled + take, 0] = block[:, 0]
                out[filled:filled + take, 1] = block[:, 0]
            else:
                c = min(self.out_channels, block.shape[1])
                out[filled:filled + take, :c] = block[:, :c]
            self._cur_idx += take
            self._q_frames -= take
            filled += take
            n = data.shape[0]
            if src_len:
                self._pos = src_start + self._cur_idx * src_len / n
            if self._cur_idx >= n:
                self._cur = None
        if self.volume != 1.0:
            out *= self.volume
        self.levels = np.max(np.abs(out), axis=0) if frames else self.levels
        self._wake.set()


def wait_until(pred, timeout: float = 2.0) -> bool:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.01)
    return pred()
