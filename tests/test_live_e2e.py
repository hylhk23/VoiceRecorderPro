"""End to end with the real speech engine (1.1.0): recorder -> worker process -> transcript files.

Needs the Live Transcript pack (VRP_LT_PACK = its folder) and the sample recordings that come with
the SenseVoiceSmall model (VRP_LT_WAVS = folder with en.wav and yue.wav); skipped without them.
"""
import os
import wave

import numpy as np
import pytest

from conftest import pump, wait_for

PACK = os.environ.get("VRP_LT_PACK", "")
WAVS = os.environ.get("VRP_LT_WAVS", "")
pytestmark = pytest.mark.skipif(not (PACK and WAVS and os.path.isdir(PACK) and os.path.isdir(WAVS)),
                                reason="real speech pack not available (set VRP_LT_PACK and VRP_LT_WAVS)")


def load(name):
    with wave.open(os.path.join(WAVS, name)) as w:
        assert w.getframerate() == 16000
        return np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768


def source(x16):
    import soxr
    cache = {}

    def src(pos, n, rate, ch):
        if rate not in cache:
            cache[rate] = soxr.resample(x16, 16000, rate).astype(np.float32)
        sp = cache[rate]
        idx = pos + np.arange(n)
        x = np.where(idx < sp.size, sp[np.minimum(idx, sp.size - 1)], 0.0)
        return np.repeat(x[:, None], ch, axis=1).astype(np.float32)
    return src


def run(qapp, tmp_path, settings, mic, system, seconds, during=None):
    from vrp.core.audio_backend import FakeBackend
    from vrp.core.engine import Engine
    from vrp.settings import Settings
    s = Settings()
    s.update({"rec.folder": str(tmp_path / "rec"), "rec.fallback_folder": str(tmp_path / "fb"),
              "vox.enabled": False, "lt.enabled": True, **settings}, save=False)
    be = FakeBackend(source(mic), realtime=2.0)
    be.loopback_source = source(system)
    e = Engine(s, be)
    e.start()
    try:
        assert e.lt_pack_status(refresh=True).ok, e.lt_pack_status().reason
        e.record()
        assert e.lt_active
        if during:
            during(e)
        pump(qapp, seconds / 2.0 + 0.5)
        e.stop()
        assert wait_for(qapp, lambda: not e.capturing, 10)
        assert wait_for(qapp, lambda: not e.transcriber.writers and not e.transcriber.closing, 60)
        return e, sorted(os.listdir(tmp_path / "rec"))
    finally:
        e.shutdown()


def test_separate_channels_me_and_others(qapp, tmp_path):
    en, yue = load("en.wav"), load("yue.wav")
    gap = np.zeros(16000, np.float32)
    mic = np.concatenate([gap, en, gap * 0, np.zeros(yue.size + 16000, np.float32)])
    system = np.concatenate([gap, np.zeros(en.size + 8000, np.float32), yue, gap])
    e, names = run(qapp, tmp_path, {"rec.src_system": True, "fmt.channels": 2, "fmt.layout": "split",
                                    "lt.save": "audio_text"}, mic, system, max(mic.size, system.size) / 16000)
    texts = [(x.speaker, x.text) for x in e.transcriber.entries if x.kind == "text"]
    assert ("me", ) == tuple({s for s, _ in texts if "chieftain" in _.lower() or "tribal" in _.lower()})
    yue_lines = [t for s, t in texts if s == "others"]
    assert yue_lines and "呢幾個字" in "".join(yue_lines)           # Traditional (Hong Kong) characters
    base = [n[:-4] for n in names if n.endswith(".mp3")][0]
    assert base + ".txt" in names and base + ".srt" in names
    txt = (tmp_path / "rec" / (base + ".txt")).read_text(encoding="utf-8")
    assert "] Me: " in txt and "] Others: " in txt


def test_text_only_and_pause(qapp, tmp_path):
    """Text only; the recording is paused in the middle of a sentence (off the record) and resumed."""
    en = load("en.wav")
    mic = np.concatenate([np.zeros(8000, np.float32), en, np.zeros(16000, np.float32)])

    def pause_mid_sentence(e):
        # 3 s into the file (2.5 s into the sentence), counted in audio written, not in clock time
        assert wait_for(qapp, lambda: e.transcriber.cur_pos >= 3.0, 20)
        e.record()
        assert wait_for(qapp, lambda: e.state.value == "rec_paused")
        pump(qapp, 0.6)
        e.record()
        assert wait_for(qapp, lambda: e.state.value == "recording")
    e, names = run(qapp, tmp_path, {"lt.save": "text_only"}, mic, np.zeros(10, np.float32), mic.size / 16000,
                   pause_mid_sentence)
    assert names and all(n.endswith(".txt") for n in names)
    txt = (tmp_path / "rec" / names[0]).read_text(encoding="utf-8")
    assert "Text only: no sound was saved" in txt and "Off the record (nothing saved)" in txt
    kinds = [x.kind for x in e.transcriber.entries]
    assert "off" in kinds and kinds.count("text") >= 2           # the sentence was cut at the pause
    # the words before the pause are one phrase, finished at the pause (shown before the marker); the rest of
    # the sentence comes after it
    texts = [x.text.lower() for x in e.transcriber.entries if x.kind == "text"]
    assert kinds[0] == "text" and kinds.index("off") < len(kinds) - 1, kinds
    assert texts[0].startswith("the") and "pieces" not in texts[0] and "pieces" in " ".join(texts[1:]), texts
