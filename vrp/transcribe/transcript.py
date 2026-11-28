"""Transcript entries and the files they are saved to.

For a recording VOX20261127-001.mp3 the transcript is saved beside it as
  VOX20261127-001.txt          text with times, written as each phrase is finished
  VOX20261127-001.srt          subtitles timed to the audio file (only when audio is kept)
  VOX20261127-001.txt.journal  the entries as JSON lines while recording; removed when the
                               transcript is complete, used to finish it after a crash
In "Text only" mode there is no audio file and no .srt.

The .txt and the journal are appended line by line and flushed to disk after each
phrase, so a crash loses at most the phrase being written. The .srt is written to a
temporary file and then moved into place.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass

from vrp import paths
from vrp.transcribe import SPEAKERS

log = logging.getLogger(__name__)

TEXT, BOOKMARK, OFF = "text", "bookmark", "off"
JOURNAL_SUFFIX = ".txt.journal"
TRANSCRIPT_EXTS = (".txt", ".srt")


@dataclass
class Entry:
    kind: str                       # text | bookmark | off
    wall: float                     # start, seconds since the epoch
    wall_end: float
    pos: float | None = None        # start in the audio file, seconds (None: no audio file)
    pos_end: float | None = None
    speaker: str | None = None      # me | others | None
    text: str = ""
    lang: str = ""

    def to_json(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_json(d: dict) -> "Entry | None":
        """Field-by-field check of one journal line; None when it is not a valid entry."""
        if not isinstance(d, dict) or d.get("kind") not in (TEXT, BOOKMARK, OFF):
            return None
        try:
            wall, wall_end = float(d["wall"]), float(d.get("wall_end", d["wall"]))
            pos = None if d.get("pos") is None else float(d["pos"])
            pos_end = None if d.get("pos_end") is None else float(d["pos_end"])
        except (KeyError, TypeError, ValueError):
            return None
        speaker = d.get("speaker")
        if speaker not in (None, "me", "others"):
            speaker = None
        text, lang = d.get("text", ""), d.get("lang", "")
        if not isinstance(text, str) or not isinstance(lang, str):
            return None
        return Entry(d["kind"], wall, wall_end, pos, pos_end, speaker, text[:10000], lang[:8])


def clock(t: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(t))


def label(entry: Entry) -> str:
    return SPEAKERS.get(entry.speaker or "", "")


def txt_line(e: Entry) -> str:
    if e.kind == BOOKMARK:
        return f"[{clock(e.wall)}] * Bookmark"
    if e.kind == OFF:
        return f"[{clock(e.wall)} - {clock(e.wall_end)}] Off the record (nothing saved)"
    who = label(e)
    return f"[{clock(e.wall)}] {who + ': ' if who else ''}{e.text}"


def srt_time(s: float) -> str:
    ms = int(round(max(0.0, s) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    sec, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"


def srt_cues(entries: list[Entry], first_number: int = 1) -> str:
    """Subtitles for the text entries that have audio positions; each cue lasts at least 1 s."""
    out = []
    n = first_number
    for e in entries:
        if e.kind != TEXT or e.pos is None:
            continue
        end = max(e.pos_end if e.pos_end is not None else e.pos, e.pos + 1.0)
        who = label(e)
        out.append(f"{n}\r\n{srt_time(e.pos)} --> {srt_time(end)}\r\n{who + ': ' if who else ''}{e.text}\r\n")
        n += 1
    return "\r\n".join(out)


def _count_cues(path: str) -> int:
    try:
        with open(paths.longpath(path), encoding="utf-8-sig") as f:
            return sum(1 for line in f if "-->" in line)
    except OSError:
        return 0


def _atomic_write(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(paths.longpath(tmp), "w", encoding="utf-8", newline="") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(paths.longpath(tmp), paths.longpath(path))


class TranscriptWriter:
    """The transcript of one recording file."""

    def __init__(self, base: str, header: list[str], append: bool = False, srt: bool = True,
                 txt: bool = True):
        self.base = base
        self.txt_path = base + ".txt"
        self.srt_path = base + ".srt"
        self.journal_path = base + JOURNAL_SUFFIX
        self.want_srt = srt
        self.want_txt = txt
        self.entries: list[Entry] = []
        self.closed = False
        os.makedirs(os.path.dirname(os.path.abspath(base)) or ".", exist_ok=True)
        exists = os.path.exists(paths.longpath(self.txt_path))
        if exists and append:
            self._txt = open(paths.longpath(self.txt_path), "a", encoding="utf-8", newline="")
            self._txt.write(f"\r\n--- Continued {time.strftime('%Y-%m-%d %H:%M:%S')} ---\r\n")
        else:
            # never overwrite: a transcript of another recording with this name stays as it is
            self._txt = open(paths.longpath(self.txt_path), "x", encoding="utf-8", newline="")
            self._txt.write("\r\n".join(header) + "\r\n\r\n")
        self._sync(self._txt)
        self._journal = open(paths.longpath(self.journal_path), "a", encoding="utf-8", newline="\n")
        self._journal.write(json.dumps({"header": header, "srt": srt, "started": time.time()},
                                       ensure_ascii=False) + "\n")
        self._sync(self._journal)

    @staticmethod
    def _sync(f) -> None:
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass

    def add(self, e: Entry) -> None:
        if self.closed:
            return
        self.entries.append(e)
        self._txt.write(txt_line(e) + "\r\n")
        self._sync(self._txt)
        self._journal.write(json.dumps(e.to_json(), ensure_ascii=False) + "\n")
        self._sync(self._journal)

    def close(self) -> list[str]:
        """Finish the transcript; returns the files written."""
        if self.closed:
            return []
        self.closed = True
        self._txt.close()
        self._journal.close()
        written = [self.txt_path]
        if self.want_srt and any(e.kind == TEXT and e.pos is not None for e in self.entries):
            first = _count_cues(self.srt_path) + 1
            cues = srt_cues(self.entries, first)
            old = ""
            if first > 1:
                with open(paths.longpath(self.srt_path), encoding="utf-8-sig", newline="") as f:
                    old = f.read().rstrip("\r\n") + "\r\n\r\n"
            _atomic_write(self.srt_path, old + cues)
            written.append(self.srt_path)
        try:
            os.remove(paths.longpath(self.journal_path))
        except OSError:
            pass
        return written


def recover(journal_path: str) -> list[str]:
    """Finish a transcript left by a crash: write its .srt from the journal and remove the journal."""
    base = journal_path[: -len(JOURNAL_SUFFIX)]
    entries, want_srt = [], True
    try:
        with open(paths.longpath(journal_path), encoding="utf-8") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue                    # a line cut short by the crash
                if isinstance(obj, dict) and "header" in obj:
                    want_srt = bool(obj.get("srt", True))
                    continue
                e = Entry.from_json(obj)
                if e is not None:
                    entries.append(e)
    except OSError as exc:
        log.warning("transcript journal %s could not be read: %s", journal_path, exc)
        return []
    written = []
    if want_srt and any(e.kind == TEXT and e.pos is not None for e in entries):
        srt = base + ".srt"
        first = _count_cues(srt) + 1
        old = ""
        if first > 1:
            with open(paths.longpath(srt), encoding="utf-8-sig", newline="") as f:
                old = f.read().rstrip("\r\n") + "\r\n\r\n"
        _atomic_write(srt, old + srt_cues(entries, first))
        written.append(srt)
    os.remove(paths.longpath(journal_path))
    log.info("transcript %s finished after a crash (%d entries)", base, len(entries))
    return written


def find_journals(folders: list[str], depth: int = 3) -> list[str]:
    """Transcript journals in the folders and their subfolders (file names like {yyyy}/{Mon}/...),
    at most depth levels down."""
    found = set()

    def scan(folder: str, level: int) -> None:
        try:
            entries = list(os.scandir(paths.longpath(folder)))
        except OSError:
            return
        for d in entries:
            if d.name.endswith(JOURNAL_SUFFIX) and d.is_file():
                found.add(os.path.join(folder, d.name))
            elif level < depth and d.is_dir(follow_symlinks=False) and not d.name.startswith("."):
                scan(os.path.join(folder, d.name), level + 1)
    for folder in folders:
        scan(folder, 0)
    return sorted(found)
