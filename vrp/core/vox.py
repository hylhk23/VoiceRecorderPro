"""Voice-operated switch (Section 7).

The gate turns a continuous stream of blocks into write operations:

    ("begin", start_sample)   a new segment starts at this absolute sample
    ("write", audio, start_sample)
    ("end", None)             the segment ended

A segment is only committed once its sound has lasted the minimum segment
length; shorter bursts (clicks) are dropped with their pre-roll and hang time
(FR-VOX-006). Times are counted in samples so results do not depend on block
size.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np


@dataclass
class VoxParams:
    threshold: float = 4.0      # 0..100 % of full scale
    window_ms: int = 20
    attack_ms: int = 20
    hang_s: float = 2.0
    preroll_ms: int = 300
    min_seg_s: float = 0.5

    @staticmethod
    def from_settings(s) -> "VoxParams":
        return VoxParams(s.get("vox.threshold"), s.get("vox.window_ms"), s.get("vox.attack_ms"),
                         s.get("vox.hang_s"), s.get("vox.preroll_ms"), s.get("vox.min_seg_s"))


IDLE, ATTACK, ACTIVE = 0, 1, 2


class VoxGate:
    def __init__(self, rate: int, channels: int, params: VoxParams):
        self.rate = rate
        self.channels = channels
        self.set_params(params)
        self.reset()

    def set_params(self, p: VoxParams) -> None:
        self.p = p
        self.win = max(1, int(self.rate * p.window_ms / 1000))
        self.thr = max(0.0, p.threshold) / 100.0
        self.attack = int(self.rate * p.attack_ms / 1000)
        self.hang = int(self.rate * p.hang_s)
        self.preroll = int(self.rate * p.preroll_ms / 1000)
        self.min_seg = int(self.rate * p.min_seg_s)

    def set_threshold(self, pct: float) -> None:
        self.p.threshold = pct
        self.thr = max(0.0, pct) / 100.0

    def reset(self) -> None:
        self.state = IDLE
        self._rest = np.zeros((0, self.channels), dtype=np.float32)
        self._rest_start = 0
        self._ring: deque = deque()           # (audio, start) pre-roll history
        self._ring_len = 0
        self._attack_buf: list = []           # (audio, start) windows above threshold
        self._attack_len = 0
        self._pending: list = []              # (audio, start) of an uncommitted segment
        self.committed = False
        self._seg_start = 0
        self._sound_start = 0
        self._last_sound_end = 0
        self._below = 0

    @property
    def active(self) -> bool:
        """True while sound is being captured (shown as Recording)."""
        return self.state == ACTIVE

    # ------------------------------------------------------------------
    def process(self, x: np.ndarray, start: int) -> list:
        """Feed a block starting at absolute sample `start`; returns operations."""
        ops: list = []
        if self._rest.shape[0]:
            x = np.concatenate([self._rest, x])
            start = self._rest_start
        n = x.shape[0]
        w = self.win
        full = (n // w) * w
        for i in range(0, full, w):
            self._window(x[i:i + w], start + i, ops)
        self._rest = x[full:]
        self._rest_start = start + full
        return ops

    def flush_end(self) -> list:
        """Close an open segment (on stop/pause)."""
        ops: list = []
        if self.state == ACTIVE and self.committed:
            if self._rest.shape[0]:
                ops.append(("write", self._rest, self._rest_start))
            ops.append(("end", None))
        self.reset()
        return ops

    # ------------------------------------------------------------------
    def _push_ring(self, a: np.ndarray, s: int) -> None:
        if self.preroll <= 0:
            return
        self._ring.append((a, s))
        self._ring_len += a.shape[0]
        while self._ring and self._ring_len - self._ring[0][0].shape[0] >= self.preroll:
            old, _ = self._ring.popleft()
            self._ring_len -= old.shape[0]

    def _ring_items(self) -> list:
        """Ring contents trimmed to exactly the pre-roll length."""
        items = list(self._ring)
        extra = self._ring_len - self.preroll
        if extra > 0 and items:
            a, s = items[0]
            items[0] = (a[extra:], s + extra)
        return items

    def _emit(self, ops: list, a: np.ndarray, s: int) -> None:
        if self.committed:
            ops.append(("write", a, s))
        else:
            self._pending.append((a, s))

    def _commit(self, ops: list) -> None:
        self.committed = True
        ops.append(("begin", self._seg_start))
        for a, s in self._pending:
            if a.shape[0]:
                ops.append(("write", a, s))
        self._pending = []

    def _window(self, a: np.ndarray, s: int, ops: list) -> None:
        mask = np.max(np.abs(a), axis=1) > self.thr if a.size else np.zeros(0, dtype=bool)
        loud = bool(mask.any())
        if self.state == IDLE:
            if loud:
                self.state = ATTACK
                self._attack_buf = [(a, s)]
                self._attack_len = a.shape[0]
                self._sound_start = s + int(np.argmax(mask))      # first sample above threshold
                self._last_sound_end = s + len(mask) - int(np.argmax(mask[::-1]))
                if self._attack_len >= self.attack:
                    self._trigger(ops)
            else:
                self._push_ring(a, s)
        elif self.state == ATTACK:
            if loud:
                self._attack_buf.append((a, s))
                self._attack_len += a.shape[0]
                self._last_sound_end = s + len(mask) - int(np.argmax(mask[::-1]))
                if self._attack_len >= self.attack:
                    self._trigger(ops)
            else:
                for aa, ss in self._attack_buf:
                    self._push_ring(aa, ss)
                self._push_ring(a, s)
                self._attack_buf = []
                self.state = IDLE
        else:  # ACTIVE
            n = a.shape[0]
            # only sound before the hang time runs out extends this segment
            limit = max(0, min(n, self._last_sound_end + self.hang - s))
            m = mask[:limit]
            if m.any():
                self._last_sound_end = s + limit - int(np.argmax(m[::-1]))   # one past the last loud sample
            end_at = self._last_sound_end + self.hang
            if s + n < end_at:
                self._emit(ops, a, s)
                if not self.committed and self._last_sound_end - self._sound_start >= self.min_seg:
                    self._commit(ops)
                return
            # the hang time runs out inside this window: stop exactly there
            cut = max(0, min(n, end_at - s))
            if cut:
                self._emit(ops, a[:cut], s)
            if not self.committed and self._last_sound_end - self._sound_start >= self.min_seg:
                self._commit(ops)
            if self.committed:
                ops.append(("end", None))
            # uncommitted bursts shorter than min_seg are dropped with their pre-roll
            self.state = IDLE
            self.committed = False
            self._pending = []
            self._ring.clear()
            self._ring_len = 0
            if cut < n:      # the rest of the window may already start the next segment
                self._window(a[cut:], s + cut, ops)

    def _trigger(self, ops: list) -> None:
        items = [*self._ring_items(), *self._attack_buf]
        seg_start = self._sound_start - self.preroll
        earliest = items[0][1] if items else self._sound_start
        seg_start = max(seg_start, earliest)
        pending = []
        for a, s in items:                       # trim to start exactly at seg_start
            if s + a.shape[0] <= seg_start:
                continue
            if s < seg_start:
                a, s = a[seg_start - s:], seg_start
            pending.append((a, s))
        self._seg_start = seg_start
        self._pending = pending
        self._ring.clear()
        self._ring_len = 0
        self._attack_buf = []
        self.state = ACTIVE
        self.committed = False
        if self._last_sound_end - self._sound_start >= self.min_seg:
            self._commit(ops)
