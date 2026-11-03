import os

import pytest

from conftest import pump, wait_for
from vrp.core.audio_backend import FakeBackend, scripted_source, tone_bursts
from vrp.core.engine import Engine, State
from vrp.settings import Settings


@pytest.fixture
def make_engine(qapp, tmp_path):
    engines = []

    def make(source=None, realtime=8.0, **settings):
        s = Settings()
        s.update({"rec.folder": str(tmp_path / "rec"), "rec.fallback_folder": str(tmp_path / "fb"),
                  "vox.enabled": False, **settings}, save=False)
        be = FakeBackend(source or tone_bursts(), realtime=realtime)
        e = Engine(s, be)
        e.start()
        engines.append(e)
        return e
    yield make
    for e in engines:
        e.shutdown()


def test_tc01_record_stop_play(qapp, make_engine):                       # TC-01
    e = make_engine(realtime=4.0)
    e.record()
    assert e.state == State.RECORDING
    pump(qapp, 2.5)                                  # 10 s of audio at 4x
    e.stop()
    assert wait_for(qapp, lambda: e.state == State.STOPPED)
    assert os.path.basename(e.file.path).endswith("_1.mp3")
    assert abs(e.file.duration - 10) < 0.6
    e.play_pause()
    assert e.state == State.PLAYING
    pump(qapp, 0.4)
    assert e.position > 0
    e.stop()
    assert e.state == State.STOPPED


def test_pause_resume_makes_segments_and_backspace(qapp, make_engine):
    e = make_engine(realtime=8.0)
    e.record()
    pump(qapp, 0.4)
    e.record()                                                # pause
    assert e.state == State.REC_PAUSED
    pump(qapp, 0.2)
    e.record()                                                # resume
    pump(qapp, 0.4)
    e.stop()
    assert wait_for(qapp, lambda: e.state == State.STOPPED)
    assert len(e.file.segments) == 2
    e.seek(5 * e.file.rate)
    e.play_pause()
    pump(qapp, 0.05)
    e.play_pause()                                            # pause
    p = e.position
    e.play_pause()                                            # resume: back 2 s (FR-PLAY-004)
    assert e.position <= p - 1.9 * e.file.rate


def test_append_by_default(qapp, make_engine):                           # FR-FILE-006
    e = make_engine(realtime=8.0, **{"fmt.codec": "wav"})
    e.record()
    pump(qapp, 0.3)
    e.stop()
    assert wait_for(qapp, lambda: e.state == State.STOPPED)
    first = e.file.path
    frames = e.file.frames
    e.record()
    pump(qapp, 0.3)
    e.stop()
    assert wait_for(qapp, lambda: e.state == State.STOPPED)
    assert e.file.path == first and e.file.frames > frames and len(e.file.segments) == 2


def test_vox_new_file_mode(qapp, make_engine):                           # FR-VOX-009
    src = scripted_source([(0.5, 1.5, 0.3), (3.0, 4.0, 0.3), (5.5, 6.5, 0.3)], total_s=60)
    e = make_engine(source=src, realtime=8.0, **{"vox.enabled": True, "vox.mode": "file", "vox.hang_s": 0.5,
                                                 "fmt.codec": "wav"})
    e.record()
    assert e.state == State.ARMED
    pump(qapp, 1.2)
    e.stop()
    assert wait_for(qapp, lambda: e.state in (State.STOPPED, State.IDLE))
    files = sorted(os.listdir(e.settings.recordings_folder()))
    assert len([f for f in files if f.endswith(".wav")]) == 3


def test_stop_while_armed_creates_no_file(qapp, make_engine):           # FR-VOX-011
    e = make_engine(source=scripted_source([]), realtime=8.0, **{"vox.enabled": True})
    e.record()
    pump(qapp, 0.4)
    e.stop()
    assert wait_for(qapp, lambda: e.state == State.IDLE)
    assert not os.path.exists(e.settings.recordings_folder()) or not os.listdir(e.settings.recordings_folder())


def test_rollover_loses_no_samples(qapp, make_engine):                  # TC-05, FR-FILE-007
    import numpy as np
    import soundfile as sf

    def counter(pos, n, rate, ch):            # each sample encodes its index (mod 1000)
        idx = (pos + np.arange(n)) % 1000
        return np.repeat((idx / 2000.0)[:, None], ch, axis=1).astype(np.float32)
    e = make_engine(source=counter, realtime=20.0, **{"fmt.codec": "wav", "rec.rollover.minutes": 1})
    e.record()
    pump(qapp, 4.0)                                           # ~80 s of audio
    e.stop()
    assert wait_for(qapp, lambda: e.state == State.STOPPED)
    folder = e.settings.recordings_folder()
    files = sorted((p for p in os.listdir(folder) if p.endswith(".wav")),
                   key=lambda p: os.path.getmtime(os.path.join(folder, p)))
    assert len(files) == 2
    data = np.concatenate([sf.read(os.path.join(folder, f), dtype="float32")[0] for f in files])
    v = np.rint(data * 2000).astype(int)
    steps = np.diff(v) % 1000
    assert np.all(steps == 1), "a sample was lost or repeated at a file boundary"


def test_midnight_rollover(qapp, make_engine, monkeypatch):            # TC-05
    import datetime as dt

    import vrp.core.recorder as rec_mod
    base = dt.datetime.now().replace(hour=23, minute=59, second=58, microsecond=0).timestamp()
    real_time = rec_mod.time.time
    start = real_time()
    monkeypatch.setattr(rec_mod.time, "time", lambda: base + (real_time() - start))
    e = make_engine(source=scripted_source([(0, 100, 0.2)]), realtime=4.0,
                    **{"fmt.codec": "wav", "rec.rollover.midnight": True})
    e.record()
    pump(qapp, 1.5)                                           # ~6 s of audio, crossing midnight
    e.stop()
    assert wait_for(qapp, lambda: e.state == State.STOPPED)
    files = [p for p in os.listdir(e.settings.recordings_folder()) if p.endswith(".wav")]
    assert len(files) == 2


def test_tc13_device_lost_and_back(qapp, make_engine):                  # TC-13, FR-DEV-007
    src = scripted_source([(0, 100, 0.2)], total_s=1.0)       # the "device" stops after 1 s
    e = make_engine(source=src, realtime=1.0, **{"fmt.codec": "wav"})
    notes = []
    e.notify.connect(lambda t, m: notes.append(t))
    e.record()
    assert wait_for(qapp, lambda: "Recording resumed" in notes, timeout=12)
    pump(qapp, 0.5)
    e.stop()
    assert wait_for(qapp, lambda: e.state == State.STOPPED)
    assert len(e.file.segments) >= 2


def test_selection_snaps_for_mp3_and_extract(qapp, make_engine):
    e = make_engine(realtime=8.0)
    e.record()
    pump(qapp, 0.6)
    e.stop()
    assert wait_for(qapp, lambda: e.state == State.STOPPED)
    e.set_selection(1000, 20000)
    a, b = e.selection
    assert (a + 529) % 576 == 0 and (b + 529) % 576 == 0
    done = []
    e.editFinished.connect(lambda label, status, msg: done.append(status))
    e.edit_extract()
    assert wait_for(qapp, lambda: done)
    assert done[0] == "ok"


def test_scan_hold_moves_position(qapp, make_engine):
    e = make_engine(realtime=8.0, **{"fmt.codec": "wav", "play.audible_scan": False})
    e.record()
    pump(qapp, 1.0)
    e.stop()
    assert wait_for(qapp, lambda: e.state == State.STOPPED)
    e.seek(0)
    e.ffwd(True)
    pump(qapp, 0.8)
    assert e.state == State.SEEKING
    e.ffwd(False)
    assert e.state == State.STOPPED and e.position > 0
    p = e.position
    e.rewind(True)
    e.rewind(False)                                           # a tap jumps back 5 s
    assert e.position == max(0, p - 5 * e.file.rate)


def test_player_prerolls_mp3_seeks(qapp, make_engine):
    e = make_engine(realtime=8.0)
    e.record()
    pump(qapp, 0.4)
    e.stop()
    assert wait_for(qapp, lambda: e.state == State.STOPPED)
    e.play_pause()
    assert e.player._seek_preroll > 0
    e.stop()
