"""Separate channels (v1.0.3): microphone on the left, computer audio on the right, never summed."""
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest

from conftest import pump, wait_for
from vrp.core import editor
from vrp.core.audio_backend import FakeBackend, scripted_source
from vrp.core.audiofile import probe
from vrp.core.engine import Engine, State
from vrp.core.model import SPLIT_ROLES, FileModel, FormatSpec
from vrp.settings import Settings

ROOT = Path(__file__).resolve().parent.parent
RATE = 16000
MIC_HZ, PC_HZ = 440.0, 1000.0


def pc_tone(level=0.2):
    def src(pos, n, rate, ch):
        t = (pos + np.arange(n)) / rate
        x = level * np.sin(2 * np.pi * PC_HZ * t)
        return np.repeat(x[:, None], ch, axis=1).astype(np.float32)
    return src


def pc_silent(pos, n, rate, ch):
    return np.zeros((n, ch), np.float32)


def tone_level(x: np.ndarray, freq: float) -> float:
    """Amplitude of one sine component (single-bin DFT) of a mono signal."""
    t = np.arange(len(x)) / RATE
    return float(2 * abs(np.sum(x * np.exp(-2j * np.pi * freq * t))) / max(1, len(x)))


def window(x: np.ndarray, a: float, b: float) -> np.ndarray:
    n = len(x)
    i = int(a * RATE) if a >= 0 else n + int(a * RATE)
    j = int(b * RATE) if b > 0 else n + int(b * RATE)
    return x[max(0, i):max(0, min(n, j))]


def db(a: float, b: float) -> float:
    return 20 * np.log10(max(a, 1e-9) / max(b, 1e-9))


@pytest.fixture
def make_engine(qapp, tmp_path):
    engines = []

    def make(mic=True, system=True, mic_source=None, pc_source=None, **settings):
        s = Settings()
        s.update({"rec.folder": str(tmp_path / "rec"), "rec.fallback_folder": str(tmp_path / "fb"),
                  "vox.enabled": False, "fmt.codec": "wav", "fmt.rate": RATE, "fmt.channels": 2,
                  "fmt.layout": "split", "rec.src_mic": mic, "rec.src_system": system, **settings},
                 save=False)
        be = FakeBackend(mic_source or scripted_source([(0, 1e9, 0.2)], freq=MIC_HZ), realtime=8.0)
        be.loopback_source = pc_source or pc_tone()
        e = Engine(s, be)
        e.start()
        engines.append(e)
        return e
    yield make
    for e in engines:
        e.shutdown()


def record_for(qapp, e, seconds=0.5):
    e.record()
    assert e.state == State.RECORDING
    pump(qapp, seconds)
    e.stop()
    assert wait_for(qapp, lambda: e.state == State.STOPPED)
    return e.file.path


def read(path):
    import soundfile as sf
    data, rate = sf.read(path, dtype="float32", always_2d=True)
    assert rate == RATE
    return data


# ------------------------------------------------------------------ the format itself
def test_format_spec_layout():
    f = FormatSpec("wav", 16000, 1, layout="split").normalised()
    assert f.channels == 2 and f.split
    assert FormatSpec(layout="other").normalised().layout == "mix"
    assert FormatSpec.from_json({"codec": "wav", "channels": 2}).layout == "mix"       # 1.0.2 journal
    assert FormatSpec.from_json({"layout": "nonsense"}).layout == "mix"                # validated
    g = FormatSpec.from_json({"codec": "flac", "channels": 1, "layout": "split"})
    assert g.split and g.channels == 2
    assert "separate L/R" in FormatSpec("mp3", 16000, 2, layout="split").describe()
    assert "stereo" in FormatSpec("mp3", 16000, 2).describe()
    assert FormatSpec("mp3", 16000, 2, layout="split") != FormatSpec("mp3", 16000, 2)  # reopen on change


def test_settings_layout_is_validated():
    s = Settings()
    assert s.get("fmt.layout") == "mix"                    # default: 1.0.2 behaviour
    s.set("fmt.layout", "split", save=False)
    assert FormatSpec.from_settings(s).split and FormatSpec.from_settings(s).channels == 2
    s.set("fmt.layout", "both", save=False)
    assert s.get("fmt.layout") == "mix"


def test_file_model_layout_from_tags():
    base = dict(path="x.wav", codec="wav", rate=RATE, frames=0, size=0)
    assert FileModel(channels=2, tags={"ICHN": SPLIT_ROLES}, **base).layout == "split"
    assert FileModel(channels=2, tags={"VRP_CHANNELS": SPLIT_ROLES}, **base).layout == "split"
    assert FileModel(channels=2, tags={}, **base).layout == "mix"
    assert FileModel(channels=1, tags={"ICHN": SPLIT_ROLES}, **base).layout == "mix"   # mono cannot be split
    assert FileModel(channels=2, tags={"ICHN": "something else"}, **base).layout == "mix"
    assert "L/R" in FileModel(channels=2, tags={"ICHN": SPLIT_ROLES}, **base).sample_text()


# ------------------------------------------------------------------ recording
@pytest.mark.parametrize("codec,leak_db", [("wav", -80), ("flac", -80), ("mp3", -40)])
def test_mic_left_computer_right(qapp, make_engine, codec, leak_db):
    extra = {"fmt.mp3.kbps": 64} if codec == "mp3" else {}
    e = make_engine(**{"fmt.codec": codec, **extra})
    e.record()
    pump(qapp, 0.5)                                        # ~4 s of audio at 8x
    assert len(e.level_percent()) == 1 and len(e.system_level_percent()) == 1   # one bar per side
    e.stop()
    assert wait_for(qapp, lambda: e.state == State.STOPPED)
    x = read(e.file.path)
    assert x.shape[1] == 2
    mid = window(x, 1.0, 3.0)
    left, right = mid[:, 0], mid[:, 1]
    assert tone_level(left, MIC_HZ) > 0.12 and tone_level(right, PC_HZ) > 0.12
    assert db(tone_level(left, PC_HZ), tone_level(right, PC_HZ)) < leak_db       # computer audio not on the left
    assert db(tone_level(right, MIC_HZ), tone_level(left, MIC_HZ)) < leak_db     # microphone not on the right
    m = probe(e.file.path)
    assert m.layout == "split" and m.channels == 2 and "L/R" in m.sample_text()


def test_mixed_stereo_still_mixes(qapp, make_engine):
    e = make_engine(**{"fmt.layout": "mix"})
    x = read(record_for(qapp, e))
    mid = window(x, 1.0, 3.0)
    for c in (0, 1):                                       # both sources in both channels, as in 1.0.2
        assert tone_level(mid[:, c], MIC_HZ) > 0.12 and tone_level(mid[:, c], PC_HZ) > 0.12
    assert probe(e.file.path).layout == "mix"


@pytest.mark.parametrize("untick,silent_side,live_side,live_hz", [
    ("rec.src_mic", 0, 1, PC_HZ), ("rec.src_system", 1, 0, MIC_HZ)])
def test_unticked_source_records_silence_on_its_side(qapp, make_engine, untick, silent_side, live_side, live_hz):
    e = make_engine()
    e.record()
    pump(qapp, 0.3)
    e.settings.set(untick, False, save=False)
    pump(qapp, 0.4)
    assert e.state == State.RECORDING
    e.stop()
    assert wait_for(qapp, lambda: e.state == State.STOPPED)
    x = read(e.file.path)
    assert x.shape[1] == 2                                 # the channel count never changes mid-file
    last = window(x, -1.5, -0.3)
    assert float(np.max(np.abs(last[:, silent_side]))) < 1e-4
    assert tone_level(last[:, live_side], live_hz) > 0.12


@pytest.mark.parametrize("mic_level,pc", [(0.0, "tone"), (0.2, "silent")])
def test_vox_starts_from_either_side(qapp, make_engine, mic_level, pc):
    e = make_engine(**{"vox.enabled": True},
                    mic_source=scripted_source([(0, 1e9, mic_level)], freq=MIC_HZ),
                    pc_source=pc_tone() if pc == "tone" else pc_silent)
    e.record()
    assert wait_for(qapp, lambda: e.state == State.RECORDING, timeout=5.0)
    pump(qapp, 0.3)
    e.stop()
    assert wait_for(qapp, lambda: e.state == State.STOPPED)
    x = read(e.file.path)
    side, hz = (1, PC_HZ) if pc == "tone" else (0, MIC_HZ)
    assert tone_level(x[RATE // 2:, side], hz) > 0.1


def test_append_needs_the_same_layout(qapp, make_engine):
    e = make_engine(**{"rec.open_file_action": "append"})
    first = record_for(qapp, e, 0.3)
    again = record_for(qapp, e, 0.3)                       # same layout: appended as a new segment
    assert again == first and len(probe(first).segments) >= 2
    e.settings.update({"fmt.layout": "mix"}, save=False)   # mixed stereo: also 2 channels, but not compatible
    third = record_for(qapp, e, 0.3)
    assert third != first
    assert probe(first).layout == "split" and probe(third).layout == "mix"
    split_file = probe(first)
    assert not e._compatible(split_file, FormatSpec("wav", RATE, 2))
    assert e._compatible(split_file, FormatSpec("wav", RATE, 2, layout="split"))


# ------------------------------------------------------------------ tags and other programs
@pytest.mark.parametrize("codec", ["wav", "flac", "mp3"])
def test_tags_name_the_channels(qapp, make_engine, codec):
    e = make_engine(**{"fmt.codec": codec, "fmt.mp3.kbps": 64, "rec.comment": "Board call"})
    path = record_for(qapp, e, 0.3)
    m = probe(path)
    assert m.layout == "split"
    assert m.comment == "Board call"                       # the user's comment is untouched
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg not installed")
    out = subprocess.run([ffmpeg, "-hide_banner", "-i", path], capture_output=True, text=True).stderr
    assert SPLIT_ROLES in out                              # other programs can read the roles


def test_mixed_files_carry_no_roles(qapp, make_engine):
    e = make_engine(**{"fmt.layout": "mix", "fmt.codec": "mp3"})
    m = probe(record_for(qapp, e, 0.3))
    assert "VRP_CHANNELS" not in m.tags and m.layout == "mix"


# ------------------------------------------------------------------ edits
def test_edits_keep_or_drop_the_roles(qapp, make_engine, tmp_path):
    e = make_engine()
    src = probe(record_for(qapp, e, 0.4))
    clip = editor.extract(src, RATE // 2, RATE * 2, str(tmp_path / "clip.wav"))
    assert clip.layout == "split"                          # a copy of part of the file keeps L/R
    flac = editor.convert(src, str(tmp_path / "copy.flac"), FormatSpec("flac", RATE, 2))
    assert flac.layout == "split"
    mono = editor.convert(src, str(tmp_path / "mono.mp3"), FormatSpec("mp3", RATE, 1, mp3_kbps=32))
    assert mono.layout == "mix" and "VRP_CHANNELS" not in mono.tags
    y = read(str(tmp_path / "copy.flac"))
    assert tone_level(window(y, 1.0, 3.0)[:, 0], PC_HZ) < 1e-3   # still nothing of the computer on the left


def test_join_refuses_mixed_layouts(qapp, make_engine, tmp_path):
    e = make_engine(**{"rec.open_file_action": "new"})
    a = record_for(qapp, e, 0.3)
    b = record_for(qapp, e, 0.3)
    e.settings.update({"fmt.layout": "mix"}, save=False)
    c = record_for(qapp, e, 0.3)
    joined = editor.join([a, b], str(tmp_path / "ab.wav"))
    assert joined.layout == "split"
    with pytest.raises(editor.EditError, match="separate channels"):
        editor.join([a, c], str(tmp_path / "ac.wav"), convert=True)


# ------------------------------------------------------------------ command line and crash recovery
def test_command_line_layout(qapp, make_engine):
    import argparse

    from vrp import cli
    ns = argparse.Namespace(vox=None, device=None, format="wav", rate=None, channels=None, layout="split",
                            out=None, pattern=None, duration=None)
    assert cli.record_args(ns)["layout"] == "split"
    e = make_engine(**{"fmt.layout": "mix", "fmt.channels": 1})
    e._overrides = {"layout": "split"}
    assert e.fmt().split and e.fmt().channels == 2
    e._overrides = {"channels": 1}                         # --channels alone: an ordinary mono file
    e.settings.update({"fmt.layout": "split"}, save=False)
    assert e.fmt().layout == "mix" and e.fmt().channels == 1


@pytest.mark.skipif(sys.platform == "win32", reason="uses SIGKILL")
@pytest.mark.parametrize("codec", ["wav", "flac"])
def test_recovery_keeps_the_layout(tmp_path, isolated_config, codec):
    env = dict(os.environ, VRP_CONFIG_DIR=str(isolated_config), QT_QPA_PLATFORM="offscreen")
    (isolated_config / "settings.json").write_text(json.dumps({"schema_version": 1, "settings": {
        "rec.folder": str(tmp_path / "rec"), "vox.enabled": False, "general.first_run_done": True}}))
    p = subprocess.Popen([sys.executable, "-m", "vrp", "record", "--headless", "--fake-audio", "--format", codec,
                          "--layout", "split"], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.time() + 20
    recorded = 0.0
    while time.time() < deadline and recorded < 2.5:
        r = subprocess.run([sys.executable, "-m", "vrp", "status", "--json"], cwd=ROOT, env=env,
                           capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            recorded = json.loads(r.stdout)["duration_s"]
        time.sleep(0.2)
    p.send_signal(signal.SIGKILL)
    p.wait(10)
    from vrp.core import journal
    pending = journal.find_pending([str(tmp_path / "rec")])
    assert len(pending) == 1 and recorded >= 2.5
    assert pending[0].state["format"]["layout"] == "split"
    ok, msg = journal.recover(pending[0])
    assert ok, msg
    m = probe(pending[0].audio)
    assert m.channels == 2 and m.layout == "split"


# ------------------------------------------------------------------ what the windows draw
def test_format_form_offers_separate_and_raises_mp3_bitrate(qapp):
    from vrp.ui.dialogs import FormatDialog, FormatForm
    form = FormatForm(allow_split=True)
    form.set_spec(FormatSpec("mp3", 16000, 1, mp3_kbps=32))
    form.show()
    assert not form.split_note.isVisible()
    form.channels.setCurrentIndex(form.channels.findData("split"))
    pump(qapp, 0.02)
    spec = form.spec()
    assert spec.split and spec.channels == 2 and spec.mp3_kbps == 64      # raised once for two channels
    assert form.split_note.isVisible() and "raised to 64 kbit/s" in form.split_note.text()
    assert "headset" in form.split_note.text()
    form.kbps.setCurrentIndex(form.kbps.findData(32))     # the user may lower it again: warned, not blocked
    pump(qapp, 0.02)
    assert form.spec().mp3_kbps == 32 and "Below 64 kbit/s" in form.split_note.text()
    form.channels.setCurrentIndex(form.channels.findData(1))
    pump(qapp, 0.02)
    assert not form.split_note.isVisible() and form.spec().layout == "mix"
    loaded = FormatForm(allow_split=True)                  # a saved setting is shown as it is, not raised
    loaded.set_spec(FormatSpec("mp3", 16000, 2, mp3_kbps=48, layout="split"))
    assert loaded.channels.currentData() == "split" and loaded.spec().mp3_kbps == 48
    flac = FormatForm(allow_split=True)
    flac.set_spec(FormatSpec("flac", 16000, 2, layout="split"))
    flac.show()
    pump(qapp, 0.02)
    assert flac.split_note.isVisible() and "kbit/s" not in flac.split_note.text()
    conv = FormatDialog(FormatSpec("wav", 16000, 2), "Save Copy As")    # conversions cannot separate sources
    assert conv.form.channels.findData("split") == -1


@pytest.fixture
def ctl(qapp, tmp_path):
    import vrp.app as A
    s = Settings()
    s.update({"rec.folder": str(tmp_path / "rec"), "general.first_run_done": True, "vox.enabled": False,
              "rec.src_mic": True, "rec.src_system": True})
    c = A.AppController(qapp, A._gui_args(["--fake-audio"]))
    c.backend.realtime = 8.0
    c.window.show()
    yield c
    c.quit()


def test_options_and_main_window(qapp, ctl):
    from PySide6.QtWidgets import QLabel

    from vrp.ui.options_dialog import OptionsDialog
    d = OptionsDialog(ctl.settings, ctl.engine, ctl.hotkeys, ctl.ext, ctl.backend, "Format", ctl.window)
    d.show()
    pump(qapp, 0.05)
    ch = d.fmt_form.channels
    assert ch.findData("split") >= 0 and ch.currentData() == 1
    ch.setCurrentIndex(ch.findData("split"))
    assert d.apply()
    assert ctl.settings.get("fmt.layout") == "split" and ctl.settings.get("fmt.channels") == 2
    texts = [w.text() for w in d.findChildren(QLabel)]
    assert any("Format > Channels > Separate" in t for t in texts)      # pointer on the Recording tab
    d.done(0)
    d2 = OptionsDialog(ctl.settings, ctl.engine, ctl.hotkeys, ctl.ext, ctl.backend, "Format", ctl.window)
    assert d2.fmt_form.channels.currentData() == "split"                # reopened: the choice is kept
    d2.done(0)
    e, w = ctl.engine, ctl.window
    pump(qapp, 0.1)
    assert w.f_sample.text().endswith("L/R")                             # idle: the format chosen for the next file
    e.record()
    assert wait_for(qapp, lambda: e.state.value == "recording")
    assert "L/R" in w.f_sample.text()                                    # recording, before and after the file opens
    assert wait_for(qapp, lambda: e.file is not None and e.file.recording)
    pump(qapp, 0.1)
    assert "L/R" in w.f_sample.text()
    pump(qapp, 0.3)
    e.stop()
    assert wait_for(qapp, lambda: e.state.value == "stopped")
    assert "L/R" in w.f_sample.text()


def test_split_mp3_extracted_to_flac_keeps_the_roles(qapp, make_engine, tmp_path):
    e = make_engine(**{"fmt.codec": "mp3", "fmt.mp3.kbps": 64})
    src = probe(record_for(qapp, e, 0.4))
    clip = editor.extract(src, 0, RATE * 2, str(tmp_path / "clip.flac"))   # the extension picks the format
    assert clip.codec == "flac" and clip.layout == "split"


def test_highpass_switched_on_during_a_split_recording(qapp, make_engine):
    e = make_engine()
    e.record()
    pump(qapp, 0.3)
    e.settings.set("fmt.highpass", True, save=False)       # applied on the fly to the microphone side only
    pump(qapp, 0.3)
    e.stop()
    assert wait_for(qapp, lambda: e.state == State.STOPPED)
    last = window(read(e.file.path), -1.2, -0.3)
    assert tone_level(last[:, 0], MIC_HZ) > 0.12 and tone_level(last[:, 1], PC_HZ) > 0.12


def test_salvage_reads_a_complete_flac_to_the_end(tmp_path):
    import soundfile as sf

    from vrp.core import journal
    src = tmp_path / "whole.flac"
    sf.write(src, np.zeros((70000, 2), np.float32), RATE, format="FLAC")
    with sf.SoundFile(tmp_path / "copy.flac", "w", samplerate=RATE, channels=2, format="FLAC") as dst:
        assert journal._salvage_flac(str(src), dst) == 70000


def test_failed_flac_recovery_leaves_the_original_and_no_temporary_file(tmp_path, monkeypatch):
    import soundfile as sf

    from vrp.core import journal
    src = tmp_path / "rec.flac"
    sf.write(src, np.zeros((RATE, 2), np.float32), RATE, format="FLAC")
    before = src.read_bytes()

    def broken(lp, dst):
        raise OSError("disk error")
    monkeypatch.setattr(journal, "_salvage_flac", broken)
    p = journal.Pending(audio=str(src), journal=str(src) + ".vrp-journal",
                        state={"format": {"codec": "flac", "rate": RATE, "channels": 2, "layout": "split"}},
                        size=src.stat().st_size, seconds=1.0)
    ok, msg = journal.recover(p)
    assert not ok and "disk error" in msg
    assert src.read_bytes() == before and not (tmp_path / "rec.flac.vrp-recover.flac").exists()
