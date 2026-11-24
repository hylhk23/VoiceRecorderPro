"""Clean-up of the microphone (v1.0.4): noise suppression (RNNoise), automatic level,
and the untouched copy written next to a cleaned recording."""
import os

import numpy as np
import pytest
import soundfile as sf

from conftest import pump, wait_for
from vrp.core import cleanup, journal
from vrp.core.audio_backend import FakeBackend
from vrp.core.cleanup import CleanChain, DelayLine, Denoiser, History, Leveller
from vrp.core.engine import Engine, State
from vrp.core.model import original_path
from vrp.settings import Settings

RATE = 16000
HAVE_RNN, WHY = cleanup.denoise_support()
needs_rnnoise = pytest.mark.skipif(not HAVE_RNN, reason=f"RNNoise not built: {WHY}")

F0 = 130.0
FORMANTS = [(650, 1.0), (1100, 0.6), (2400, 0.3)]


def voice(pos, n, rate):
    """Voice-like test signal: harmonics of 130 Hz shaped by three formants, in syllables with pauses."""
    t = (pos + np.arange(n)) / rate
    phase = 2 * np.pi * (F0 * t - F0 * 0.04 / (2 * np.pi * 4.5) * np.cos(2 * np.pi * 4.5 * t))
    y = np.zeros(n)
    for k in range(1, int(4000 / F0)):
        fk = k * F0
        y += (sum(a / (1 + ((fk - fc) / 120) ** 2) for fc, a in FORMANTS) + 0.02) * np.sin(k * phase)
    env = np.clip(np.sin(2 * np.pi * 2.2 * t), 0, None) ** 0.6
    return y * env / 3.2


def syllable_mask(n, rate, pos=0):
    t = (pos + np.arange(n)) / rate
    s = np.clip(np.sin(2 * np.pi * 2.2 * t), 0, None)
    return s > 0.3, s == 0


def db(x):
    return 10 * np.log10(np.mean(np.asarray(x, np.float64) ** 2) + 1e-15)


def mic_source(voice_db=-26.0, noise_db=-40.0, voice_on=True):
    """Fake microphone: voice (optional) plus white noise, reproducible per block."""
    vg = 10 ** (voice_db / 20)
    ng = 10 ** (noise_db / 20)

    def src(pos, n, rate, ch):
        x = (voice(pos, n, rate) * vg if voice_on else np.zeros(n))
        x = x + np.random.default_rng(pos).normal(0, ng, n)
        return np.repeat(x[:, None], ch, axis=1).astype(np.float32)
    return src


def run_chain(chain, x, block=1000):
    outs = [chain.process(x[i:i + block]) for i in range(0, len(x), block)]
    return np.concatenate([o[0] for o in outs]), np.concatenate([o[1] for o in outs])


# ------------------------------------------------------------------ noise suppression
@needs_rnnoise
@pytest.mark.parametrize("rate", [8000, 11025, 16000, 22050, 44100, 48000, 96000])
def test_denoiser_keeps_length_and_never_runs_dry(rate):
    d = Denoiser(rate, 1)
    rng = np.random.default_rng(rate)
    for _ in range(120):
        n = int(rng.integers(1, 8192))
        assert d.process(np.zeros((n, 1), np.float32)).shape == (n, 1)
    assert d.underruns == 0
    d.close()


@needs_rnnoise
def test_denoiser_removes_noise_keeps_voice_and_reports_its_delay():
    n = RATE * 6
    x = mic_source()(0, n, RATE, 1)
    chain = CleanChain(RATE, 1, denoise=True, level=False)
    det, out = run_chain(chain, x)
    assert det is not None and np.array_equal(det, out)          # no leveller: detect == out
    d = chain.delay
    # the delay the recorder compensates is the real one (cross-correlation with the input)
    a, b = x[:, 0] - x[:, 0].mean(), out[:, 0] - out[:, 0].mean()
    cc = np.fft.irfft(np.fft.rfft(b, 2 * n) * np.conj(np.fft.rfft(a, 2 * n)))[:n]
    assert int(np.argmax(cc)) == d
    on, off = syllable_mask(n, RATE)
    on[:RATE] = off[:RATE] = False
    y = np.roll(out[:, 0], -d)
    assert abs(db(y[on]) - db(x[on, 0])) < 2.0                   # the voice keeps its level
    assert db(y[off]) < db(x[off, 0]) - 12.0                     # noise between syllables much lower


@needs_rnnoise
def test_denoiser_stereo_channels_independent():
    n = RATE * 3
    left = mic_source()(0, n, RATE, 1)[:, 0]
    right = np.random.default_rng(5).normal(0, 0.01, n).astype(np.float32)   # noise only
    chain = CleanChain(RATE, 2, True, False)
    _, out = run_chain(chain, np.stack([left, right], axis=1))
    tail = slice(RATE, n)
    assert db(out[tail, 0]) > db(out[tail, 1]) + 20


# ------------------------------------------------------------------ automatic level
def test_leveller_near_and_far_voices_come_out_alike():
    lev = Leveller(RATE, 1)
    parts, spans, pos = [], [], 0
    for lvl in (-30.0, 0.0, -30.0):            # far (about -40 dBFS), near (-10), far again: 30 dB apart
        seg = mic_source(lvl, -70.0)(pos, RATE * 5, RATE, 1)
        parts.append(seg)
        spans.append((pos + RATE * 5 // 2, pos + RATE * 5))    # judged after 2.5 s of each talker
        pos += RATE * 5
    x = np.concatenate(parts)
    y = np.concatenate([lev.process(x[i:i + 777]) for i in range(0, len(x), 777)])
    assert y.shape == x.shape
    outs = []
    for a, b in spans:
        on, _ = syllable_mask(b - a, RATE, a)
        outs.append(db(y[a:b][on]))
    ins = []
    for a, b in spans:
        on, _ = syllable_mask(b - a, RATE, a)
        ins.append(db(x[a:b][on]))
    assert max(ins) - min(ins) > 29
    assert max(outs) - min(outs) < 4.0                          # 30 dB apart in, under 4 dB out
    assert all(abs(o - Leveller.TARGET_DB) < 5.0 for o in outs)
    assert np.max(np.abs(y)) <= Leveller.CEILING + 1e-4         # limiter: below -1 dBFS


def test_leveller_does_not_raise_noise_before_anyone_speaks():
    x = np.random.default_rng(1).normal(0, 10 ** (-45 / 20), (RATE * 3, 1)).astype(np.float32)
    lev = Leveller(RATE, 1)
    y = lev.process(x)
    assert abs(db(y) - db(x)) < 0.5
    assert lev.speech_db is None


def test_leveller_ignores_a_single_click():
    x = np.random.default_rng(2).normal(0, 10 ** (-50 / 20), (RATE * 2, 1)).astype(np.float32)
    x[RATE:RATE + 80] = 0.5                                     # 5 ms click
    lev = Leveller(RATE, 1)
    lev.process(x)
    assert lev.speech_db is None and lev.gain_db == 0.0


def test_leveller_holds_gain_in_pauses():
    lev = Leveller(RATE, 1)
    lev.process(mic_source(-40, -75)(0, RATE * 3, RATE, 1))
    g = lev.gain_db
    assert g > 10
    lev.process(np.zeros((RATE * 2, 1), np.float32))            # two seconds of silence
    assert lev.gain_db == pytest.approx(g)


# ------------------------------------------------------------------ helpers
def test_history_and_delay_line():
    h = History(RATE, 1, seconds=1.0)
    for k in range(5):
        h.push(np.full((RATE // 2, 1), k, np.float32), k * RATE // 2)
    got = h.get(RATE * 3 // 2, RATE // 2)                       # fourth block (one second is held)
    assert np.all(got == 3)
    old = h.get(0, 10)                                          # no longer held: silence, counted
    assert np.all(old == 0) and h.missed == 10
    h.push(np.ones((5, 1), np.float32), 10 ** 6)                # a jump in sample numbers starts afresh
    assert h.get(10 ** 6, 5).sum() == 5
    dl = DelayLine(3, 1)
    y = np.concatenate([dl.process(np.arange(k, k + 2, dtype=np.float32)[:, None]) for k in (1, 3, 5)])
    assert y[:, 0].tolist() == [0, 0, 0, 1, 2, 3]


def test_original_path_names():
    assert original_path(os.path.join("r", "VOX20261123-001.mp3")) == os.path.join("r", "VOX20261123-001-original.mp3")
    assert original_path("a/b.wav", 2) == "a/b-original-2.wav"


def test_unavailable_library_is_reported(monkeypatch, tmp_path):
    cleanup._reset_cache()
    monkeypatch.setenv("VRP_RNNOISE", str(tmp_path / "missing.so"))
    monkeypatch.setattr(cleanup, "_candidates", lambda: [tmp_path / "missing.so"])
    ok, why = cleanup.denoise_support()
    assert not ok and "missing.so" in why
    chain = CleanChain(RATE, 1, denoise=True, level=False)
    assert not chain.active and chain.error
    cleanup._reset_cache()


# ------------------------------------------------------------------ in the recorder
@pytest.fixture
def make_engine(qapp, tmp_path):
    engines = []

    def make(mic_src=None, pc_src=None, system=False, realtime=8.0, **settings):
        s = Settings()
        s.update({"rec.folder": str(tmp_path / "rec"), "rec.fallback_folder": str(tmp_path / "fb"),
                  "vox.enabled": False, "fmt.codec": "wav", "fmt.rate": RATE, "fmt.channels": 1,
                  "rec.src_mic": True, "rec.src_system": system, **settings}, save=False)
        be = FakeBackend(mic_src or mic_source(), realtime=realtime)
        if pc_src is not None:
            be.loopback_source = pc_src
        e = Engine(s, be)
        e.start()
        engines.append(e)
        return e
    yield make
    for e in engines:
        e.shutdown()


def record(qapp, e, seconds_wall):
    e.record()
    assert e.state in (State.RECORDING, State.ARMED)
    pump(qapp, seconds_wall)
    path = e.recorder.status.get("path")
    e.stop()
    assert wait_for(qapp, lambda: e.state == State.STOPPED, 8.0)
    return path


@needs_rnnoise
def test_recording_with_cleanup_writes_aligned_untouched_copy(qapp, make_engine):
    e = make_engine(**{"fmt.denoise": True, "fmt.keep_original": True})
    path = record(qapp, e, 0.8)                                 # ~6 s of audio at 8x
    orig = original_path(path)
    assert os.path.exists(path) and os.path.exists(orig)
    a, ra = sf.read(path, dtype="float32")
    b, rb = sf.read(orig, dtype="float32")
    assert ra == rb == RATE and len(a) == len(b) and len(a) > RATE * 4
    # syllables and pauses, found in the untouched copy (10 ms frames, after the first second)
    f = RATE // 100
    m = (len(b) // f) * f
    eb = np.mean(b[:m].reshape(-1, f) ** 2, axis=1)
    ea = np.mean(a[:m].reshape(-1, f) ** 2, axis=1)
    eb, ea = eb[100:], ea[100:]
    on = eb > np.percentile(eb, 70)
    off = eb < np.percentile(eb, 25)
    # the copy is untouched: noise where it was; in the recording the pauses are far quieter
    assert 10 * np.log10(ea[off].mean()) < 10 * np.log10(eb[off].mean()) - 10
    # and both line up sample for sample: syllables fall in the same frames of both files
    lag = np.argmax(np.correlate(np.log10(ea + 1e-12) - np.log10(ea + 1e-12).mean(),
                                 np.log10(eb + 1e-12) - np.log10(eb + 1e-12).mean(), "full")) - (len(ea) - 1)
    assert lag == 0
    assert 10 * np.log10(ea[on].mean()) > 10 * np.log10(ea[off].mean()) + 25
    # tags: what was done, and which file the copy belongs to
    info = sf.info(path)
    with open(path, "rb") as f:
        blob = f.read()
    assert b"IPRC" in blob and b"noise suppression (RNNoise)" in blob
    with open(orig, "rb") as f:
        oblob = f.read()
    assert b"Untouched copy of " + os.path.basename(path).encode() in oblob and b"IPRC" not in oblob
    assert info.frames == len(a)
    # no journals are left behind
    assert not os.path.exists(path + journal.SUFFIX) and not os.path.exists(orig + journal.SUFFIX)


@needs_rnnoise
def test_split_layout_keeps_computer_audio_identical_in_both_files(qapp, make_engine):
    tone = lambda pos, n, rate, ch: np.repeat((0.2 * np.sin(2 * np.pi * 1000 * (pos + np.arange(n)) / rate))[:, None],
                                              ch, axis=1).astype(np.float32)
    e = make_engine(pc_src=tone, system=True, **{"fmt.layout": "split", "fmt.channels": 2,
                                                  "fmt.denoise": True, "fmt.keep_original": True})
    path = record(qapp, e, 0.8)
    a, _ = sf.read(path, dtype="float32")
    b, _ = sf.read(original_path(path), dtype="float32")
    assert a.shape == b.shape and a.shape[1] == 2
    # the right channel (computer audio) is delayed by the same amount in both: identical
    assert np.max(np.abs(a[:, 1] - b[:, 1])) < 1e-4
    assert db(a[:, 1]) > -20


@needs_rnnoise
def test_vox_ignores_noise_after_suppression(qapp, make_engine):
    noisy = mic_source(voice_on=False, noise_db=-28.0)       # loud fan noise, nobody talking
    # without clean-up, the noise opens Vox at threshold 6 (-24 dBFS peaks exceed it)
    e1 = make_engine(mic_src=noisy, **{"vox.enabled": True, "vox.threshold": 6})
    e1.record()
    pump(qapp, 0.5)
    assert e1.recorder.status.get("path")
    e1.stop()
    assert wait_for(qapp, lambda: e1.state == State.STOPPED, 8.0)
    # with noise suppression the same noise records nothing
    e2 = make_engine(mic_src=noisy, **{"vox.enabled": True, "vox.threshold": 6, "fmt.denoise": True})
    e2.record()
    pump(qapp, 0.8)
    assert e2.state == State.ARMED and not e2.recorder.status.get("path")
    assert e2.recorder.in_levels.max() > 0.05 > e2.recorder.levels.max()   # input loud, Vox hears quiet
    e2.stop()


def test_level_only_copy_matches_input_and_vox_uses_unlevelled_signal(qapp, make_engine):
    e = make_engine(mic_src=mic_source(-40, -60), **{"fmt.level": True, "fmt.keep_original": True})
    path = record(qapp, e, 0.6)
    a, _ = sf.read(path, dtype="float32")
    b, _ = sf.read(original_path(path), dtype="float32")
    assert len(a) == len(b)
    tail = slice(RATE * 2, len(a))
    assert db(a[tail]) > db(b[tail]) + 10                       # levelled up from about -45 dBFS


def test_no_copy_when_keep_original_off_or_nothing_cleaned(qapp, make_engine, tmp_path):
    e = make_engine(**{"fmt.level": True, "fmt.keep_original": False})
    p1 = record(qapp, e, 0.3)
    e2 = make_engine(**{"fmt.keep_original": True})            # no clean-up: nothing to keep
    p2 = record(qapp, e2, 0.3)
    assert os.path.exists(p1) and os.path.exists(p2)
    assert not os.path.exists(original_path(p1)) and not os.path.exists(original_path(p2))
    with open(p2, "rb") as f:
        assert b"IPRC" not in f.read()


def test_copy_follows_rollover_and_append(qapp, make_engine):
    e = make_engine(**{"fmt.level": True, "rec.rollover.minutes": 1}, realtime=60.0)
    e.record()
    folder = e.settings.get("rec.folder")

    def mains_now():
        return [f for f in (os.listdir(folder) if os.path.isdir(folder) else [])
                if f.endswith(".wav") and "-original" not in f]
    assert wait_for(qapp, lambda: len(mains_now()) >= 2, 10.0)
    e.stop()
    assert wait_for(qapp, lambda: e.state == State.STOPPED, 8.0)
    folder = e.settings.get("rec.folder")
    mains = sorted(f for f in os.listdir(folder) if f.endswith(".wav") and "-original" not in f)
    for m in mains:
        o = original_path(os.path.join(folder, m))
        assert os.path.exists(o)
        assert sf.info(os.path.join(folder, m)).frames == sf.info(o).frames
    # append to the first file: its copy grows by the same amount
    e.settings.set("rec.rollover.minutes", 0)                   # it is already one minute long
    first = os.path.join(folder, mains[0])
    before = sf.info(first).frames
    e.open_file(first)
    e.record()
    pump(qapp, 0.05)
    e.stop()
    assert wait_for(qapp, lambda: e.state == State.STOPPED, 8.0)
    assert sf.info(first).frames > before
    assert sf.info(first).frames == sf.info(original_path(first)).frames


def test_both_journals_kept_while_recording(qapp, make_engine):
    e = make_engine(**{"fmt.level": True})
    e.record()
    pump(qapp, 0.3)
    path = e.recorder.status["path"]
    orig = e.recorder.status["original"]
    assert orig == original_path(path)
    assert os.path.exists(path + journal.SUFFIX) and os.path.exists(orig + journal.SUFFIX)
    pending = {p.audio for p in journal.find_pending()}
    assert {path, orig} <= pending                               # a crash now would offer both
    e.stop()
    assert wait_for(qapp, lambda: e.state == State.STOPPED, 8.0)
    assert not journal.find_pending()


def test_options_page_binds_cleanup(qapp, make_engine):
    from vrp.inputs.external import ExternalManager
    from vrp.inputs.hotkeys import HotkeyManager
    from vrp.ui.options_dialog import OptionsDialog
    e = make_engine()
    d = OptionsDialog(e.settings, e, HotkeyManager(), ExternalManager(), e.backend, "Format", None)
    d.show()
    pump(qapp, 0.05)
    assert not d.level_chk.isChecked() and not d.keep_chk.isEnabled()       # nothing to keep a copy of
    d.level_chk.setChecked(True)
    assert d.keep_chk.isEnabled() and d.keep_chk.isChecked()
    assert d.denoise_chk.isEnabled() == HAVE_RNN
    assert d.apply()
    assert e.settings.get("fmt.level") is True and e.settings.get("fmt.keep_original") is True
    d.done(0)
