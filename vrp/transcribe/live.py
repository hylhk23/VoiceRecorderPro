"""Phrase-by-phrase recognition with provisional text.

Runs inside the worker process. Audio arrives per stream (Me, Others, or one mixed
stream) at 16 kHz with the position in the audio file and the wall-clock time of its
first sample. A voice detector cuts it into phrases (a pause of 0.5 s ends a phrase;
15 s at most). Each finished phrase is recognised once: a "final" result. While a
phrase is still being spoken, the part heard so far is recognised about once a
second: a "partial" result, shown in italics until the final one replaces it.

The decoder and the voice detector are passed in, so this module is tested without
the speech engine.

When the recording is paused ("off the record") the phrase in progress is finished
at once (flush), so no phrase joins words from before and after a pause.

Results (tuples):
  ("partial", sid, key, text, file_s, wall)
  ("final", sid, key, text, lang, file_s, file_end_s, wall, wall_end)
  ("ended", sid, key)
"""
from __future__ import annotations

import bisect
import re

import numpy as np

from vrp.transcribe import SR

KEEP_ANCHORS_S = 180.0
_FOREIGN = re.compile(r"[\u3040-\u30ff\uac00-\ud7af]")


class _Stream:
    def __init__(self, vad):
        self.vad = vad
        self.base = 0                   # stream sample at which this voice detector started
        self.pos = 0                    # samples received
        self.anchors_pos: list[int] = []
        self.anchors: list[tuple[float, float]] = []      # (file seconds, wall) at each anchor
        self.last_partial = 0
        self.partial_text = ""

    def anchor(self, file_s: float, wall: float) -> None:
        self.anchors_pos.append(self.pos)
        self.anchors.append((file_s, wall))
        cut = self.pos - int(KEEP_ANCHORS_S * SR)
        k = bisect.bisect_right(self.anchors_pos, cut) - 1
        if k > 0:
            del self.anchors_pos[:k]
            del self.anchors[:k]

    def where(self, p: int) -> tuple[float, float]:
        """File position and wall time of stream sample p."""
        k = max(0, bisect.bisect_right(self.anchors_pos, p) - 1)
        if not self.anchors:
            return 0.0, 0.0
        a = self.anchors_pos[k]
        fs, wall = self.anchors[k]
        d = (p - a) / SR
        return fs + d, wall + d


class LiveRecognizer:
    def __init__(self, decode, make_vad, partial: bool = True, partial_every_s: float = 1.0,
                 max_partial_s: float = 12.0, min_partial_s: float = 0.8):
        """decode(samples float32 16 kHz) -> (text, lang); make_vad() -> sherpa-like voice detector."""
        self.decode = decode
        self.make_vad = make_vad
        self.partial = partial
        self.partial_every = int(partial_every_s * SR)
        self.max_partial = int(max_partial_s * SR)
        self.min_partial = int(min_partial_s * SR)
        self.behind = False              # set by the worker when it cannot keep up: no partials then
        self.streams: dict[tuple, _Stream] = {}

    def audio(self, sid, key, samples: np.ndarray, file_s: float, wall: float) -> list:
        st = self.streams.get((sid, key))
        if st is None:
            st = self.streams[(sid, key)] = _Stream(self.make_vad())
        x = np.asarray(samples)
        if x.dtype == np.int16:
            x = x.astype(np.float32) / 32768.0
        x = np.ascontiguousarray(x, dtype=np.float32).reshape(-1)
        if x.size == 0:
            return []
        st.anchor(file_s, wall)
        st.vad.accept_waveform(x)
        st.pos += x.size
        out = self._finals(sid, key, st)
        if self.partial and not self.behind and st.vad.is_speech_detected() and \
                st.pos - st.last_partial >= self.partial_every:
            cur = st.vad.current_segment
            n = len(cur.samples)
            if cur.start >= 0 and self.min_partial <= n <= self.max_partial:
                text, _lang = self.decode(np.asarray(cur.samples, dtype=np.float32))
                if _FOREIGN.search(text):         # a guess in Japanese or Korean kana on a first syllable
                    text = ""
                st.last_partial = st.pos
                if text != st.partial_text:
                    st.partial_text = text
                    fs, wall0 = st.where(st.base + cur.start)
                    out.append(("partial", sid, key, text, fs, wall0))
        return out

    def _finals(self, sid, key, st: _Stream) -> list:
        out = []
        while not st.vad.empty():
            seg = st.vad.front
            samples = np.asarray(seg.samples, dtype=np.float32)
            start = st.base + int(seg.start)
            st.vad.pop()
            text, lang = self.decode(samples)
            fs, w0 = st.where(start)
            fe, w1 = st.where(start + samples.size)
            st.partial_text = ""
            st.last_partial = st.pos
            out.append(("final", sid, key, text, lang, fs, fe, w0, w1))
        return out

    def end(self, sid, key) -> list:
        """The file or the recording ended: finish the phrase in progress."""
        st = self.streams.pop((sid, key), None)
        if st is None:
            return [("ended", sid, key)]
        st.vad.flush()
        out = self._finals(sid, key, st)
        out.append(("ended", sid, key))
        return out

    def flush(self, sid, key) -> list:
        """Finish the phrase in progress now (the recording was paused); the stream goes on."""
        st = self.streams.get((sid, key))
        if st is None:
            return []
        st.vad.flush()
        out = self._finals(sid, key, st)
        st.vad = self.make_vad()
        st.base = st.pos
        st.partial_text = ""
        return out

    def keys(self) -> list[tuple]:
        return list(self.streams)
