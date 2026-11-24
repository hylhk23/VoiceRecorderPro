"""Microphone clean-up (v1.0.4): noise suppression and automatic level.

Noise suppression uses RNNoise (Jean-Marc Valin, Xiph.Org; BSD-3-Clause), built from the
official 0.2 release and loaded with ctypes, so no Python package is needed. RNNoise works
on 48 kHz mono frames of 480 samples; other file rates are resampled to 48 kHz and back,
and each microphone channel gets its own RNNoise state.

The automatic leveller is written here in NumPy. Every 10 ms it measures the level and
keeps track of the background-noise floor. A frame counts as speech after 30 ms in a row
at least 10 dB above the floor. The speech level is the average power of the last second
of speech frames: a louder voice raises it at once, and a new, quieter talker replaces the
previous one within about a second of speech. The gain brings that level to the target,
within limits, and is held during pauses so background noise is not pumped up between
words. A peak limiter keeps the result below -1 dBFS.

Both processors return exactly as many frames as they are given, so the recorder's sample
bookkeeping (segments, Vox, the untouched copy) is unchanged. Noise suppression delays the
microphone by about DENOISE_DELAY_S plus the resamplers' own delay; the leveller adds none.

Why 0.12 s: the streaming resampler (soxr) hands back audio in bursts, and RNNoise only
takes whole 10 ms frames, so the output buffer must hold a cushion. Measured on silence
for every file rate from 8 to 96 kHz and blocks of 64 to 8192 frames, after a 0.2 s
warm-up, the largest shortfall was 103 ms (8 kHz); at the default 16 kHz it was 20 ms.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import logging
import math
import os
import sys
from collections import deque
from pathlib import Path

import numpy as np

from vrp.core import dsp

log = logging.getLogger(__name__)

RNN_RATE = 48000
RNN_FRAME = 480
RNN_SCALE = 32768.0           # RNNoise expects 16-bit sample values as floats
DENOISE_DELAY_S = 0.12        # output primed with this much silence (see Denoiser)
WARMUP_S = 0.2                # resamplers are run on silence first so they deliver at once

_lib = None
_lib_error: str | None = None
_P = ctypes.POINTER(ctypes.c_float)


# ============================================================== RNNoise library
def _candidates() -> list[Path]:
    here = Path(__file__).resolve().parent.parent / "lib"          # vrp/lib
    out = []
    env = os.environ.get("VRP_RNNOISE")
    if env:
        out.append(Path(env))
    if sys.platform == "win32":
        out.append(here / "rnnoise.dll")
    else:
        out.append(here / "librnnoise.so")
        found = ctypes.util.find_library("rnnoise")
        if found:
            out.append(Path(found))
    return out


def load_rnnoise():
    """The RNNoise library (cached), or raise OSError with a reason a user can read."""
    global _lib, _lib_error
    if _lib is not None:
        return _lib
    if _lib_error is not None:
        raise OSError(_lib_error)
    reasons = []
    for path in _candidates():
        if not path.exists() and os.sep in str(path):
            reasons.append(f"{path.name} not found")
            continue
        try:
            lib = ctypes.CDLL(str(path))
            lib.rnnoise_create.restype = ctypes.c_void_p
            lib.rnnoise_create.argtypes = [ctypes.c_void_p]
            lib.rnnoise_init.restype = ctypes.c_int
            lib.rnnoise_init.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            lib.rnnoise_destroy.restype = None
            lib.rnnoise_destroy.argtypes = [ctypes.c_void_p]
            lib.rnnoise_process_frame.restype = ctypes.c_float
            lib.rnnoise_process_frame.argtypes = [ctypes.c_void_p, _P, _P]
            lib.rnnoise_get_frame_size.restype = ctypes.c_int
            if lib.rnnoise_get_frame_size() != RNN_FRAME:
                raise OSError("unexpected RNNoise frame size")
            _lib = lib
            log.info("noise suppression: RNNoise loaded from %s", path)
            return lib
        except (OSError, AttributeError) as exc:
            reasons.append(f"{path.name}: {exc}")
    _lib_error = "the RNNoise library could not be loaded (" + "; ".join(reasons or ["not found"]) + ")"
    log.warning("noise suppression unavailable: %s", _lib_error)
    raise OSError(_lib_error)


def denoise_support() -> tuple[bool, str]:
    """(available, reason) for noise suppression on this PC."""
    try:
        load_rnnoise()
        return True, ""
    except OSError as exc:
        return False, str(exc)


def _reset_cache() -> None:          # tests
    global _lib, _lib_error
    _lib, _lib_error = None, None


# ============================================================== noise suppression
class Denoiser:
    """RNNoise on (frames x channels) float32 blocks at any rate; output length == input length."""

    def __init__(self, rate: int, channels: int):
        self.lib = load_rnnoise()
        self.rate, self.channels = int(rate), int(channels)
        self.delay = int(round(self.rate * DENOISE_DELAY_S))
        self._states = [self.lib.rnnoise_create(None) for _ in range(self.channels)]
        if not all(self._states):
            self.close()
            raise OSError("RNNoise could not allocate its state")
        self.underruns = 0
        self.speech_prob = 0.0            # RNNoise's voice-activity estimate for the last frame
        self.reset()
        # total delay in frames: the cushion, both resamplers and RNNoise's own 20 ms
        # (two 10 ms frames; measured by cross-correlating speech in and out at 8, 16, 44.1
        # and 48 kHz, which matched this sum to the sample)
        self.total_delay = self.delay + self._resampler_delay() + 2 * (self.rate // 100)

    def _resampler_delay(self) -> int:
        """Delay of the up/down resampler pair (after warm-up), measured with an impulse."""
        if self.rate == RNN_RATE:
            return 0
        up, down = dsp.Resampler(self.rate, RNN_RATE, 1), dsp.Resampler(RNN_RATE, self.rate, 1)
        up.process(np.zeros((int(self.rate * WARMUP_S), 1), dtype=np.float32))
        down.process(np.zeros((int(RNN_RATE * WARMUP_S), 1), dtype=np.float32))
        x = np.zeros((int(self.rate * 0.3), 1), dtype=np.float32)
        x[0, 0] = 1.0
        y = down.process(up.process(x))
        return int(np.argmax(np.abs(y[:, 0]))) if y.size else 0

    def reset(self) -> None:
        for st in self._states:
            self.lib.rnnoise_init(st, None)
        same = self.rate == RNN_RATE
        self._up = None if same else dsp.Resampler(self.rate, RNN_RATE, self.channels)
        self._down = None if same else dsp.Resampler(RNN_RATE, self.rate, self.channels)
        if not same:          # fill the resamplers' delay lines with silence (output discarded)
            self._up.process(np.zeros((int(self.rate * WARMUP_S), self.channels), dtype=np.float32))
            self._down.process(np.zeros((int(RNN_RATE * WARMUP_S), self.channels), dtype=np.float32))
        self._in = np.zeros((0, self.channels), dtype=np.float32)
        self._out = np.zeros((self.delay, self.channels), dtype=np.float32)

    def close(self) -> None:
        for st in getattr(self, "_states", []):
            if st:
                self.lib.rnnoise_destroy(st)
        self._states = []

    def __del__(self):                # pragma: no cover - depends on garbage collection
        try:
            self.close()
        except Exception:
            pass

    def _run_frames(self, x48: np.ndarray) -> np.ndarray:
        k = x48.shape[0]
        out = np.empty_like(x48)
        probs = []
        for c, st in enumerate(self._states):
            src = np.ascontiguousarray(x48[:, c] * RNN_SCALE, dtype=np.float32)
            dst = np.empty(k, dtype=np.float32)
            a, b = src.ctypes.data, dst.ctypes.data
            for i in range(0, k, RNN_FRAME):
                probs.append(self.lib.rnnoise_process_frame(st, ctypes.cast(b + 4 * i, _P),
                                                            ctypes.cast(a + 4 * i, _P)))
            out[:, c] = dst / RNN_SCALE
        if probs:
            self.speech_prob = float(max(probs[-self.channels:]))
        return out

    def process(self, x: np.ndarray) -> np.ndarray:
        n = x.shape[0]
        if n == 0:
            return x
        x = np.ascontiguousarray(x, dtype=np.float32)
        up = self._up.process(x) if self._up is not None else x
        buf = np.concatenate([self._in, up]) if self._in.shape[0] else up
        k = (buf.shape[0] // RNN_FRAME) * RNN_FRAME
        if k:
            clean = self._run_frames(buf[:k])
            down = self._down.process(clean) if self._down is not None else clean
            if down.shape[0]:
                self._out = np.concatenate([self._out, down.astype(np.float32, copy=False)])
        self._in = buf[k:]
        have = self._out.shape[0]
        if have >= n:
            y, self._out = self._out[:n], self._out[n:]
            return y
        # not enough (not seen in the measurements above): pad with silence and rebuild half
        # the cushion, so one short gap is heard instead of a run of them
        self.underruns += 1
        if self.underruns == 1:
            log.warning("noise suppression fell behind by %d frames", n - have)
        y = np.concatenate([np.zeros((n - have, self.channels), dtype=np.float32), self._out])
        self._out = np.zeros((self.delay // 2, self.channels), dtype=np.float32)
        return y


# ============================================================== automatic level
class Leveller:
    """Automatic level for speech: near and far voices come out at about the same loudness."""

    TARGET_DB = -20.0         # level of active speech the leveller aims for (dBFS, 10 ms frames)
    MAX_GAIN_DB = 24.0        # at most this much boost (a quiet, distant speaker)
    MIN_GAIN_DB = -10.0       # at most this much cut (someone very close to the microphone)
    SPEECH_MIN_DB = -60.0     # quieter frames are never treated as speech
    SPEECH_OVER_FLOOR_DB = 10.0
    SPEECH_RUN = 3            # frames (30 ms) in a row above the floor before it counts as speech
    FLOOR_RISE_DB_S = 2.0     # the noise-floor estimate creeps up this fast; it drops at once
    SPEECH_WINDOW = 100       # speech frames (1 s of speech) averaged for the speech level
    CEILING = 10 ** (-1.0 / 20)   # limiter: peaks stay below -1 dBFS
    LIMIT_RELEASE_DB_S = 8.0

    def __init__(self, rate: int, channels: int):
        self.rate, self.channels = int(rate), int(channels)
        self.frame = max(1, self.rate // 100)
        self.reset()

    def reset(self) -> None:
        self.floor_db: float | None = None
        self.speech_db: float | None = None
        self._pow = np.zeros(self.SPEECH_WINDOW, dtype=np.float64)   # ring of speech-frame powers
        self._pow_n = 0
        self.gain_db = 0.0
        self._run = 0             # frames in a row above the noise floor
        self._g = 1.0             # linear gain at the end of the last frame
        self._lim = 1.0

    def _frame(self, seg: np.ndarray) -> np.ndarray:
        m = seg.shape[0]
        dt = m / self.rate
        lvl = 10.0 * math.log10(float(np.mean(seg.astype(np.float64) ** 2)) + 1e-12)
        if self.floor_db is None or lvl < self.floor_db:
            self.floor_db = lvl
        else:
            self.floor_db = min(lvl, self.floor_db + self.FLOOR_RISE_DB_S * dt)
        loud = lvl > max(self.floor_db + self.SPEECH_OVER_FLOOR_DB, self.SPEECH_MIN_DB)
        self._run = self._run + 1 if loud else 0
        speech = self._run >= self.SPEECH_RUN       # a short click or noise spike is not speech
        if speech:
            # average power, so loud syllables count more than their quiet edges and the
            # estimate matches how loud the voice sounds
            self._pow[self._pow_n % self.SPEECH_WINDOW] = 10.0 ** (lvl / 10.0)
            self._pow_n += 1
            k = min(self._pow_n, self.SPEECH_WINDOW)
            self.speech_db = 10.0 * math.log10(float(self._pow[:k].mean()) + 1e-15)
        if self.speech_db is not None:
            self.gain_db = min(self.MAX_GAIN_DB, max(self.MIN_GAIN_DB, self.TARGET_DB - self.speech_db))
        g_new = 10.0 ** (self.gain_db / 20.0)
        ramp = np.linspace(self._g, g_new, m + 1, dtype=np.float64)[1:]
        self._g = g_new
        y = seg * ramp[:, None]
        peak = float(np.max(np.abs(y))) if y.size else 0.0
        lim = min(1.0, self._lim * 10.0 ** (self.LIMIT_RELEASE_DB_S * dt / 20.0))
        if peak * lim > self.CEILING:
            lim = self.CEILING / peak
        self._lim = lim
        return (y * lim).astype(np.float32)

    def process(self, x: np.ndarray) -> np.ndarray:
        n = x.shape[0]
        if n == 0:
            return x
        out = np.empty((n, x.shape[1]), dtype=np.float32)
        f = self.frame
        for i in range(0, n, f):
            out[i:i + f] = self._frame(x[i:i + f])
        return np.clip(out, -1.0, 1.0)


# ============================================================== chain
class CleanChain:
    """Noise suppression and/or automatic level on the microphone signal.

    process() returns (detect, out): `detect` is the signal after noise suppression only
    (Vox listens to it, so the threshold keeps its meaning), `out` is what is recorded.
    """

    def __init__(self, rate: int, channels: int, denoise: bool, level: bool):
        self.denoise: Denoiser | None = None
        self.error = ""
        if denoise:
            try:
                self.denoise = Denoiser(rate, channels)
            except OSError as exc:
                self.error = str(exc)
        self.level = Leveller(rate, channels) if level else None

    @property
    def active(self) -> bool:
        return self.denoise is not None or self.level is not None

    @property
    def delay(self) -> int:
        """Frames by which the cleaned microphone lags the input (0 without noise suppression)."""
        return self.denoise.total_delay if self.denoise is not None else 0

    def describe(self) -> str:
        parts = []
        if self.denoise is not None:
            parts.append("noise suppression (RNNoise)")
        if self.level is not None:
            parts.append(f"automatic level (target {Leveller.TARGET_DB:.0f} dBFS)")
        return "; ".join(parts)

    def reset(self) -> None:
        if self.denoise is not None:
            self.denoise.reset()
        if self.level is not None:
            self.level.reset()

    def process(self, x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        det = self.denoise.process(x) if self.denoise is not None else x
        out = self.level.process(det) if self.level is not None else det
        return det, out

    def close(self) -> None:
        if self.denoise is not None:
            self.denoise.close()
            self.denoise = None


# ============================================================== delay line
class DelayLine:
    """Delays a stream by a fixed number of frames (computer audio and the untouched copy
    are delayed as much as noise suppression delays the microphone, so all stay in step)."""

    def __init__(self, frames: int, channels: int):
        self.frames = max(0, int(frames))
        self.channels = channels
        self.reset()

    def reset(self) -> None:
        self._buf = np.zeros((self.frames, self.channels), dtype=np.float32)

    def process(self, x: np.ndarray) -> np.ndarray:
        if self.frames == 0 or x.shape[0] == 0:
            return x
        buf = np.concatenate([self._buf, x.astype(np.float32, copy=False)])
        n = x.shape[0]
        y, self._buf = buf[:n], buf[n:]
        return y


# ============================================================== sample history
class History:
    """The last `seconds` of a stream, addressed by absolute sample number.

    Vox decides on one signal (`detect`) and hands out write operations by sample number;
    the recorder takes the same sample range of the recorded signal and of the untouched
    copy from these histories, so all files stay sample-aligned.
    """

    def __init__(self, rate: int, channels: int, seconds: float = 15.0):
        self.cap = max(1, int(rate * seconds))
        self.channels = channels
        self.blocks: deque = deque()          # (start, array)
        self.n = 0
        self.missed = 0

    @property
    def end(self) -> int | None:
        if not self.blocks:
            return None
        s, a = self.blocks[-1]
        return s + a.shape[0]

    def push(self, x: np.ndarray, start: int) -> None:
        if self.blocks and start != self.end:
            self.blocks.clear()
            self.n = 0
        self.blocks.append((start, x))
        self.n += x.shape[0]
        while len(self.blocks) > 1 and self.n - self.blocks[0][1].shape[0] >= self.cap:
            _, old = self.blocks.popleft()
            self.n -= old.shape[0]

    def get(self, start: int, n: int) -> np.ndarray:
        out = np.zeros((n, self.channels), dtype=np.float32)
        end = start + n
        got = 0
        for s, a in self.blocks:
            e = s + a.shape[0]
            if e <= start or s >= end:
                continue
            lo, hi = max(s, start), min(e, end)
            out[lo - start:hi - start] = a[lo - s:hi - s]
            got += hi - lo
        if got < n:
            self.missed += n - got
            if self.missed == n - got:
                log.warning("history miss: %d of %d frames from %d were no longer held", n - got, n, start)
        return out
