"""Recording sources (v1.0.2): microphone, computer audio (loopback), or both; live tick/untick."""
import numpy as np
import pytest

from conftest import pump, wait_for
from vrp.core import dsp
from vrp.core.audio_backend import FakeBackend, scripted_source
from vrp.core.engine import Engine, State
from vrp.core.loopback import music_bursts
from vrp.settings import Settings

RATE = 16000
MIC_HZ, PC_HZ = 440.0, 1000.0


def pc_tone(level=0.2):
    """Computer audio that plays all the time (stereo, 48 kHz from the fake loopback)."""
    def src(pos, n, rate, ch):
        t = (pos + np.arange(n)) / rate
        x = level * np.sin(2 * np.pi * PC_HZ * t)
        return np.repeat(x[:, None], ch, axis=1).astype(np.float32)
    return src


def tone_level(x: np.ndarray, freq: float) -> float:
    """Amplitude of one sine component (single-bin DFT)."""
    if x.ndim > 1:
        x = x.mean(axis=1)
    t = np.arange(len(x)) / RATE
    return float(2 * abs(np.sum(x * np.exp(-2j * np.pi * freq * t))) / max(1, len(x)))


def window(x: np.ndarray, a: float, b: float) -> np.ndarray:
    n = len(x)
    i, j = int(a * RATE) if a >= 0 else n + int(a * RATE), int(b * RATE) if b > 0 else n + int(b * RATE)
    return x[max(0, i):max(0, min(n, j))]


@pytest.fixture
def make_engine(qapp, tmp_path):
    engines = []

    def make(mic=True, system=False, mic_source=None, pc_source=None, realtime=8.0, **settings):
        s = Settings()
        s.update({"rec.folder": str(tmp_path / "rec"), "rec.fallback_folder": str(tmp_path / "fb"),
                  "vox.enabled": False, "fmt.codec": "wav", "fmt.rate": RATE, "fmt.channels": 1,
                  "rec.src_mic": mic, "rec.src_system": system, **settings}, save=False)
        be = FakeBackend(mic_source or scripted_source([(0, 1e9, 0.2)], freq=MIC_HZ), realtime=realtime)
        be.loopback_source = pc_source or pc_tone()
        e = Engine(s, be)
        e.start()
        engines.append(e)
        return e
    yield make
    for e in engines:
        e.shutdown()


def recorded(qapp, e) -> np.ndarray:
    import soundfile as sf
    e.stop()
    assert wait_for(qapp, lambda: e.state == State.STOPPED)
    data, rate = sf.read(e.file.path, dtype="float32")
    assert rate == RATE
    return data


def test_both_sources_are_mixed(qapp, make_engine):
    e = make_engine(mic=True, system=True)
    e.record()
    assert e.state == State.RECORDING
    assert e.recorder.mic_open and e.recorder.system_open
    pump(qapp, 0.5)                                        # ~4 s of audio
    assert max(e.system_level_percent() or [0]) > 5
    assert max(e.level_percent() or [0]) > 5
    x = recorded(qapp, e)
    mid = window(x, 1.0, 3.0)
    assert tone_level(mid, MIC_HZ) > 0.12 and tone_level(mid, PC_HZ) > 0.12
    assert e.file.channels == 1


def test_tick_and_untick_while_recording(qapp, make_engine):
    e = make_engine(mic=True, system=False)
    e.record()
    pump(qapp, 0.3)                                        # mic only
    e.settings.set("rec.src_system", True, save=False)     # tick Computer
    assert e.recorder.system_open
    pump(qapp, 0.3)                                        # both
    e.settings.set("rec.src_mic", False, save=False)       # untick Mic
    assert not e.recorder.mic_open
    pump(qapp, 0.3)                                        # computer audio only (wall clock)
    assert e.state == State.RECORDING
    x = recorded(qapp, e)
    dur = len(x) / RATE
    assert dur > 5.0
    first, last = window(x, 0.2, 1.5), window(x, -1.6, -0.3)
    assert tone_level(first, MIC_HZ) > 0.12 and tone_level(first, PC_HZ) < 0.02
    assert tone_level(last, PC_HZ) > 0.12 and tone_level(last, MIC_HZ) < 0.02
    both = [tone_level(window(x, a, a + 0.5), MIC_HZ) > 0.1 and tone_level(window(x, a, a + 0.5), PC_HZ) > 0.1
            for a in np.arange(1.5, dur - 2.0, 0.25)]
    assert any(both)


def test_computer_audio_only_pads_silence_by_the_clock(qapp, make_engine):
    e = make_engine(mic=False, system=True, pc_source=music_bursts(level=0.25, on_s=2.0, off_s=2.0, freq=PC_HZ))
    e.record()
    assert e.state == State.RECORDING
    assert not e.recorder.mic_open and e.recorder.system_open
    pump(qapp, 0.8)                                        # ~6.4 s of wall-clock audio at 8x
    x = recorded(qapp, e)
    dur = len(x) / RATE
    assert 4.5 < dur < 8.5                                 # the file follows the clock, gaps included
    assert tone_level(window(x, 0.5, 1.5), PC_HZ) > 0.1     # music playing (left channel, mixed to mono)
    quiet = window(x, 2.6, 3.6)
    assert len(quiet) and float(np.max(np.abs(quiet))) < 0.01  # nothing playing: silence, not a gap


def test_nothing_ticked_refuses_to_start(qapp, make_engine):
    e = make_engine(mic=False, system=False)
    said = []
    e.message.connect(lambda lvl, title, text: said.append(title))
    e.record()
    assert e.state == State.IDLE
    assert said and said[-1] == "Nothing to record"


def test_loopback_failure(qapp, make_engine):
    e = make_engine(mic=True, system=True)
    e.backend.fail_loopback = True
    said = []
    e.message.connect(lambda lvl, title, text: said.append((lvl, title)))
    e.record()                                             # microphone still records
    assert e.state == State.RECORDING and e.recorder.mic_open and not e.recorder.system_open
    assert ("warning", "Computer audio not available") in said
    assert wait_for(qapp, lambda: not e.settings.get("rec.src_system"))   # unticked for the user
    e.stop()
    assert wait_for(qapp, lambda: e.state != State.RECORDING)

    e2 = make_engine(mic=False, system=True)
    e2.backend.fail_loopback = True
    e2.record()                                            # nothing else to record: refused
    assert e2.state == State.IDLE and not e2.recorder.input_open


def test_hotkey_actions_toggle_sources(qapp, make_engine):
    e = make_engine(mic=True, system=False)
    e.do("source_system")
    assert e.settings.get("rec.src_system") is True
    e.do("source_mic")
    assert e.settings.get("rec.src_mic") is False
    st = e.status_dict()
    assert st["sources"] == {"microphone": False, "computer_audio": True}
    assert st["version"]


def test_mic_lost_keeps_recording_computer_audio(qapp, make_engine):
    # the microphone stops delivering after 1 s of audio; computer audio continues
    e = make_engine(mic=True, system=True, mic_source=scripted_source([(0, 1e9, 0.2)], total_s=1.0, freq=MIC_HZ))
    e.record()
    assert wait_for(qapp, lambda: e._device_lost_at is not None, timeout=5.0)
    pump(qapp, 0.6)                                        # wall clock drives the file meanwhile
    assert e.state == State.RECORDING
    x = recorded(qapp, e)
    assert len(x) / RATE > 3.0                             # far more than the 1 s from the microphone
    assert tone_level(window(x, -1.5, -0.3), PC_HZ) > 0.1


def test_to_channels_downmix():
    six = np.zeros((4, 6), np.float32)
    six[:, 2] = 1.0                                        # dialogue in the centre channel
    st = dsp.to_channels(six, 2)
    assert st.shape == (4, 2) and np.allclose(st, 0.7071, atol=1e-3)
    mono = dsp.to_channels(np.ones((3, 2), np.float32) * [0.2, 0.4], 1)
    assert mono.shape == (3, 1) and np.allclose(mono, 0.3)
    assert dsp.to_channels(np.ones((5, 1), np.float32), 2).shape == (5, 2)
