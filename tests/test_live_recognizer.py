"""Phrase-by-phrase recognition, the worker process and the main-process service (1.1.0).

The speech engine is replaced by tests/fake_speech.py; test_live_e2e.py runs the real one.
"""
import os
import time

import numpy as np
import pytest

from fake_speech import SR, FakeVad, InProcWorker, build, silence, tone
from vrp.transcribe.live import LiveRecognizer
from vrp.transcribe.retention import AUDIO, TEXT, Retention
from vrp.transcribe.service import TranscriptionService, options_from_settings
from vrp.transcribe.transcript import BOOKMARK, OFF

W0 = 1795800000.0


def recog(**kw):
    decode, make_vad = build({})
    return LiveRecognizer(decode, make_vad, **kw)


def feed(lr, x, sid="mix", key="a.mp3", file_s=0.0, wall=W0, block=1600):
    out = []
    for i in range(0, x.size, block):
        out += lr.audio(sid, key, x[i:i + block], file_s + i / SR, wall + i / SR)
    return out


# ------------------------------------------------------------------ recogniser
def test_final_phrase_times_and_partials():
    lr = recog()
    x = np.concatenate([silence(1.0), tone(2.0), silence(1.0)])
    ev = feed(lr, x, file_s=10.0)
    finals = [e for e in ev if e[0] == "final"]
    partials = [e for e in ev if e[0] == "partial"]
    assert len(finals) == 1
    _, sid, key, text, lang, fs, fe, w0, w1 = finals[0]
    assert (sid, key, lang) == ("mix", "a.mp3", "en") and text.startswith("phrase of 2.0")
    assert fs == pytest.approx(11.0, abs=0.04) and fe == pytest.approx(13.0, abs=0.04)
    assert w0 == pytest.approx(W0 + 1.0, abs=0.04) and w1 - w0 == pytest.approx(2.0, abs=0.04)
    # about one partial a second while the phrase is spoken, timed at the phrase start
    assert 1 <= len(partials) <= 2 and all(p[4] == pytest.approx(11.0, abs=0.04) for p in partials)


def test_no_partials_when_off_or_behind():
    for kw, behind in (({"partial": False}, False), ({}, True)):
        lr = recog(**kw)
        lr.behind = behind
        ev = feed(lr, np.concatenate([tone(3.0), silence(1.0)]))
        assert [e[0] for e in ev] == ["final"]


def test_foreign_partial_is_not_shown():
    """A first syllable is sometimes guessed as Japanese or Korean: such a partial is not shown."""
    lr = LiveRecognizer(lambda s: ("こんにちは", "ja"), FakeVad)
    ev = feed(lr, np.concatenate([tone(2.0), silence(1.0)]))
    assert not [e for e in ev if e[0] == "partial" and e[3]]
    assert [e[3] for e in ev if e[0] == "final"] == ["こんにちは"]


def test_end_finishes_phrase_in_progress():
    lr = recog()
    feed(lr, tone(1.5))                                   # still speaking when the file closes
    ev = lr.end("mix", "a.mp3")
    assert [e[0] for e in ev] == ["final", "ended"]
    assert lr.keys() == [] and lr.end("mix", "a.mp3") == [("ended", "mix", "a.mp3")]


def test_flush_at_pause_keeps_positions_right():
    """Off the record: the phrase before the pause is finished; later phrases keep correct times."""
    lr = recog()
    feed(lr, np.concatenate([silence(0.5), tone(1.5)]))
    ev = lr.flush("mix", "a.mp3")
    assert [e[0] for e in ev] == ["final"] and ev[0][5] == pytest.approx(0.5, abs=0.04)
    # after the pause the file goes on at 2.0 s (nothing was written while paused); wall time jumped 60 s
    ev = feed(lr, np.concatenate([silence(0.5), tone(1.0), silence(1.0)]), file_s=2.0, wall=W0 + 62.0)
    finals = [e for e in ev if e[0] == "final"]
    assert len(finals) == 1
    assert finals[0][5] == pytest.approx(2.5, abs=0.04)          # file position
    assert finals[0][7] == pytest.approx(W0 + 62.5, abs=0.04)    # wall clock
    assert lr.flush("me", "unknown") == []


def test_two_streams_are_independent():
    lr = recog()
    a = feed(lr, np.concatenate([tone(1.0), silence(1.0)]), sid="me")
    b = feed(lr, np.concatenate([silence(0.5), tone(1.0, amp=0.5), silence(1.0)]), sid="others")
    assert [e[1] for e in a if e[0] == "final"] == ["me"]
    fb = [e for e in b if e[0] == "final"][0]
    assert fb[1] == "others" and fb[4] == "yue" and fb[5] == pytest.approx(0.5, abs=0.04)


def test_anchors_are_trimmed_on_long_recordings():
    lr = recog(partial=False)
    for k in range(300):                                   # 300 s in 1 s blocks
        lr.audio("mix", "a", silence(1.0), float(k), W0 + k)
    st = lr.streams[("mix", "a")]
    assert len(st.anchors) <= 182
    assert st.where(st.pos - SR)[0] == pytest.approx(299.0)


# ------------------------------------------------------------------ service (fake in-process worker)
class Pack:
    model = tokens = vad = "x"


OPTS = {"language": "auto", "script": "hk", "names": [], "mask": False, "save": "audio_text", "keep_days": 7,
        "delete_days": 0, "txt": True, "srt": True, "partial": True, "threads": 2, "priority": "below",
        "encrypt": False}


def service(tmp_path, **opts):
    ret = Retention(str(tmp_path / "retention.json"))
    sv = TranscriptionService(ret, InProcWorker)
    sv.start_session({**OPTS, **opts}, Pack(), opts.pop("split", False) if "split" in opts else False)
    return sv


def play(sv, path, x, rate=48000, file_s=0.0, wall=W0, block=4800, channels=1):
    """Hand blocks to the service as the recorder thread does (rate of the file, frames x channels)."""
    import soxr
    y = soxr.resample(x, SR, rate).astype(np.float32) if rate != SR else x
    if channels == 1:
        y = y[:, None]
    for i in range(0, y.shape[0], block):
        sv.tap(y[i:i + block], int(file_s * rate) + i, rate, wall + i / rate, path)
        sv.poll()


def test_service_audio_and_text_files(tmp_path):
    sv = service(tmp_path)
    path = str(tmp_path / "VOX-001.mp3")
    play(sv, path, np.concatenate([silence(0.5), tone(2.0), silence(1.0)]))
    assert sv.worker.ready and [e.kind for e in sv.entries] == ["text"]
    assert sv.entries[0].speaker is None and sv.entries[0].pos == pytest.approx(0.5, abs=0.05)
    assert os.path.exists(str(tmp_path / "VOX-001.txt")) and sv.last_txt.endswith("VOX-001.txt")
    sv.tap_close(path)
    sv.poll()
    assert not sv.writers and os.path.exists(str(tmp_path / "VOX-001.srt"))
    txt = open(tmp_path / "VOX-001.txt", encoding="utf-8").read()
    assert "Recording: VOX-001.mp3" in txt and "phrase of 2.0 seconds" in txt


def test_service_split_channels_me_and_others(tmp_path):
    ret = Retention(str(tmp_path / "r.json"))
    sv = TranscriptionService(ret, InProcWorker)
    sv.start_session(dict(OPTS), Pack(), True)
    me = np.concatenate([tone(1.0), silence(3.0)])
    others = np.concatenate([silence(2.0), tone(1.0, amp=0.5), silence(1.0)])
    stereo = np.stack([me, others], axis=1)
    path = str(tmp_path / "call.wav")
    for i in range(0, stereo.shape[0], 1600):
        sv.tap(stereo[i:i + 1600], i, SR, W0 + i / SR, path)
        sv.poll()
    assert [(e.speaker, e.lang) for e in sv.entries] == [("me", "en"), ("others", "yue")]
    assert sv.entries[1].text == "呢個係唔係1.0秒"      # Traditional (Hong Kong), Cantonese fix applied
    sv.tap_close(path)
    sv.poll()
    txt = open(tmp_path / "call.txt", encoding="utf-8").read()
    assert "] Me: phrase of" in txt and "] Others: 呢個係" in txt


def test_text_only_never_overwrites_and_has_no_srt(tmp_path):
    (tmp_path / "rec.txt").write_text("an earlier transcript", encoding="utf-8")
    sv = service(tmp_path, save="text_only")
    path = str(tmp_path / "rec.txt")                    # Text only: the recorder's "file" is the .txt name
    play(sv, path, np.concatenate([tone(1.0), silence(1.0)]))
    sv.tap_close(path)
    sv.poll()
    assert (tmp_path / "rec.txt").read_text(encoding="utf-8") == "an earlier transcript"
    new = (tmp_path / "rec-2.txt").read_text(encoding="utf-8")
    assert "Text only: no sound was saved" in new and "phrase of" in new
    assert not list(tmp_path.glob("*.srt")) and sv.entries[0].pos is None


def test_bookmark_during_a_phrase_goes_after_it(tmp_path):
    """Regression (1.1.0 development): a phrase finished after a bookmark set while it was spoken was
    shown and saved after the bookmark, with an earlier time."""
    sv = service(tmp_path)
    path = str(tmp_path / "b.mp3")
    play(sv, path, np.concatenate([silence(0.5), tone(1.0)]), wall=time.time() - 1.5)
    bm = sv.bookmark()                                  # the phrase is still being spoken
    assert sv.held and sv.entries[-1] is bm
    play(sv, path, silence(1.0), file_s=1.5, wall=time.time())
    assert [e.kind for e in sv.entries] == ["text", BOOKMARK] and sv.reordered == 1
    sv.tap_close(path)
    sv.poll()
    lines = (tmp_path / "b.txt").read_text(encoding="utf-8").splitlines()
    assert lines[-2].endswith("phrase of 1.0 seconds") and lines[-1].endswith("* Bookmark")


def test_bookmark_in_silence_is_written_after_a_short_wait(tmp_path):
    sv = service(tmp_path)
    path = str(tmp_path / "s.mp3")
    play(sv, path, silence(0.5), wall=time.time())
    bm = sv.bookmark()
    sv._release_due(bm.wall + 1.0)
    assert sv.held
    sv._release_due(bm.wall + 2.5)
    assert not sv.held
    assert (tmp_path / "s.txt").read_text(encoding="utf-8").rstrip().endswith("* Bookmark")


def test_off_the_record_flushes_and_marks(tmp_path):
    sv = service(tmp_path)
    path = str(tmp_path / "o.mp3")
    play(sv, path, np.concatenate([silence(0.3), tone(1.0)]))
    sv.paused()
    assert "flush" in sv.worker.sent and sv.off_record
    sv.poll()
    assert [e.kind for e in sv.entries] == ["text"]       # the phrase before the pause is finished at once
    sv.resumed()
    assert not sv.off_record and sv.entries[-1].kind == OFF
    sv.resumed()                                           # a second resume adds nothing
    assert [e.kind for e in sv.entries] == ["text", OFF]


def test_text_check_registers_audio_and_delete_days_registers_text(tmp_path):
    sv = service(tmp_path, save="text_check", keep_days=3, delete_days=30)
    path = str(tmp_path / "k.mp3")
    open(path, "wb").close()
    play(sv, path, np.concatenate([tone(1.0), silence(1.0)]))
    sv.audio_closed(path)
    sv.tap_close(path)
    sv.poll()
    r = sv.retention
    assert r.find(path, AUDIO) and r.find(str(tmp_path / "k.txt"), TEXT) and r.find(str(tmp_path / "k.srt"), TEXT)


def test_worker_crash_is_reported_and_recording_goes_on(tmp_path):
    sv = service(tmp_path)
    sv.worker.dead = True
    assert sv.poll() and not sv.active
    assert sv.take_messages() == [("warning", sv.error)] and "closed unexpectedly" in sv.error
    sv.tap(np.zeros((480, 1), np.float32), 0, 48000, W0, "x.mp3")       # no longer sent anywhere


def test_prewarm_reuses_worker_and_release_frees_it(tmp_path):
    ret = Retention(str(tmp_path / "r.json"))
    made = []

    def factory(cfg):
        w = InProcWorker(cfg)
        made.append(w)
        return w
    sv = TranscriptionService(ret, factory)
    sv.prewarm(dict(OPTS), Pack())
    sv.start_session(dict(OPTS), Pack(), False)
    assert len(made) == 1                                   # loaded once, before the recording
    sv.start_session({**OPTS, "language": "yue"}, Pack(), False)
    assert len(made) == 2                                   # another language: a new engine
    sv.stop_session()
    sv.release()
    assert sv.worker is None


def test_options_from_settings():
    from vrp.settings import Settings
    s = Settings()
    o = options_from_settings(s)
    assert o["save"] == "text_check" and o["language"] == "auto" and o["srt"] is True and o["threads"] == 2


def test_worker_process_with_fake_engine():
    """The real worker process (spawned) with the fake engine: ready, phrases, lag, quit."""
    from vrp.transcribe.worker import WorkerHandle
    w = WorkerHandle({"_builder": build, "partial": False, "priority": "normal"})
    w.start()
    try:
        x = (np.concatenate([tone(1.0), silence(1.0)]) * 32767).astype(np.int16)
        now = time.time()
        w.send(("audio", "mix", "k", x, 0.0, now - 2.0))
        w.send(("flush", "mix", "k"))
        w.send(("end", "mix", "k"))
        got = []
        t0 = time.time()
        while time.time() - t0 < 30 and not any(e[0] == "ended" for e in got):
            got += w.poll()
            time.sleep(0.02)
        kinds = [e[0] for e in got if e[0] != "lag"]
        assert w.ready and kinds == ["ready", "final", "ended"]
    finally:
        w.stop()
    assert not w.alive()


def test_encrypt_on_close_and_pending_bookmark(tmp_path):
    ret = Retention(str(tmp_path / "r.json"))
    done = []
    sv = TranscriptionService(ret, InProcWorker, encrypt=lambda p: (done.append(p) or (False, "not NTFS")))
    sv.start_session({**OPTS, "encrypt": True}, Pack(), False)
    sv.bookmark()                                         # before any audio (Vox waiting): kept until a file
    assert sv.pending == [] and sv.held                    # held first, then pending once released
    sv._release_due(time.time() + 3)
    assert len(sv.pending) == 1
    path = str(tmp_path / "e.mp3")
    play(sv, path, np.concatenate([tone(1.0), silence(1.0)]))
    assert not sv.pending
    sv.tap_close(path)
    sv.poll()
    assert done == [str(tmp_path / "e.txt"), str(tmp_path / "e.srt")]     # a failure is logged, not fatal
    lines = [x for x in (tmp_path / "e.txt").read_text(encoding="utf-8").splitlines() if x.startswith("[")]
    assert lines[0].endswith("* Bookmark") and "phrase of" in lines[1]


def test_transcript_folder_not_writable(tmp_path):
    sv = service(tmp_path)
    blocker = tmp_path / "blocked"
    blocker.write_text("a file where a folder should be")
    play(sv, str(blocker / "x.mp3"), np.concatenate([tone(1.0), silence(1.0)]))
    assert sv.entries and "could not be saved" in sv.error
    assert len(sv.take_messages()) == 1                   # said once, not for every phrase
    play(sv, str(blocker / "x.mp3"), np.concatenate([tone(1.0), silence(1.0)]), file_s=2.0)
    assert len(sv.entries) == 2 and sv.take_messages() == []


def test_words_in_progress_are_masked_too(tmp_path):
    sv = service(tmp_path, mask=True)
    sv._event(("partial", "mix", "k", "call me on 9123 4567", 0.0, W0))
    assert sv.partials["mix"][0] == "call me on [phone number]"
