"""Open existing files: format, length, segments and tags."""
from __future__ import annotations

import os

from vrp import paths
from vrp.core import flacmeta, id3, wavfile
from vrp.core.model import FileModel, Segment


class UnsupportedFile(Exception):
    pass


def codec_of(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return {".mp3": "mp3", ".wav": "wav", ".flac": "flac"}.get(ext, "other")


def probe(path: str) -> FileModel:
    """Read a file's details (FR-ERR-015: raises UnsupportedFile with a clear reason)."""
    import soundfile as sf

    lp = paths.longpath(path)
    if not os.path.exists(lp):
        raise UnsupportedFile(f"{os.path.basename(path)} does not exist.")
    try:
        info = sf.info(lp)
    except Exception as exc:
        raise UnsupportedFile(f"{os.path.basename(path)} is not a supported audio file ({exc}).") from exc
    codec = codec_of(path)
    fmt_name = (info.format or "").upper()
    if codec == "other":
        codec = {"WAV": "wav", "WAVEX": "wav", "MP3": "mp3", "MPEG": "mp3", "FLAC": "flac"}.get(fmt_name, "other")
    size = os.path.getsize(lp)
    model = FileModel(path=path, codec=codec, rate=info.samplerate, channels=info.channels,
                      frames=info.frames, size=size, subtype=info.subtype or "")
    segs: list[Segment] = []
    try:
        if codec == "wav":
            with open(lp, "rb") as f:
                w = wavfile.parse(f)
            from vrp.core.encoders import _label_ts
            segs = [Segment(fr, _label_ts(w.labels.get(cid, ""))) for cid, fr in sorted(w.cues, key=lambda c: c[1])]
            model.title = w.info.get("INAM", "")
            model.comment = w.info.get("ICMT", "")
            model.tags = dict(w.info)
            model.subtype = w.subtype
        elif codec == "mp3":
            with open(lp, "rb") as f:
                head = f.read(10)
                n = id3.tag_size_at_start(head)
                f.seek(0)
                raw = f.read(n) if n else b""
            tag = id3.parse_tag(raw)
            segs = [Segment(int(round(c.start_ms * info.samplerate / 1000)), c.timestamp) for c in tag.chapters]
            model.title, model.comment = tag.title, tag.comment
            model.tags = {"TIT2": tag.title, "TPE1": tag.artist, "TDRC": tag.date, "TSSE": tag.encoder,
                          **tag.extra}
            if size and info.duration:
                model.kbps = int(round(size * 8 / info.duration / 1000))
        elif codec == "flac":
            comments = flacmeta.read_comments(lp)
            d = dict(comments)
            i = 1
            while f"CHAPTER{i:03d}" in d:
                ms = flacmeta.chapter_time_to_ms(d[f"CHAPTER{i:03d}"])
                name = d.get(f"CHAPTER{i:03d}NAME", "")
                ts = name if len(name) >= 19 and name[4:5] == "-" and "T" in name else None
                segs.append(Segment(int(round(ms * info.samplerate / 1000)), ts))
                i += 1
            model.title, model.comment = d.get("TITLE", ""), d.get("COMMENT", "")
            model.tags = d
    except Exception:
        segs = []
    segs = sorted((s for s in segs if 0 <= s.start <= max(0, info.frames)), key=lambda s: s.start)
    if not segs or segs[0].start != 0:
        segs.insert(0, Segment(0, segs[0].timestamp if segs and segs[0].start < info.samplerate // 20 else None))
        # merge a first chapter that starts a few ms in (MP3 encoder delay)
        if len(segs) > 1 and segs[1].start < info.samplerate // 20:
            segs.pop(1)
    model.segments = segs
    try:
        with open(lp, "r+b"):
            pass
    except PermissionError:
        model.readonly = True                 # FR-ERR-014 (locked by another program)
    except OSError:
        model.readonly = True
    return model


def tags_from_model(m: FileModel) -> dict:
    t = m.tags or {}
    return {
        "title": m.title or os.path.splitext(os.path.basename(m.path))[0],
        "comment": m.comment,
        "artist": t.get("IART") or t.get("TPE1") or t.get("ARTIST", ""),
        "date": t.get("VRP_START") or t.get("ICRD") or t.get("TDRC") or t.get("DATE", ""),
        "device": t.get("VRP_DEVICE") or t.get("ISRC", ""),
        "format": t.get("VRP_FORMAT", ""),
    }
