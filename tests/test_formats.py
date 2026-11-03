import numpy as np
import soundfile as sf

from vrp.core import dsp, encoders, flacmeta, id3, mp3frames, wavfile
from vrp.core.audiofile import probe
from vrp.core.model import FormatSpec, Segment

RATE = 16000


def tone(seconds=3.0, amp=0.3, ch=1):
    t = np.arange(int(RATE * seconds)) / RATE
    x = (amp * np.sin(2 * np.pi * 440 * t)).astype(np.float32)[:, None]
    return np.repeat(x, ch, axis=1)


def write(path, fmt, x, segs, tags=None):
    w = encoders.create_writer(str(path), fmt)
    for i in range(0, len(x), 1000):
        w.write(x[i:i + 1000])
        if i % 8000 == 0:
            w.flush()
    w.finalize(segs, tags or {"title": "t"})
    return w


def test_g711_matches_standard_within_one_step():
    x = np.linspace(-32768, 32767, 5000).astype(np.int16)
    for enc, dec in ((dsp.lin2ulaw, dsp.ulaw2lin), (dsp.lin2alaw, dsp.alaw2lin)):
        y = dec(enc(x)).astype(np.int32)
        err = np.abs(y - x.astype(np.int32))
        # G.711 quantisation step is at most 1024 at full scale
        assert err.max() <= 1100


def test_wav_subtypes_readable_and_tagged(tmp_path):
    x = tone()
    for st in ("PCM_U8", "PCM_16", "PCM_24", "FLOAT", "ULAW", "ALAW"):
        p = tmp_path / f"a_{st}.wav"
        write(p, FormatSpec("wav", RATE, 1, wav_subtype=st), x,
              [Segment(0, "2026-10-02T14:03:21.250-07:00"), Segment(RATE)])
        y, r = sf.read(p, dtype="float32", always_2d=True)
        assert r == RATE and y.shape == x.shape
        assert np.max(np.abs(y - x)) < 0.05
        m = probe(str(p))
        assert [s.start for s in m.segments] == [0, RATE]
        assert m.segments[0].timestamp == "2026-10-02T14:03:21.250-07:00"


def test_wav_header_valid_while_recording(tmp_path):                # FR-REC-004
    p = tmp_path / "live.wav"
    w = encoders.create_writer(str(p), FormatSpec("wav", RATE, 1))
    w.write(tone(1.0))
    w.flush()
    info = sf.info(str(p))
    assert info.frames == RATE
    w.finalize([Segment(0)], {})


def test_mp3_has_info_frame_and_chapters(tmp_path):
    p = tmp_path / "a.mp3"
    write(p, FormatSpec("mp3", RATE, 1, mp3_kbps=32), tone(), [Segment(0, "2026-10-02T14:03:21.250-07:00"),
                                                              Segment(RATE + 576)])
    with open(p, "rb") as f:
        lay = mp3frames.scan_file(f)
        f.seek(lay.xing_offset)
        frame = f.read(lay.xing_len)
    nframes, nbytes = mp3frames.read_xing(frame, mp3frames.parse_header(frame))
    assert nframes == lay.frames and nbytes == lay.end - lay.xing_offset
    m = probe(str(p))
    assert m.codec == "mp3" and abs(m.duration - 3.0) < 0.1
    assert len(m.segments) == 2 and abs(m.segments[1].start - (RATE + 576)) <= 16
    assert m.segments[0].timestamp.startswith("2026-10-02T14:03:21")


def test_mp3_vbr_and_stereo(tmp_path):
    p = tmp_path / "v.mp3"
    write(p, FormatSpec("mp3", 44100, 2, mp3_mode="vbr", mp3_vbr_q=4), np.repeat(tone(2.0), 1, axis=0)[:, [0, 0]],
          [Segment(0)])
    info = sf.info(str(p))
    assert info.channels == 2 and info.samplerate in (16000, 44100)


def test_mp3_append_keeps_old_audio(tmp_path):
    p = tmp_path / "ap.mp3"
    fmt = FormatSpec("mp3", RATE, 1, mp3_kbps=32)
    write(p, fmt, tone(2.0), [Segment(0)])
    before = sf.info(str(p)).frames
    w = encoders.create_writer(str(p), fmt, append=True)
    start = w.segment_start()
    w.write(tone(1.0))
    w.finalize(w.existing_segments + [Segment(start)], {})
    m = probe(str(p))
    assert m.frames > before + RATE * 0.9
    assert len(m.segments) == 2


def test_wav_append(tmp_path):
    p = tmp_path / "ap.wav"
    fmt = FormatSpec("wav", RATE, 1)
    write(p, fmt, tone(2.0), [Segment(0)])
    w = encoders.create_writer(str(p), fmt, append=True)
    start = w.segment_start()
    assert start == 2 * RATE
    w.write(tone(1.0))
    w.finalize(w.existing_segments + [Segment(start)], {})
    m = probe(str(p))
    assert m.frames == 3 * RATE and [s.start for s in m.segments] == [0, 2 * RATE]


def test_flac_chapters(tmp_path):
    p = tmp_path / "a.flac"
    write(p, FormatSpec("flac", RATE, 1), tone(), [Segment(0), Segment(RATE * 2, "2026-01-01T00:00:00.000+00:00")])
    d = dict(flacmeta.read_comments(str(p)))
    assert d["CHAPTER002"] == "00:00:02.000"
    m = probe(str(p))
    assert [s.start for s in m.segments] == [0, 2 * RATE] and m.frames == 3 * RATE


def test_id3_more_than_255_chapters():
    tag = id3.TagData(chapters=[id3.Chapter(i * 1000, i * 1000 + 999, f"Segment {i}") for i in range(300)])
    raw = id3.build_tag(tag)
    back = id3.parse_tag(raw)
    assert len(back.chapters) == 300


def test_wav_parse_tolerates_crashed_header(tmp_path):
    p = tmp_path / "c.wav"
    with open(p, "wb") as f:
        f.write(wavfile.header_bytes("PCM_16", 1, RATE, 0, 0))
        f.write(b"\x01\x00" * 1000)
    with open(p, "rb") as f:
        info = wavfile.parse(f)
    assert info.frames == 1000
