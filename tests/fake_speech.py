"""A stand-in for the speech engine, for tests that must not need the 240 MB model.

FakeVad cuts 16 kHz audio into phrases by loudness (like Silero VAD, with the same
methods as sherpa-onnx's VoiceActivityDetector). fake_decode "recognises" a phrase as
its length, and its loudness picks the language, so a test can tell phrases apart:
loud (> 0.3) = Cantonese text in Simplified characters, otherwise English.
"""
import numpy as np

SR = 16000
WIN = 512


class _Seg:
    def __init__(self, start, samples):
        self.start = start
        self.samples = samples


class FakeVad:
    def __init__(self, threshold=0.02, min_silence_s=0.5, min_speech_s=0.25, max_speech_s=15.0):
        self.threshold = threshold
        self.min_silence = int(min_silence_s * SR)
        self.min_speech = int(min_speech_s * SR)
        self.max_speech = int(max_speech_s * SR)
        self.n = 0                      # samples seen
        self.buf = np.zeros(0, np.float32)
        self.cur_start = -1
        self.cur: list = []
        self.silence = 0
        self.out: list = []

    def accept_waveform(self, x):
        self.buf = np.concatenate([self.buf, np.asarray(x, np.float32)])
        while self.buf.size >= WIN:
            w, self.buf = self.buf[:WIN], self.buf[WIN:]
            loud = float(np.sqrt(np.mean(w * w))) > self.threshold
            if loud:
                if self.cur_start < 0:
                    self.cur_start = self.n
                    self.cur = []
                self.silence = 0
            if self.cur_start >= 0:
                self.cur.append(w)
                if not loud:
                    self.silence += WIN
                if self.silence >= self.min_silence or len(self.cur) * WIN >= self.max_speech:
                    self._close()
            self.n += WIN

    def _close(self):
        samples = np.concatenate(self.cur) if self.cur else np.zeros(0, np.float32)
        if self.silence:
            samples = samples[: max(0, samples.size - self.silence)]
        if samples.size >= self.min_speech:
            self.out.append(_Seg(self.cur_start, samples))
        self.cur_start, self.cur, self.silence = -1, [], 0

    def empty(self):
        return not self.out

    @property
    def front(self):
        return self.out[0]

    def pop(self):
        self.out.pop(0)

    def flush(self):
        if self.cur_start >= 0:
            self._close()

    def is_speech_detected(self):
        return self.cur_start >= 0

    @property
    def current_segment(self):
        if self.cur_start < 0:
            return _Seg(-1, np.zeros(0, np.float32))
        return _Seg(self.cur_start, np.concatenate(self.cur))

    def reset(self):
        self.__init__(self.threshold)


def fake_decode(samples):
    x = np.asarray(samples, np.float32)
    secs = x.size / SR
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    if peak > 0.3:
        return f"呢个系唔系{secs:.1f}秒", "<|yue|>"
    return f"phrase of {secs:.1f} seconds", "<|en|>"


def build(cfg):
    """Same shape as vrp.transcribe.worker.build: (decode, make_vad)."""
    def decode(samples):
        text, lang = fake_decode(samples)
        return text, lang.strip("<|>")
    return decode, FakeVad


def tone(seconds, amp=0.1, rate=SR):
    t = np.arange(int(seconds * rate)) / rate
    return (amp * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def silence(seconds, rate=SR):
    return np.zeros(int(seconds * rate), np.float32)


class InProcWorker:
    """The worker loop, run synchronously in this process (thread-safe: the recorder thread sends audio)."""

    def __init__(self, cfg):
        import threading
        from vrp.transcribe.live import LiveRecognizer
        self.cfg = cfg
        decode, make_vad = build(cfg)
        self.lr = LiveRecognizer(decode, make_vad, partial=cfg.get("partial", True))
        self.events = []
        self.started = self.dead = False
        self.ready = False
        self.sent = []
        self.lock = threading.Lock()

    def start(self):
        self.started = True
        with self.lock:
            self.events.append(("ready", 0.1))

    def alive(self):
        return self.started and not self.dead

    def send(self, msg):
        with self.lock:
            self.sent.append(msg[0])
            kind = msg[0]
            if kind == "audio":
                _, sid, key, pcm, fs, wall = msg
                self.events += self.lr.audio(sid, key, pcm, fs, wall)
            elif kind == "end":
                self.events += self.lr.end(msg[1], msg[2])
            elif kind == "flush":
                self.events += self.lr.flush(msg[1], msg[2])

    def poll(self):
        with self.lock:
            ev, self.events = self.events, []
        if any(e[0] == "ready" for e in ev):
            self.ready = True
        return ev

    def stop(self):
        ev = self.poll()
        with self.lock:
            for sid, key in self.lr.keys():
                ev += self.lr.end(sid, key)
        self.started = False
        return ev
