"""Transcript files: .txt written live, .srt at the end, journal for crash recovery (1.1.0)."""
import json
import os

import pytest

from vrp.transcribe import transcript as TR
from vrp.transcribe.transcript import BOOKMARK, OFF, TEXT, Entry, TranscriptWriter

W0 = 1795800000.0


def ent(kind=TEXT, t=0.0, d=2.0, pos=None, speaker=None, text="hello", lang="en"):
    return Entry(kind, W0 + t, W0 + t + d, pos, None if pos is None else pos + d, speaker, text, lang)


def test_txt_lines():
    clock = TR.clock(W0)
    assert TR.txt_line(ent(text="hi")) == f"[{clock}] hi"
    assert TR.txt_line(ent(speaker="me", text="hi")) == f"[{clock}] Me: hi"
    assert TR.txt_line(ent(speaker="others", text="hi")) == f"[{clock}] Others: hi"
    assert TR.txt_line(ent(BOOKMARK)) == f"[{clock}] * Bookmark"
    assert TR.txt_line(ent(OFF, d=65)) == f"[{clock} - {TR.clock(W0 + 65)}] Off the record (nothing saved)"


def test_srt_cues_timing():
    assert TR.srt_time(3723.4567) == "01:02:03,457" and TR.srt_time(-1) == "00:00:00,000"
    cues = TR.srt_cues([ent(pos=1.25, d=0.4, speaker="me", text="short"), ent(BOOKMARK, pos=3.0),
                        ent(pos=None, text="no audio"), ent(pos=5.0, d=2.0, text="long")], 7)
    assert cues == ("7\r\n00:00:01,250 --> 00:00:02,250\r\nMe: short\r\n\r\n"        # at least 1 s on screen
                    "8\r\n00:00:05,000 --> 00:00:07,000\r\nlong\r\n")


@pytest.mark.parametrize("d", [{}, {"kind": "x", "wall": 1}, {"kind": "text"}, {"kind": "text", "wall": "a"},
                               {"kind": "text", "wall": 1, "text": 5}, {"kind": "text", "wall": 1, "pos": "x"},
                               "string", None])
def test_journal_entries_checked_field_by_field(d):
    assert Entry.from_json(d) is None


def test_journal_entry_round_trip_and_limits():
    e = ent(speaker="me", pos=1.0)
    assert Entry.from_json(json.loads(json.dumps(e.to_json()))) == e
    odd = Entry.from_json({"kind": "text", "wall": 1, "speaker": "boss", "text": "x" * 20000, "lang": "en"})
    assert odd.speaker is None and len(odd.text) == 10000


def read(p):
    with open(p, encoding="utf-8", newline="") as f:
        return f.read()


def test_writer_live_txt_srt_and_journal(tmp_path):
    base = str(tmp_path / "會議 VOX-001")                     # Unicode path
    w = TranscriptWriter(base, ["Voice Recorder Pro - live transcript", "Recording: x.mp3"])
    w.add(ent(pos=0.5, speaker="me", text="早晨"))
    txt = read(base + ".txt")
    assert txt.startswith("Voice Recorder Pro - live transcript\r\nRecording: x.mp3\r\n\r\n")
    assert txt.endswith("Me: 早晨\r\n")                        # on disk at once, before the file closes
    assert os.path.exists(base + TR.JOURNAL_SUFFIX)
    w.add(ent(BOOKMARK, t=3))
    w.add(ent(t=4, pos=4.5, text="bye"))
    files = w.close()
    assert files == [base + ".txt", base + ".srt"]
    assert not os.path.exists(base + TR.JOURNAL_SUFFIX) and not os.path.exists(base + ".srt.tmp")
    srt = read(base + ".srt")
    assert srt.count("-->") == 2 and "Me: 早晨" in srt
    assert w.close() == [] and w.add(ent()) is None             # closed: nothing more is written


def test_writer_never_overwrites_and_appends_with_marker(tmp_path):
    base = str(tmp_path / "rec")
    w = TranscriptWriter(base, ["h"])
    w.add(ent(pos=0.0, text="one"))
    w.close()
    with pytest.raises(FileExistsError):
        TranscriptWriter(base, ["h"])                            # another recording with the same name
    w2 = TranscriptWriter(base, ["h"], append=True)               # the same recording, appended
    w2.add(ent(pos=10.0, text="two"))
    w2.close()
    txt = read(base + ".txt")
    assert txt.count("--- Continued") == 1 and txt.index("one") < txt.index("two")
    srt = read(base + ".srt")
    assert srt.startswith("1\r\n") and "\r\n\r\n2\r\n00:00:10,000" in srt   # cue numbers carry on


def test_text_only_writer_has_no_srt(tmp_path):
    base = str(tmp_path / "t")
    w = TranscriptWriter(base, ["Text only: no sound was saved"], srt=False)
    w.add(ent(text="x"))
    assert w.close() == [base + ".txt"] and not os.path.exists(base + ".srt")


def test_recover_after_crash(tmp_path):
    base = str(tmp_path / "sub" / "crash")
    w = TranscriptWriter(base, ["h"])
    w.add(ent(pos=1.0, text="kept"))
    w._txt.close()
    w._journal.close()                                            # the program "crashes" here
    with open(base + TR.JOURNAL_SUFFIX, "a", encoding="utf-8") as f:
        f.write('{"kind": "text", "wall": 1, "te')                # a line cut short
    found = TR.find_journals([str(tmp_path)])
    assert found == [base + TR.JOURNAL_SUFFIX]                    # found one folder down
    assert TR.recover(found[0]) == [base + ".srt"]
    assert "kept" in read(base + ".srt") and not os.path.exists(base + TR.JOURNAL_SUFFIX)
    assert TR.find_journals([str(tmp_path / "missing")]) == []


def test_recover_text_only_journal_writes_no_srt(tmp_path):
    base = str(tmp_path / "t")
    w = TranscriptWriter(base, ["h"], srt=False)
    w.add(ent(text="x"))
    w._txt.close()
    w._journal.close()
    assert TR.recover(base + TR.JOURNAL_SUFFIX) == []
    assert not os.path.exists(base + ".srt") and not os.path.exists(base + TR.JOURNAL_SUFFIX)
