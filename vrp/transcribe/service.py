"""Live transcript, main-process side.

The recorder thread hands over every block it writes (tap) and tells when a file
closes (tap_close). Blocks are resampled to 16 kHz and sent to the worker process
per stream: with Separate channels the left channel is "Me" (microphone) and the
right one "Others" (computer audio); otherwise one mixed stream without a label.

The engine polls this object from its 50 ms timer on the GUI thread. Finished
phrases are cleaned up (vrp.transcribe.text), shown and appended to the transcript
files (vrp.transcribe.transcript). Nothing here touches the network.
"""
from __future__ import annotations

import logging
import os
import threading
import time

import numpy as np

from vrp.transcribe import SR, language_label, script_label, sense_voice_code, text
from vrp.transcribe.retention import AUDIO, TEXT, Retention
from vrp.transcribe.transcript import BOOKMARK, OFF, TEXT as T_TEXT, Entry, TranscriptWriter

log = logging.getLogger(__name__)

MAX_SHOWN = 5000
HOLD_S = 2.0          # a bookmark waits this long for the phrase that was being spoken when it was set
HOLD_MAX_S = 16.0     # ... but never longer than the longest phrase


def options_from_settings(s) -> dict:
    return {
        "language": s.get("lt.language"), "script": s.get("lt.script"), "names": s.get("lt.names"),
        "mask": bool(s.get("lt.mask")), "save": s.get("lt.save"), "keep_days": s.get("lt.keep_days"),
        "delete_days": s.get("lt.delete_days"), "txt": True, "srt": bool(s.get("lt.files_srt")),
        "partial": bool(s.get("lt.partial")), "threads": s.get("lt.threads"),
        "priority": s.get("lt.priority"), "encrypt": bool(s.get("lt.encrypt")),
    }


class TranscriptionService:
    def __init__(self, retention: Retention | None = None, worker_factory=None, encrypt=None):
        self.retention = retention
        self.worker_factory = worker_factory     # (cfg) -> WorkerHandle-like
        self.encrypt = encrypt or (lambda _p: (True, ""))
        self.worker = None
        self.worker_cfg: dict = {}
        self.opts: dict = {}
        self.active = False
        self.split = False
        self.entries: list[Entry] = []
        self.partials: dict[str, tuple[str, float]] = {}
        self.lag = 0.0
        self.error = ""
        self.messages: list[tuple[str, str]] = []       # (level, text) for the engine to show
        self.rev = 0
        self.added = 0                  # entries added this session (the list keeps the newest MAX_SHOWN)
        self.reordered = 0              # a phrase was placed before a newer bookmark: redraw
        self.held: list[tuple[Entry, str | None]] = []     # markers not yet written to the file
        self.last_txt = ""              # the newest transcript file (Transcript > Open Transcript)
        self.writers: dict[str, TranscriptWriter] = {}
        self.open_sids: dict[str, set] = {}
        self.closing: set[str] = set()
        self.pending: list[Entry] = []
        self.cur_key: str | None = None
        self.cur_pos = 0.0
        self._lock = threading.Lock()
        self._resamplers: dict = {}
        self._off_since: float | None = None
        self.started_at = 0.0

    # ------------------------------------------------------------- session
    @property
    def save_audio(self) -> bool:
        return self.opts.get("save") != "text_only"

    @staticmethod
    def _worker_cfg(opts: dict, pack) -> dict:
        return {"model": pack.model, "tokens": pack.tokens, "vad": pack.vad,
                "language": sense_voice_code(opts.get("language", "auto")), "threads": opts.get("threads", 2),
                "partial": opts.get("partial", True), "priority": opts.get("priority", "below")}

    def _ensure_worker(self, cfg: dict) -> None:
        if self.worker is not None and (cfg != self.worker_cfg or not self.worker.alive()):
            self._collect(self.worker.stop())
            self.worker = None
        if self.worker is None:
            self.worker = self.worker_factory(cfg)
            self.worker_cfg = cfg
            self.worker.start()

    def prewarm(self, opts: dict, pack) -> None:
        """Load the speech engine before the first recording, so the words appear from the start."""
        if not self.active:
            self._ensure_worker(self._worker_cfg(opts, pack))

    def release(self) -> None:
        """The live transcript was turned off: free the speech engine's memory."""
        if not self.active and self.worker is not None and not self.writers and not self.closing:
            self._collect(self.worker.stop())
            self.worker = None

    def start_session(self, opts: dict, pack, split: bool) -> None:
        """A recording starts with the live transcript on."""
        self.opts = dict(opts)
        self.split = split
        self._ensure_worker(self._worker_cfg(opts, pack))
        self._release(lambda _m, k: k in self.writers)
        self.held = []
        self.entries = []
        self.added = 0
        self.partials = {}
        self.pending = []
        self.error = ""
        self.lag = 0.0
        self.cur_key = None
        self._off_since = None
        with self._lock:
            self._resamplers = {}
        self.started_at = time.time()
        self.active = True
        self.rev += 1

    def stop_session(self) -> None:
        """The recording stopped. Phrases still in the worker arrive later and are still saved."""
        if self._off_since is not None:
            self.resumed()
        self.active = False
        self.partials = {}
        self.rev += 1

    def shutdown(self) -> None:
        if self.worker is not None:
            self._collect(self.worker.stop())
            self.worker = None
        self._release(lambda _m, k: k in self.writers)
        self.held = []
        for key in list(self.writers):
            self._close_writer(key)

    # ------------------------------------------------ recorder thread side
    def tap(self, x: np.ndarray, file_frame: int, rate: int, wall: float, path: str) -> None:
        """One block as written to the file (or, in Text only mode, as it would have been)."""
        if not self.active or self.worker is None or x is None or x.shape[0] == 0:
            return
        if x.ndim == 1:
            x = x[:, None]
        if self.split and x.shape[1] >= 2:
            parts = (("me", x[:, 0]), ("others", x[:, 1]))
        else:
            parts = (("mix", x.mean(axis=1) if x.shape[1] > 1 else x[:, 0]),)
        file_s = file_frame / float(rate)
        for sid, mono in parts:
            y = self._resample(sid, path, mono, rate)
            if y.size:
                pcm = np.clip(y * 32767.0, -32768, 32767).astype(np.int16)
                self.worker.send(("audio", sid, path, pcm, file_s, wall))
        self.cur_key = path
        self.cur_pos = file_s + x.shape[0] / float(rate)

    def _resample(self, sid: str, path: str, mono: np.ndarray, rate: int) -> np.ndarray:
        mono = np.ascontiguousarray(mono, dtype=np.float32)
        if rate == SR:
            return mono
        with self._lock:
            key = (sid, path, rate)
            rs = self._resamplers.get(key)
            if rs is None:
                import soxr
                rs = self._resamplers[key] = soxr.ResampleStream(rate, SR, 1, dtype="float32", quality="MQ")
        return rs.resample_chunk(mono)

    def tap_close(self, path: str) -> None:
        """The recorder closed this file: finish its phrases."""
        if self.worker is None:
            return
        sids = ("me", "others") if self.split else ("mix",)
        with self._lock:
            for k in [k for k in self._resamplers if k[1] == path]:
                del self._resamplers[k]
        self.open_sids.setdefault(path, set()).update(sids)
        self.closing.add(path)
        for sid in sids:
            self.worker.send(("end", sid, path))

    # --------------------------------------------------------- engine side
    def poll(self) -> bool:
        """Collect results; True when something on screen changed."""
        if self.worker is None:
            return False
        before = self.rev
        self._collect(self.worker.poll())
        self._release_due()
        if self.active and not self.worker.alive() and not self.error:
            self.error = "The live transcript stopped: the speech engine closed unexpectedly. " \
                         "The recording continues."
            self.messages.append(("warning", self.error))
            self.active = False
            self.rev += 1
        return self.rev != before

    def _collect(self, events: list) -> None:
        for ev in events:
            try:
                self._event(ev)
            except Exception:
                log.exception("transcript event failed: %r", ev[:3])

    def _event(self, ev) -> None:
        kind = ev[0]
        if kind == "partial":
            _, sid, _key, txt, _fs, wall = ev
            # words in progress are shown, never saved; masked too, so a shared screen shows no numbers
            t = text.clean(txt, "", self.opts.get("script", "hk"), self.opts.get("names"),
                           self.opts.get("mask", False))
            if t:
                self.partials[sid] = (t, wall)
            else:
                self.partials.pop(sid, None)
            self.rev += 1
        elif kind == "final":
            _, sid, key, txt, lang, fs, fe, w0, w1 = ev
            self.partials.pop(sid, None)
            t = text.clean(txt, lang, self.opts.get("script", "hk"), self.opts.get("names"),
                           self.opts.get("mask", False))
            self.rev += 1
            if not t:
                return
            pos = (fs, fe) if self.save_audio else (None, None)
            e = Entry(T_TEXT, w0, w1, pos[0], pos[1], None if sid == "mix" else sid, t, lang)
            self._add(e, key)
        elif kind == "ended":
            _, sid, key = ev
            sids = self.open_sids.get(key)
            if sids is not None:
                sids.discard(sid)
                if not sids and key in self.closing:
                    self.closing.discard(key)
                    self.open_sids.pop(key, None)
                    self._release(lambda _m, k: k == key)
                    self._close_writer(key)
        elif kind == "lag":
            if abs(ev[1] - self.lag) >= 0.1:
                self.lag = ev[1]
                self.rev += 1
        elif kind == "ready":
            log.info("speech engine ready in %.1f s", ev[1])
            self.rev += 1
        elif kind == "error":
            self.error = ev[1]
            self.messages.append(("warning", ev[1]))
            self.rev += 1

    def _writer(self, key: str) -> TranscriptWriter | None:
        w = self.writers.get(key)
        if w is not None:
            return w
        base = os.path.splitext(key)[0]
        o = self.opts
        what = (f"Recording: {os.path.basename(key)}" if self.save_audio else "Text only: no sound was saved")
        header = ["Voice Recorder Pro - live transcript", what,
                  f"Started: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.started_at or time.time()))}",
                  f"Language: {language_label(o.get('language', 'auto'))}. Chinese characters: "
                  f"{script_label(o.get('script', 'hk'))}."]
        if o.get("mask"):
            header.append("Phone, ID and card numbers and e-mail addresses are masked.")
        try:
            try:
                w = TranscriptWriter(base, header, append=os.path.exists(base + ".txt") and self.save_audio,
                                     srt=bool(o.get("srt", True)) and self.save_audio)
            except FileExistsError:
                if self.save_audio or not os.path.isfile(base + ".txt"):
                    raise
                # Text only: a transcript of an earlier recording has this name; never overwrite it
                for k in range(2, 100):
                    try:
                        w = TranscriptWriter(f"{base}-{k}", header, srt=False)
                        break
                    except FileExistsError:
                        continue
                else:
                    raise OSError(f"no free name for the transcript of {os.path.basename(key)}") from None
        except OSError as exc:
            if self.error != f"The transcript could not be saved: {exc}":
                self.error = f"The transcript could not be saved: {exc}"
                self.messages.append(("warning", self.error))
            return None
        self.writers[key] = w
        self.last_txt = w.txt_path
        for e in self.pending:
            w.add(e)
        self.pending = []
        return w

    def _add(self, e: Entry, key: str | None) -> None:
        """Show an entry and save it. A phrase that was being spoken when a bookmark was set
        finishes after the bookmark: it is placed before it, on screen and in the file."""
        i = len(self.entries)
        if e.kind == T_TEXT:
            while i > 0 and self.entries[i - 1].kind != T_TEXT and self.entries[i - 1].wall > e.wall:
                i -= 1
        if i == len(self.entries):
            self.entries.append(e)
        else:
            self.entries.insert(i, e)
            self.reordered += 1
        self.added += 1
        if len(self.entries) > MAX_SHOWN:
            del self.entries[: len(self.entries) - MAX_SHOWN]
        self.rev += 1
        if e.kind != T_TEXT:
            self.held.append((e, key))
            return
        self._release(lambda m, _k: m.wall <= e.wall)
        self._write(e, key)

    def _release(self, cond) -> None:
        """Write the held markers, oldest first, while cond(marker, key) holds."""
        while self.held and cond(*self.held[0]):
            m, key = self.held.pop(0)
            self._write(m, key)

    def _release_due(self, now: float | None = None) -> None:
        if not self.held:
            return
        now = time.time() if now is None else now
        age = now - (self.held[0][0].wall_end or self.held[0][0].wall)
        if (age >= HOLD_S and not self.partials and self.lag < 1.0) or age >= HOLD_MAX_S or not self.active:
            if age >= HOLD_S:
                self._release(lambda _m, _k: True)

    def _write(self, e: Entry, key: str | None) -> None:
        if key is None:
            self.pending.append(e)
            return
        w = self._writer(key)
        if w is not None:
            try:
                w.add(e)
            except OSError as exc:
                self.error = f"The transcript could not be written: {exc}"
                self.messages.append(("warning", self.error))

    def _close_writer(self, key: str) -> None:
        w = self.writers.pop(key, None)
        if w is None:
            return
        try:
            files = w.close()
        except OSError as exc:
            self.messages.append(("warning", f"The transcript {os.path.basename(w.txt_path)} could not be "
                                             f"finished: {exc}"))
            return
        if self.opts.get("encrypt"):
            for p in files:
                ok, why = self.encrypt(p)
                if not ok:
                    log.warning("could not encrypt %s: %s", p, why)
        days = int(self.opts.get("delete_days") or 0)
        if days and self.retention is not None:
            for p in files:
                self.retention.add(p, TEXT, days)
        log.info("transcript saved: %s", ", ".join(os.path.basename(p) for p in files))

    # ------------------------------------------------------------ markers
    def bookmark(self) -> Entry:
        now = time.time()
        e = Entry(BOOKMARK, now, now, self.cur_pos if self.save_audio and self.cur_key else None, None)
        self._add(e, self.cur_key if self.cur_key not in self.closing else None)
        return e

    def paused(self) -> None:
        if self.active and self._off_since is None:
            self._off_since = time.time()
            self.rev += 1
            if self.worker is not None and self.cur_key:
                for sid in (("me", "others") if self.split else ("mix",)):
                    self.worker.send(("flush", sid, self.cur_key))     # no phrase spans the pause

    def resumed(self) -> None:
        if self._off_since is None:
            return
        e = Entry(OFF, self._off_since, time.time(), None, None)
        self._off_since = None
        self._add(e, self.cur_key if self.cur_key not in self.closing else None)

    @property
    def off_record(self) -> bool:
        return self._off_since is not None

    def audio_closed(self, path: str) -> None:
        """An audio file (or its untouched copy) was finished."""
        if self.opts.get("save") == "text_check" and self.retention is not None and os.path.exists(path):
            self.retention.add(path, AUDIO, float(self.opts.get("keep_days") or 30))

    def take_messages(self) -> list[tuple[str, str]]:
        m, self.messages = self.messages, []
        return m
