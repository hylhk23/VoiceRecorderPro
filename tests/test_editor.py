import os
import time

import numpy as np
import pytest
import soundfile as sf

from vrp.core import editor, encoders, mp3frames
from vrp.core.audiofile import probe
from vrp.core.model import FormatSpec, Segment

RATE = 16000


def make(path, codec, seconds, segs=None, **kw):
    rng = np.random.default_rng(1)
    x = rng.uniform(-0.4, 0.4, (int(RATE * seconds), 1)).astype(np.float32)
    w = encoders.create_writer(str(path), FormatSpec(codec, RATE, 1, **kw))
    for i in range(0, len(x), 65536):
        w.write(x[i:i + 65536])
    w.finalize(segs or [Segment(0)], {"title": "src"})
    return x


def test_tc07_wav_extract_fast_and_exact(tmp_path):                       # TC-07, FR-EDIT-004
    src = tmp_path / "hour.wav"
    x = make(src, "wav", 3600)
    m = probe(str(src))
    t0 = time.perf_counter()
    out = editor.extract(m, 600 * RATE, 610 * RATE)
    elapsed = time.perf_counter() - t0
    assert elapsed < 1.0
    y, _ = sf.read(out.path, dtype="int16", always_2d=True)
    ref = np.clip(np.rint(x[600 * RATE:610 * RATE] * 32767), -32768, 32767).astype(np.int16)
    assert np.array_equal(y, ref)


def test_tc08_mp3_extract_copies_frames(tmp_path):                        # TC-08
    src = tmp_path / "a.mp3"
    make(src, "mp3", 60, mp3_kbps=64)
    m = probe(str(src))
    out = editor.extract(m, 10 * RATE, 40 * RATE)
    data_src = src.read_bytes()
    with open(out.path, "rb") as f:
        lay = mp3frames.scan_file(f)
        f.seek(lay.first_audio)
        body = f.read(lay.end - lay.first_audio)
    assert data_src.find(body) > 0                                         # byte-identical frames
    assert abs(out.duration - 30) < 0.2


def test_tc09_cancel_leaves_original(tmp_path):                          # TC-09, FR-EDIT-005
    src = tmp_path / "c.wav"
    make(src, "wav", 120)
    before = src.read_bytes()
    m = probe(str(src))
    calls = {"n": 0}

    def cancel():
        calls["n"] += 1
        return calls["n"] >= 2              # cancel while copying the second range
    with pytest.raises(InterruptedError):
        editor.delete_range(m, 10 * RATE, 100 * RATE, cancel=cancel)
    assert src.read_bytes() == before
    assert not [p for p in os.listdir(tmp_path) if p.endswith(".vrp-tmp") or ".vrp-edit" in p]


def test_delete_and_undo(tmp_path):
    src = tmp_path / "d.wav"
    make(src, "wav", 20, [Segment(0), Segment(5 * RATE), Segment(12 * RATE)])
    m = editor.delete_range(probe(str(src)), 4 * RATE, 6 * RATE)
    assert m.frames == 18 * RATE
    assert [s.start for s in m.segments] == [0, 4 * RATE, 10 * RATE]
    assert editor.undo(str(src))
    assert probe(str(src)).frames == 20 * RATE


def test_segments_inside_deleted_range_are_removed(tmp_path):            # FR-EDIT-007
    src = tmp_path / "e.wav"
    make(src, "wav", 20, [Segment(0), Segment(5 * RATE), Segment(6 * RATE), Segment(12 * RATE)])
    m = editor.delete_range(probe(str(src)), 4 * RATE, 7 * RATE)
    assert [s.start for s in m.segments] == [0, 4 * RATE, 9 * RATE]


def test_split_join_roundtrip(tmp_path):
    src = tmp_path / "s.wav"
    make(src, "wav", 10)
    p1, p2 = editor.split(probe(str(src)), 4 * RATE)
    j = editor.join([p1, p2], str(tmp_path / "j.wav"))
    a, _ = sf.read(str(src), dtype="int16")
    b, _ = sf.read(j.path, dtype="int16")
    assert np.array_equal(a, b)


def test_join_mixed_formats_needs_convert(tmp_path):                     # FR-EDIT-008
    a, b = tmp_path / "a.wav", tmp_path / "b.mp3"
    make(a, "wav", 2)
    make(b, "mp3", 2)
    with pytest.raises(editor.FormatMismatch):
        editor.join([str(a), str(b)], str(tmp_path / "j.wav"))
    j = editor.join([str(a), str(b)], str(tmp_path / "j.wav"), convert=True)
    assert abs(j.duration - 4.0) < 0.15


def test_convert_and_export_segments(tmp_path):
    src = tmp_path / "x.flac"
    make(src, "flac", 9, [Segment(0), Segment(3 * RATE), Segment(6 * RATE)])
    m = probe(str(src))
    files = editor.export_segments(m)
    assert len(files) == 3
    out = editor.convert(m, str(tmp_path / "x.mp3"), FormatSpec("mp3", 16000, 1, mp3_kbps=48))
    assert out.codec == "mp3" and len(out.segments) == 3


def test_insert_boundary(tmp_path):
    src = tmp_path / "b.mp3"
    make(src, "mp3", 6, mp3_kbps=32)
    m = editor.insert_boundary(probe(str(src)), 3 * RATE)
    assert len(m.segments) == 2


def test_extract_extension_decides_format(tmp_path):
    src = tmp_path / "w.wav"
    make(src, "wav", 4)
    out = editor.extract(probe(str(src)), RATE, 3 * RATE, str(tmp_path / "clip.mp3"))
    assert out.codec == "mp3" and sf.info(out.path).format == "MP3"
    assert abs(out.duration - 2.0) < 0.1
