"""Minimal in-house ID3v2.4 writer/reader with CHAP/CTOC chapters (Section 8.2).

Written in-house so the app does not depend on GPL-licensed tag libraries
(Section 20.2).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

TS_DESC = "VRP_TIMESTAMP"


def synchsafe(n: int) -> bytes:
    return bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])


def unsynchsafe(b: bytes) -> int:
    return (b[0] << 21) | (b[1] << 14) | (b[2] << 7) | b[3]


def _frame(fid: str, payload: bytes) -> bytes:
    return fid.encode("ascii") + synchsafe(len(payload)) + b"\0\0" + payload


def text_frame(fid: str, text: str) -> bytes:
    return _frame(fid, b"\x03" + text.encode("utf-8"))


def txxx_frame(desc: str, value: str) -> bytes:
    return _frame("TXXX", b"\x03" + desc.encode("utf-8") + b"\0" + value.encode("utf-8"))


def comm_frame(text: str, lang: bytes = b"eng") -> bytes:
    return _frame("COMM", b"\x03" + lang + b"\0" + text.encode("utf-8"))


@dataclass
class Chapter:
    start_ms: int
    end_ms: int
    title: str
    timestamp: str | None = None


@dataclass
class TagData:
    title: str = ""
    artist: str = ""
    comment: str = ""
    date: str = ""            # TDRC, ISO 8601
    encoder: str = ""         # TSSE
    extra: dict = field(default_factory=dict)   # TXXX desc -> value
    chapters: list[Chapter] = field(default_factory=list)


def build_frames(tag: TagData) -> bytes:
    out = b""
    if tag.title:
        out += text_frame("TIT2", tag.title)
    if tag.artist:
        out += text_frame("TPE1", tag.artist)
    if tag.date:
        # ID3v2.4 TDRC allows yyyy-MM-ddTHH:mm:ss; full precision goes in TXXX
        out += text_frame("TDRC", tag.date[:19])
    if tag.encoder:
        out += text_frame("TSSE", tag.encoder)
    if tag.comment:
        out += comm_frame(tag.comment)
    for k, v in tag.extra.items():
        out += txxx_frame(k, v)
    chaps = tag.chapters
    if chaps:
        ids = []
        for i, c in enumerate(chaps, start=1):
            eid = f"chp{i}".encode("latin1")
            ids.append(eid)
            sub = text_frame("TIT2", c.title)
            if c.timestamp:
                sub += txxx_frame(TS_DESC, c.timestamp)
            payload = eid + b"\0" + struct.pack(">IIII", max(0, c.start_ms), max(0, c.end_ms),
                                                0xFFFFFFFF, 0xFFFFFFFF) + sub
            out += _frame("CHAP", payload)
        # CTOC entry count is one byte: nest sub-tables when there are more than 255.
        if len(ids) <= 255:
            out += _frame("CTOC", b"toc\0" + bytes([0x03, len(ids)]) + b"".join(i + b"\0" for i in ids)
                          + text_frame("TIT2", "Segments"))
        else:
            groups = [ids[i:i + 255] for i in range(0, len(ids), 255)]
            sub_ids = []
            for gi, g in enumerate(groups, start=1):
                sid = f"toc{gi}".encode("latin1")
                sub_ids.append(sid)
                out += _frame("CTOC", sid + b"\0" + bytes([0x01, len(g)]) + b"".join(i + b"\0" for i in g))
            out += _frame("CTOC", b"toc\0" + bytes([0x03, len(sub_ids)]) +
                          b"".join(i + b"\0" for i in sub_ids) + text_frame("TIT2", "Segments"))
    return out


def build_tag(tag: TagData, total_size: int | None = None) -> bytes:
    """Complete ID3v2.4 tag; padded to total_size when given (and large enough)."""
    frames = build_frames(tag)
    body = len(frames)
    if total_size is not None and total_size - 10 >= body:
        body = total_size - 10
    return b"ID3\x04\x00\x00" + synchsafe(body) + frames + b"\0" * (body - len(frames))


def empty_tag(total_size: int) -> bytes:
    return b"ID3\x04\x00\x00" + synchsafe(total_size - 10) + b"\0" * (total_size - 10)


def tag_size_at_start(head: bytes) -> int:
    """Total size of an ID3v2 tag at the start of the data, 0 when none."""
    if len(head) >= 10 and head[:3] == b"ID3" and head[3] in (2, 3, 4):
        size = unsynchsafe(head[6:10]) + 10
        if head[5] & 0x10:      # footer present
            size += 10
        return size
    return 0


def _decode_text(b: bytes) -> str:
    if not b:
        return ""
    enc, data = b[0], b[1:]
    if enc == 0:
        return data.split(b"\0")[0].decode("latin1")
    if enc == 3:
        return data.split(b"\0")[0].decode("utf-8", "replace")
    if enc in (1, 2):
        codec = "utf-16" if enc == 1 else "utf-16-be"
        text = data.decode(codec, "replace")
        return text.split("\0")[0]
    return ""


def _split_enc(b: bytes) -> tuple[str, str]:
    """Split 'desc\0value' for TXXX (handles UTF-16 terminators)."""
    enc, data = b[0], b[1:]
    if enc in (1, 2):
        codec = "utf-16" if enc == 1 else "utf-16-be"
        idx = 0
        while idx + 1 < len(data):
            if data[idx] == 0 and data[idx + 1] == 0:
                break
            idx += 2
        return data[:idx].decode(codec, "replace"), data[idx + 2:].decode(codec, "replace").rstrip("\0")
    parts = data.split(b"\0", 1)
    codec = "latin1" if enc == 0 else "utf-8"
    return parts[0].decode(codec, "replace"), (parts[1] if len(parts) > 1 else b"").decode(codec, "replace").rstrip("\0")


def _iter_frames(data: bytes, version: int):
    p = 0
    while p + 10 <= len(data):
        fid = data[p:p + 4]
        if fid[0] == 0 or not fid.isalnum():
            break
        size = unsynchsafe(data[p + 4:p + 8]) if version == 4 else struct.unpack(">I", data[p + 4:p + 8])[0]
        payload = data[p + 10:p + 10 + size]
        yield fid.decode("ascii", "replace"), payload
        p += 10 + size


def parse_tag(raw: bytes) -> TagData:
    """Parse an ID3v2.3/2.4 tag (raw starts with 'ID3')."""
    tag = TagData()
    if len(raw) < 10 or raw[:3] != b"ID3":
        return tag
    version = raw[3]
    if version not in (3, 4):
        return tag
    size = unsynchsafe(raw[6:10])
    data = raw[10:10 + size]
    if raw[5] & 0x40:   # extended header: skip it
        ext = unsynchsafe(data[:4]) if version == 4 else struct.unpack(">I", data[:4])[0] + 4
        data = data[ext:]
    for fid, payload in _iter_frames(data, version):
        try:
            if fid == "TIT2":
                tag.title = _decode_text(payload)
            elif fid == "TPE1":
                tag.artist = _decode_text(payload)
            elif fid in ("TDRC", "TYER"):
                tag.date = _decode_text(payload)
            elif fid == "TSSE":
                tag.encoder = _decode_text(payload)
            elif fid == "COMM" and len(payload) > 4:
                _, tag.comment = _split_enc(payload[:1] + payload[4:])
            elif fid == "TXXX" and payload:
                d, v = _split_enc(payload)
                tag.extra[d] = v
            elif fid == "CHAP":
                z = payload.index(b"\0")
                start_ms, end_ms = struct.unpack(">II", payload[z + 1:z + 9])
                title, ts = "", None
                for sfid, sp in _iter_frames(payload[z + 17:], version):
                    if sfid == "TIT2":
                        title = _decode_text(sp)
                    elif sfid == "TXXX" and sp:
                        d, v = _split_enc(sp)
                        if d == TS_DESC:
                            ts = v
                tag.chapters.append(Chapter(start_ms, end_ms, title, ts))
        except (ValueError, IndexError, struct.error):
            continue
    tag.chapters.sort(key=lambda c: c.start_ms)
    return tag
