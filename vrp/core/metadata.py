"""Write segment boundaries and tags into finished files (FR-FILE-004, FR-FILE-008)."""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime

from vrp import __version__, paths
from vrp.core import flacmeta, id3, wavfile
from vrp.core.model import Segment

SOFTWARE = f"Voice Recorder Pro {__version__}"


def segment_label(i: int, seg: Segment) -> str:
    return seg.timestamp or f"Segment {i}"


def wav_trailer(segments: list[Segment], tags: dict) -> bytes:
    cues = [(s.start, segment_label(i, s)) for i, s in enumerate(segments, start=1)]
    info = {
        "INAM": tags.get("title", ""),
        "ICMT": tags.get("comment", ""),
        "IART": tags.get("artist", ""),
        "ICRD": tags.get("date", ""),
        "ISRC": tags.get("device", ""),
        "ISFT": SOFTWARE,
    }
    return wavfile.trailer_bytes(cues, info)


def wav_finalize(f, data_offset: int, data_size: int, frames: int, fact_offset: int | None,
                 segments: list[Segment], tags: dict) -> None:
    """Append cue/LIST chunks after the data and fix all sizes. f is open r+b."""
    f.seek(data_offset + data_size)
    f.truncate()
    if data_size & 1:
        f.write(b"\0")
    trailer = wav_trailer(segments, tags)
    f.write(trailer)
    wavfile.update_sizes(f, data_offset, data_size, frames, fact_offset, len(trailer))


def mp3_tag_data(segments: list[Segment], rate: int, total_frames: int, tags: dict) -> id3.TagData:
    chapters = []
    for i, s in enumerate(segments, start=1):
        end = segments[i].start if i < len(segments) else total_frames
        chapters.append(id3.Chapter(int(s.start * 1000 / rate), int(end * 1000 / rate),
                                    f"Segment {i}", s.timestamp))
    extra = {}
    if tags.get("device"):
        extra["VRP_DEVICE"] = tags["device"]
    if tags.get("format"):
        extra["VRP_FORMAT"] = tags["format"]
    if tags.get("date"):
        extra["VRP_START"] = tags["date"]
    return id3.TagData(title=tags.get("title", ""), artist=tags.get("artist", ""),
                       comment=tags.get("comment", ""), date=tags.get("date", ""),
                       encoder=SOFTWARE, extra=extra, chapters=chapters)


def mp3_write_tag(path: str, tag: id3.TagData, min_padding: int = 4096) -> None:
    """Write the ID3v2 tag at the start of the file, in place when it fits."""
    lp = paths.longpath(path)
    with open(lp, "r+b") as f:
        head = f.read(10)
        old = id3.tag_size_at_start(head)
        body = id3.build_frames(tag)
        if old and len(body) + 10 <= old:
            f.seek(0)
            f.write(id3.build_tag(tag, old))
            f.flush()
            os.fsync(f.fileno())
            return
    # rewrite the whole file with a bigger tag
    new_size = len(body) + 10 + max(min_padding, len(body) // 2)
    tmp = lp + ".vrp-tmp"
    with open(lp, "rb") as src, open(tmp, "wb") as dst:
        dst.write(id3.build_tag(tag, new_size))
        src.seek(old)
        shutil.copyfileobj(src, dst, 1 << 20)
        dst.flush()
        os.fsync(dst.fileno())
    os.replace(tmp, lp)


def flac_write_tags(path: str, segments: list[Segment], rate: int, tags: dict) -> None:
    comments = []
    for key, tk in (("TITLE", "title"), ("ARTIST", "artist"), ("COMMENT", "comment"),
                    ("DATE", "date"), ("VRP_DEVICE", "device"), ("VRP_FORMAT", "format")):
        if tags.get(tk):
            comments.append((key, tags[tk]))
    comments.append(("ENCODER", SOFTWARE))
    for i, s in enumerate(segments, start=1):
        comments.append((f"CHAPTER{i:03d}", flacmeta.ms_to_chapter_time(int(s.start * 1000 / rate))))
        comments.append((f"CHAPTER{i:03d}NAME", segment_label(i, s)))
    flacmeta.write_comments(path, comments)


def write_sidecar(path: str, segments: list[Segment], rate: int, tags: dict) -> None:
    data = {
        "file": os.path.basename(path), "rate": rate, "tags": tags,
        "segments": [{"index": i, "start_frame": s.start, "start_seconds": round(s.start / rate, 3),
                      "timestamp": s.timestamp} for i, s in enumerate(segments, start=1)],
        "written": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    with open(paths.longpath(path + ".segments.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def write_tags_for_file(path: str, codec: str, segments: list[Segment], rate: int, frames: int,
                        tags: dict) -> None:
    """Re-tag a closed file (used after edits and conversions)."""
    if codec == "mp3":
        mp3_write_tag(path, mp3_tag_data(segments, rate, frames, tags))
    elif codec == "flac":
        flac_write_tags(path, segments, rate, tags)
    elif codec == "wav":
        lp = paths.longpath(path)
        with open(lp, "r+b") as f:
            info = wavfile.parse(f)
            wav_finalize(f, info.data_offset, info.data_size, info.frames, info.fact_offset, segments, tags)
