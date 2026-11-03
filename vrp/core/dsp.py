"""Sample conversion, levels, gain, high-pass filter, resampling and G.711."""
from __future__ import annotations

import math

import numpy as np

try:  # optional, high quality streaming resampler
    import soxr
except Exception:  # pragma: no cover
    soxr = None


def peak_levels(x: np.ndarray) -> np.ndarray:
    """Peak absolute value per channel (0..1+)."""
    if x.size == 0:
        return np.zeros(x.shape[1] if x.ndim == 2 else 1, dtype=np.float32)
    return np.max(np.abs(x), axis=0)


def level_to_percent(peak: float) -> float:
    return float(min(100.0, max(0.0, peak * 100.0)))


def percent_to_dbfs(pct: float) -> float:
    if pct <= 0:
        return -120.0
    return 20.0 * math.log10(pct / 100.0)


def dbfs_to_percent(db: float) -> float:
    return 100.0 * (10 ** (db / 20.0))


def db_to_gain(db: float) -> float:
    return 10 ** (db / 20.0)


def to_channels(x: np.ndarray, out_ch: int) -> np.ndarray:
    """Convert interleaved frames (n x ch) to mono or stereo.

    Surround output (computer audio from 5.1/7.1 speakers) is folded down with the
    centre channel at -3 dB into both sides, so dialogue is not lost.
    """
    if x.ndim == 1:
        x = x[:, None]
    ch = x.shape[1]
    if ch == 1:
        st = np.repeat(x, 2, axis=1) if out_ch == 2 else x
        return np.ascontiguousarray(st, dtype=np.float32)
    if ch == 2:
        st = x
    else:
        k = 0.7071
        left = x[:, 0].astype(np.float32).copy()
        right = x[:, 1].astype(np.float32).copy()
        if ch == 4:                                  # FL FR BL BR
            left += k * x[:, 2]
            right += k * x[:, 3]
        else:                                        # FL FR C [LFE] [BL BR] [SL SR]
            left += k * x[:, 2]
            right += k * x[:, 2]
            for i in range(4, ch - 1, 2):
                left += k * x[:, i]
                right += k * x[:, i + 1]
        st = np.stack([left, right], axis=1)
    if out_ch == 1:
        return np.ascontiguousarray(st[:, :2].mean(axis=1, keepdims=True), dtype=np.float32)
    return np.ascontiguousarray(st[:, :2], dtype=np.float32)


class HighPass:
    """80 Hz high-pass filter with continuous state (FR-REC-010).

    Two cascaded one-pole high-pass sections (12 dB/octave below the cut-off),
    computed with NumPy only: the recursion y[n] = r*y[n-1] + x[n] - x[n-1] is
    solved in closed form over short sub-blocks, so there is no per-sample
    Python loop and no SciPy dependency.
    """

    SUB = 64

    def __init__(self, rate: int, channels: int, cutoff: float = 80.0):
        self.available = True
        self.channels = channels
        self.r = math.exp(-2.0 * math.pi * cutoff / rate)
        k = np.arange(self.SUB, dtype=np.float64)
        self._rpow = self.r ** k                 # r^k
        self._rinv = self.r ** (-k)              # r^-k (at most ~55 for 8 kHz)
        self._state = [(np.zeros(channels), np.zeros(channels)) for _ in range(2)]   # (x_prev, y_prev)

    def _section(self, x: np.ndarray, idx: int) -> np.ndarray:
        x_prev, y_prev = self._state[idx]
        out = np.empty_like(x)
        r = self.r
        for a in range(0, x.shape[0], self.SUB):
            blk = x[a:a + self.SUB]
            m = blk.shape[0]
            d = np.diff(blk, axis=0, prepend=x_prev[None, :])
            acc = np.cumsum(d * self._rinv[:m, None], axis=0)
            y = self._rpow[:m, None] * (r * y_prev[None, :] + acc)
            out[a:a + m] = y
            x_prev, y_prev = blk[-1], y[-1]
        self._state[idx] = (x_prev.astype(np.float64), y_prev.astype(np.float64))
        return out

    def process(self, x: np.ndarray) -> np.ndarray:
        if x.size == 0:
            return x
        y = self._section(x.astype(np.float64), 0)
        y = self._section(y, 1)
        return y.astype(np.float32)


class Resampler:
    """Streaming resampler: soxr when installed, linear interpolation otherwise."""

    def __init__(self, in_rate: int, out_rate: int, channels: int):
        self.in_rate, self.out_rate, self.channels = in_rate, out_rate, channels
        self.passthrough = in_rate == out_rate
        self._soxr = None
        if not self.passthrough and soxr is not None:
            self._soxr = soxr.ResampleStream(in_rate, out_rate, channels, dtype="float32", quality="HQ")
        self._pos = 0.0          # fractional read position in the virtual input stream
        self._tail = np.zeros((0, channels), dtype=np.float32)

    def process(self, x: np.ndarray, last: bool = False) -> np.ndarray:
        if self.passthrough:
            return x
        if self._soxr is not None:
            y = self._soxr.resample_chunk(np.ascontiguousarray(x, dtype=np.float32), last=last)
            return y.reshape(-1, self.channels).astype(np.float32, copy=False)
        buf = np.concatenate([self._tail, x]) if self._tail.size else x
        step = self.in_rate / self.out_rate
        if buf.shape[0] < 2:
            self._tail = buf
            return np.zeros((0, self.channels), dtype=np.float32)
        positions = np.arange(self._pos, buf.shape[0] - 1, step)
        idx = positions.astype(np.int64)
        frac = (positions - idx)[:, None].astype(np.float32)
        y = buf[idx] * (1 - frac) + buf[idx + 1] * frac
        nxt = self._pos + len(positions) * step
        keep_from = int(nxt)
        self._tail = buf[keep_from:]
        self._pos = nxt - keep_from
        return y.astype(np.float32)


# --------------------------------------------------------------- conversions
def to_int16(x: np.ndarray) -> np.ndarray:
    return np.clip(np.rint(x * 32767.0), -32768, 32767).astype("<i2")


def to_pcm_bytes(x: np.ndarray, subtype: str) -> bytes:
    """Interleaved little-endian bytes for a WAV data chunk."""
    if subtype == "PCM_16":
        return to_int16(x).tobytes()
    if subtype == "PCM_U8":
        return np.clip(np.rint(x * 128.0) + 128, 0, 255).astype(np.uint8).tobytes()
    if subtype == "PCM_24":
        i = np.clip(np.rint(x * 8388607.0), -8388608, 8388607).astype("<i4")
        return i.reshape(-1).view(np.uint8).reshape(-1, 4)[:, :3].tobytes()
    if subtype == "FLOAT":
        return np.ascontiguousarray(x, dtype="<f4").tobytes()
    if subtype == "ULAW":
        return lin2ulaw(to_int16(x)).tobytes()
    if subtype == "ALAW":
        return lin2alaw(to_int16(x)).tobytes()
    raise ValueError(f"unsupported subtype {subtype}")


def from_pcm_bytes(data: bytes, subtype: str, channels: int) -> np.ndarray:
    """Inverse of to_pcm_bytes (used for tests and WAV edits)."""
    if subtype == "PCM_16":
        x = np.frombuffer(data, "<i2").astype(np.float32) / 32768.0
    elif subtype == "PCM_U8":
        x = (np.frombuffer(data, np.uint8).astype(np.float32) - 128.0) / 128.0
    elif subtype == "PCM_24":
        b = np.frombuffer(data, np.uint8).reshape(-1, 3)
        i = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) | (b[:, 2].astype(np.int32) << 16))
        i = np.where(i >= 1 << 23, i - (1 << 24), i)
        x = i.astype(np.float32) / 8388608.0
    elif subtype == "FLOAT":
        x = np.frombuffer(data, "<f4").astype(np.float32)
    elif subtype == "ULAW":
        x = ulaw2lin(np.frombuffer(data, np.uint8)).astype(np.float32) / 32768.0
    elif subtype == "ALAW":
        x = alaw2lin(np.frombuffer(data, np.uint8)).astype(np.float32) / 32768.0
    else:
        raise ValueError(subtype)
    return x.reshape(-1, channels)


SUBTYPE_BITS = {"PCM_U8": 8, "PCM_16": 16, "PCM_24": 24, "FLOAT": 32, "ULAW": 8, "ALAW": 8}


# ----------------------------------------------------------------- G.711
_ULAW_BIAS = 0x84
_ULAW_CLIP = 32635
_SEG_END = np.array([0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF, 0x3FFF, 0x7FFF])
_ALAW_SEG_END = np.array([0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF])


def lin2ulaw(pcm: np.ndarray) -> np.ndarray:
    """16-bit linear PCM to 8-bit mu-law (ITU-T G.711)."""
    x = pcm.astype(np.int32)
    sign = np.where(x < 0, 0x80, 0x00)
    mag = np.minimum(np.abs(x), _ULAW_CLIP) + _ULAW_BIAS
    seg = np.searchsorted(_SEG_END, mag, side="left")
    seg = np.minimum(seg, 7)
    mant = (mag >> (seg + 3)) & 0x0F
    u = (seg << 4) | mant
    return ((u | sign) ^ 0xFF).astype(np.uint8)


def ulaw2lin(u: np.ndarray) -> np.ndarray:
    u = (~u.astype(np.int32)) & 0xFF
    t = ((u & 0x0F) << 3) + _ULAW_BIAS
    t = t << ((u & 0x70) >> 4)
    return np.where(u & 0x80, _ULAW_BIAS - t, t - _ULAW_BIAS).astype(np.int16)


def lin2alaw(pcm: np.ndarray) -> np.ndarray:
    """16-bit linear PCM to 8-bit A-law (ITU-T G.711)."""
    x = pcm.astype(np.int32) >> 3
    mask = np.where(x >= 0, 0xD5, 0x55)
    x = np.where(x >= 0, x, -x - 1)
    seg = np.searchsorted(_ALAW_SEG_END, x, side="left")
    aval = np.where(seg >= 8, 0x7F,
                    (np.minimum(seg, 7) << 4) | np.where(seg < 2, (x >> 1) & 0x0F,
                                                         (x >> np.minimum(seg, 7)) & 0x0F))
    return (aval ^ mask).astype(np.uint8)


def alaw2lin(a: np.ndarray) -> np.ndarray:
    a = a.astype(np.int32) ^ 0x55
    t = (a & 0x0F) << 4
    seg = (a & 0x70) >> 4
    t = np.where(seg == 0, t + 8, np.where(seg == 1, t + 0x108, (t + 0x108) << np.maximum(seg - 1, 0)))
    return np.where(a & 0x80, t, -t).astype(np.int16)
