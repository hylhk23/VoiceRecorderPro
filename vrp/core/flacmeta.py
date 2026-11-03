"""Read and rewrite the VORBIS_COMMENT block of a FLAC file (Section 8.2).

Chapters are stored as CHAPTER001=HH:MM:SS.mmm and CHAPTER001NAME=<label>.
"""
from __future__ import annotations

import os
import struct

from vrp import paths

VORBIS_COMMENT = 4
PADDING = 1


def _read_blocks(f) -> tuple[list[tuple[int, bytes]], int]:
    if f.read(4) != b"fLaC":
        raise ValueError("not a FLAC file")
    blocks = []
    while True:
        hdr = f.read(4)
        if len(hdr) < 4:
            raise ValueError("truncated FLAC metadata")
        last = hdr[0] & 0x80
        btype = hdr[0] & 0x7F
        length = int.from_bytes(hdr[1:4], "big")
        blocks.append((btype, f.read(length)))
        if last:
            break
    return blocks, f.tell()


def read_comments(path: str) -> list[tuple[str, str]]:
    with open(paths.longpath(path), "rb") as f:
        blocks, _ = _read_blocks(f)
    for btype, data in blocks:
        if btype == VORBIS_COMMENT:
            vlen = struct.unpack_from("<I", data, 0)[0]
            p = 4 + vlen
            n = struct.unpack_from("<I", data, p)[0]
            p += 4
            out = []
            for _ in range(n):
                ln = struct.unpack_from("<I", data, p)[0]
                p += 4
                entry = data[p:p + ln].decode("utf-8", "replace")
                p += ln
                if "=" in entry:
                    k, v = entry.split("=", 1)
                    out.append((k.upper(), v))
            return out
    return []


def _comment_block(comments: list[tuple[str, str]], vendor: str = "Voice Recorder Pro") -> bytes:
    v = vendor.encode("utf-8")
    out = struct.pack("<I", len(v)) + v + struct.pack("<I", len(comments))
    for k, val in comments:
        e = f"{k}={val}".encode("utf-8")
        out += struct.pack("<I", len(e)) + e
    return out


def write_comments(path: str, comments: list[tuple[str, str]], padding: int = 4096) -> None:
    """Replace all Vorbis comments, rewriting the file atomically."""
    src = paths.longpath(path)
    tmp = src + ".vrp-tmp"
    with open(src, "rb") as f:
        blocks, audio_pos = _read_blocks(f)
        keep = [(t, d) for t, d in blocks if t not in (VORBIS_COMMENT, PADDING)]
        keep.append((VORBIS_COMMENT, _comment_block(comments)))
        keep.append((PADDING, b"\0" * padding))
        with open(tmp, "wb") as o:
            o.write(b"fLaC")
            for i, (t, d) in enumerate(keep):
                last = 0x80 if i == len(keep) - 1 else 0
                o.write(bytes([last | t]) + len(d).to_bytes(3, "big") + d)
            f.seek(audio_pos)
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                o.write(chunk)
            o.flush()
            os.fsync(o.fileno())
    os.replace(tmp, src)


def ms_to_chapter_time(ms: int) -> str:
    h, rem = divmod(int(ms), 3600000)
    m, rem = divmod(rem, 60000)
    s, msec = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{msec:03d}"


def chapter_time_to_ms(text: str) -> int:
    parts = text.strip().split(":")
    total = 0.0
    for p in parts:
        total = total * 60 + float(p)
    return int(round(total * 1000))
