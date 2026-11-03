"""Streaming WSOLA time-stretch: playback speed 50-200% with pitch preserved (FR-PLAY-006)."""
from __future__ import annotations

import numpy as np


class Wsola:
    def __init__(self, rate: int, channels: int, speed: float = 1.0):
        self.rate = rate
        self.channels = channels
        n = int(rate * 0.040)
        self.N = n + (n % 2)                     # frame length, even
        self.Hs = self.N // 2                    # synthesis hop (50% overlap)
        self.tol = int(rate * 0.010)             # search +-10 ms
        k = np.arange(self.N)
        self.win = (0.5 - 0.5 * np.cos(2 * np.pi * k / self.N)).astype(np.float32)[:, None]
        self.dec = max(1, rate // 8000)          # search on a decimated signal to save CPU
        self.speed = speed
        self.reset()

    def reset(self) -> None:
        self.buf = np.zeros((0, self.channels), dtype=np.float32)
        self.base = 0                # absolute index of buf[0]
        self.ana = 0.0               # nominal analysis position (absolute)
        self.prev: int | None = None
        self.acc = np.zeros((self.N, self.channels), dtype=np.float32)
        self.consumed_in = 0         # input frames represented by output so far

    def set_speed(self, speed: float) -> None:
        self.speed = max(0.25, min(4.0, speed))

    def _mono(self, a: np.ndarray) -> np.ndarray:
        m = a.mean(axis=1) if a.shape[1] > 1 else a[:, 0]
        return m[:: self.dec]

    def process(self, x: np.ndarray, final: bool = False) -> np.ndarray:
        if x.size:
            self.buf = np.concatenate([self.buf, x.astype(np.float32, copy=False)])
        if final:
            self.buf = np.concatenate([self.buf, np.zeros((self.N + 2 * self.tol, self.channels),
                                                          dtype=np.float32)])
        out = []
        N, Hs, tol = self.N, self.Hs, self.tol
        end = self.base + self.buf.shape[0]
        while True:
            nominal = int(round(self.ana))
            if self.prev is None:
                cand = nominal
                if cand + N > end:
                    break
            else:
                natural = self.prev + Hs
                lo = max(self.base, nominal - tol)
                hi = nominal + tol
                if hi + N > end or natural + N > end:
                    break
                tmpl = self._mono(self.buf[natural - self.base: natural - self.base + N])
                region = self._mono(self.buf[lo - self.base: hi - self.base + N])
                if region.shape[0] >= tmpl.shape[0] and tmpl.size:
                    corr = np.correlate(region, tmpl, mode="valid")
                    cand = lo + int(np.argmax(corr)) * self.dec
                else:
                    cand = nominal
                cand = min(max(cand, self.base), end - N)
            frame = self.buf[cand - self.base: cand - self.base + N] * self.win
            self.acc += frame
            out.append(self.acc[:Hs].copy())
            self.acc[:-Hs] = self.acc[Hs:]
            self.acc[-Hs:] = 0
            self.prev = cand
            self.ana += Hs * self.speed
            # drop input that can no longer be referenced
            keep_from = min(self.prev + Hs, int(self.ana) - tol) - 1
            if keep_from > self.base:
                cut = keep_from - self.base
                self.buf = self.buf[cut:]
                self.base = keep_from
        if final and self.prev is not None:
            out.append(self.acc[:Hs].copy())
            self.acc[:] = 0
        if not out:
            return np.zeros((0, self.channels), dtype=np.float32)
        return np.concatenate(out)
