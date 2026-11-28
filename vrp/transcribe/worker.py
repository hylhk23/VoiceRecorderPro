"""The separate process that runs the speech engine.

Recognition runs in its own process, at below-normal priority by default, so a
slow or crashing speech engine can never hold up or stop the recording. The
pattern (spawned process, two queues, priority class) is the one used by Secure
Offline Transcriber's worker (sot/worker.py).

Commands (main -> worker): ("audio", sid, key, int16 array, file_s, wall),
("flush", sid, key) when the recording is paused, ("end", sid, key), ("quit",).
Events (worker -> main): ("ready", seconds to load), ("error", text), ("lag", seconds),
and the results of vrp.transcribe.live.
"""
from __future__ import annotations

import logging
import multiprocessing as mp
import os
import queue
import time

log = logging.getLogger(__name__)

BEHIND_S = 2.0          # partial results stop while the text is this far behind
LAG_EVERY_S = 1.0


def _lower_priority() -> None:
    if os.name == "nt":
        import ctypes
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
    else:
        try:
            os.nice(10)
        except OSError:
            pass


def build(cfg: dict):
    """The real decoder and voice detector (sherpa-onnx)."""
    import numpy as np
    import sherpa_onnx
    from vrp.transcribe import SR
    rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=cfg["model"], tokens=cfg["tokens"], num_threads=int(cfg.get("threads", 2)), use_itn=True,
        language=cfg.get("language", ""))

    def decode(samples):
        st = rec.create_stream()
        st.accept_waveform(SR, np.asarray(samples, dtype=np.float32))
        rec.decode_stream(st)
        r = st.result
        return (r.text or "").strip(), (getattr(r, "lang", "") or "").strip("<|>")

    def make_vad():
        c = sherpa_onnx.VadModelConfig()
        c.silero_vad.model = cfg["vad"]
        c.silero_vad.threshold = float(cfg.get("vad_threshold", 0.5))
        c.silero_vad.min_silence_duration = float(cfg.get("min_silence_s", 0.5))
        c.silero_vad.min_speech_duration = 0.25
        c.silero_vad.max_speech_duration = float(cfg.get("max_phrase_s", 15.0))
        c.sample_rate = SR
        c.num_threads = 1
        return sherpa_onnx.VoiceActivityDetector(c, buffer_size_in_seconds=120)

    return decode, make_vad


def worker_main(cmd_q, evt_q, cfg: dict) -> None:
    from vrp.transcribe.live import LiveRecognizer
    if cfg.get("priority", "below") == "below":
        try:
            _lower_priority()
        except Exception:
            pass
    t0 = time.perf_counter()
    try:
        decode, make_vad = (cfg["_builder"] if "_builder" in cfg else build)(cfg)
    except Exception as exc:           # engine or model missing or damaged
        evt_q.put(("error", f"The speech engine could not start: {exc}"))
        return
    lr = LiveRecognizer(decode, make_vad, partial=bool(cfg.get("partial", True)))
    evt_q.put(("ready", round(time.perf_counter() - t0, 2)))
    last_lag = 0.0
    newest = None
    while True:
        try:
            msg = cmd_q.get(timeout=0.5)
        except queue.Empty:
            msg = None
        except (EOFError, OSError):
            return
        now = time.time()
        if msg is not None:
            kind = msg[0]
            try:
                if kind == "quit":
                    for sid, key in lr.keys():
                        for ev in lr.end(sid, key):
                            evt_q.put(ev)
                    return
                if kind == "audio":
                    _, sid, key, samples, file_s, wall = msg
                    newest = wall + len(samples) / 16000.0
                    lag = max(0.0, now - newest)
                    lr.behind = lag > BEHIND_S
                    for ev in lr.audio(sid, key, samples, file_s, wall):
                        evt_q.put(ev)
                elif kind == "end":
                    for ev in lr.end(msg[1], msg[2]):
                        evt_q.put(ev)
                elif kind == "flush":
                    for ev in lr.flush(msg[1], msg[2]):
                        evt_q.put(ev)
            except Exception as exc:
                evt_q.put(("error", f"Speech recognition failed: {exc}"))
        if now - last_lag >= LAG_EVERY_S:
            last_lag = now
            evt_q.put(("lag", round(max(0.0, now - newest), 1) if newest and cmd_q_nonempty(cmd_q) else 0.0))


def cmd_q_nonempty(q) -> bool:
    try:
        return not q.empty()
    except (OSError, NotImplementedError):
        return True


class WorkerHandle:
    """Main-process handle on the worker process."""

    def __init__(self, cfg: dict, target=worker_main):
        self.cfg = dict(cfg)
        self.target = target
        self.ctx = mp.get_context("spawn")
        self.proc = None
        self.cmd_q = self.evt_q = None
        self.ready = False

    def start(self) -> None:
        if self.alive():
            return
        self.cmd_q = self.ctx.Queue()
        self.evt_q = self.ctx.Queue()
        self.ready = False
        self.proc = self.ctx.Process(target=self.target, name="vrp-transcribe",
                                     args=(self.cmd_q, self.evt_q, self.cfg), daemon=True)
        self.proc.start()

    def alive(self) -> bool:
        return bool(self.proc and self.proc.is_alive())

    def send(self, msg) -> None:
        if self.cmd_q is not None:
            try:
                self.cmd_q.put(msg)
            except (ValueError, OSError):
                pass

    def poll(self, limit: int = 500) -> list:
        out = []
        if self.evt_q is None:
            return out
        for _ in range(limit):
            try:
                ev = self.evt_q.get_nowait()
            except queue.Empty:
                break
            except (EOFError, OSError):
                break
            if ev[0] == "ready":
                self.ready = True
            out.append(ev)
        return out

    def stop(self, timeout: float = 3.0) -> list:
        """Ask the worker to finish its phrases and quit; returns its last events."""
        events = []
        if self.alive():
            self.send(("quit",))
            end = time.monotonic() + timeout
            while self.proc.is_alive() and time.monotonic() < end:
                events += self.poll()
                time.sleep(0.02)
            events += self.poll()
            if self.proc.is_alive():
                self.proc.terminate()
                self.proc.join(1)
        self.proc = None
        return events
