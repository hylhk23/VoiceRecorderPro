"""WAV (RIFF) header, cue points and LIST chunks (Section 8.2).

Segments are stored as a 'cue ' chunk with one cue point per segment and a
'LIST'/'adtl' chunk with a 'labl' per cue. File details go into 'LIST'/'INFO'.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

TAG_PCM, TAG_FLOAT, TAG_ALAW, TAG_ULAW, TAG_EXTENSIBLE = 1, 3, 6, 7, 0xFFFE
SUBTYPE_TAG = {"PCM_U8": TAG_PCM, "PCM_16": TAG_PCM, "PCM_24": TAG_PCM, "FLOAT": TAG_FLOAT,
               "ULAW": TAG_ULAW, "ALAW": TAG_ALAW}
SUBTYPE_BITS = {"PCM_U8": 8, "PCM_16": 16, "PCM_24": 24, "FLOAT": 32, "ULAW": 8, "ALAW": 8}
MAX_WAV_BYTES = 0xFFFFFFFF


@dataclass
class WavInfo:
    format_tag: int = TAG_PCM
    channels: int = 1
    rate: int = 16000
    bits: int = 16
    block_align: int = 2
    data_offset: int = 0       # first byte of sample data
    data_size: int = 0         # bytes of sample data (as declared or as present)
    fact_offset: int | None = None
    riff_size_declared: int = 0
    cues: list[tuple[int, int]] = field(default_factory=list)   # (cue id, sample frame)
    labels: dict[int, str] = field(default_factory=dict)        # cue id -> text
    info: dict[str, str] = field(default_factory=dict)          # INFO sub-chunks

    @property
    def subtype(self) -> str:
        if self.format_tag == TAG_FLOAT:
            return "FLOAT"
        if self.format_tag == TAG_ULAW:
            return "ULAW"
        if self.format_tag == TAG_ALAW:
            return "ALAW"
        return {8: "PCM_U8", 16: "PCM_16", 24: "PCM_24", 32: "PCM_32"}.get(self.bits, f"PCM_{self.bits}")

    @property
    def frames(self) -> int:
        return self.data_size // self.block_align if self.block_align else 0


def header_bytes(subtype: str, channels: int, rate: int, data_size: int = 0, frames: int = 0) -> bytes:
    """RIFF header up to and including the 'data' chunk header."""
    tag = SUBTYPE_TAG[subtype]
    bits = SUBTYPE_BITS[subtype]
    block_align = channels * bits // 8
    byte_rate = rate * block_align
    if tag == TAG_PCM:
        fmt = struct.pack("<HHIIHH", tag, channels, rate, byte_rate, block_align, bits)
        extra = b""
    else:
        fmt = struct.pack("<HHIIHHH", tag, channels, rate, byte_rate, block_align, bits, 0)
        extra = b"fact" + struct.pack("<II", 4, frames)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + extra + b"data" + struct.pack("<I", data_size)
    riff_size = len(body) + data_size
    return b"RIFF" + struct.pack("<I", min(riff_size, MAX_WAV_BYTES)) + body


def header_layout(subtype: str) -> tuple[int, int | None]:
    """(data_offset, fact_offset) for headers produced by header_bytes."""
    h = header_bytes(subtype, 1, 8000)
    fact = h.find(b"fact")
    return len(h), (fact if fact >= 0 else None)


def update_sizes(f, data_offset: int, data_size: int, frames: int, fact_offset: int | None,
                 trailer_size: int = 0) -> None:
    """Rewrite RIFF/data/fact sizes in place; file position is restored."""
    pos = f.tell()
    f.seek(4)
    f.write(struct.pack("<I", min(data_offset - 8 + data_size + (data_size & 1) + trailer_size, MAX_WAV_BYTES)))
    f.seek(data_offset - 4)
    f.write(struct.pack("<I", min(data_size, MAX_WAV_BYTES)))
    if fact_offset is not None:
        f.seek(fact_offset + 8)
        f.write(struct.pack("<I", frames & 0xFFFFFFFF))
    f.seek(pos)


def _chunk(cid: bytes, payload: bytes) -> bytes:
    out = cid + struct.pack("<I", len(payload)) + payload
    if len(payload) & 1:
        out += b"\0"
    return out


def trailer_bytes(cues: list[tuple[int, str]], info: dict[str, str]) -> bytes:
    """'cue ', 'LIST'/'adtl' and 'LIST'/'INFO' chunks.

    cues: list of (sample frame, label text). Cue ids start at 1.
    """
    out = b""
    if cues:
        cue_payload = struct.pack("<I", len(cues))
        adtl = b"adtl"
        for i, (frame, label) in enumerate(cues, start=1):
            cue_payload += struct.pack("<II4sIII", i, frame, b"data", 0, 0, frame)
            text = label.encode("utf-8") + b"\0"
            adtl += _chunk(b"labl", struct.pack("<I", i) + text)
        out += _chunk(b"cue ", cue_payload)
        out += _chunk(b"LIST", adtl)
    if info:
        payload = b"INFO"
        for k, v in info.items():
            if not v:
                continue
            payload += _chunk(k.encode("ascii")[:4].ljust(4), v.encode("utf-8") + b"\0")
        if len(payload) > 4:
            out += _chunk(b"LIST", payload)
    return out


def parse(f, file_size: int | None = None) -> WavInfo:
    """Parse a WAV file opened in binary mode. Tolerates truncated files."""
    f.seek(0, 2)
    size = f.tell() if file_size is None else file_size
    f.seek(0)
    head = f.read(12)
    if len(head) < 12 or head[:4] not in (b"RIFF", b"RF64") or head[8:12] != b"WAVE":
        raise ValueError("not a WAV file")
    info = WavInfo(riff_size_declared=struct.unpack("<I", head[4:8])[0])
    pos = 12
    have_fmt = False
    while pos + 8 <= size:
        f.seek(pos)
        hdr = f.read(8)
        if len(hdr) < 8:
            break
        cid, csize = hdr[:4], struct.unpack("<I", hdr[4:])[0]
        body_pos = pos + 8
        if cid == b"fmt ":
            d = f.read(min(csize, 64))
            tag, ch, rate, _br, ba, bits = struct.unpack("<HHIIHH", d[:16])
            if tag == TAG_EXTENSIBLE and len(d) >= 26:
                tag = struct.unpack("<H", d[24:26])[0]
            info.format_tag, info.channels, info.rate, info.block_align, info.bits = tag, ch, rate, ba, bits
            have_fmt = True
        elif cid == b"fact":
            info.fact_offset = pos
        elif cid == b"data":
            info.data_offset = body_pos
            available = max(0, size - body_pos)
            if csize == 0 or csize > available or csize == 0xFFFFFFFF:
                csize = available           # header not updated (crash) or streaming
            info.data_size = csize
        elif cid == b"cue ":
            d = f.read(csize)
            n = struct.unpack("<I", d[:4])[0] if len(d) >= 4 else 0
            for i in range(n):
                rec = d[4 + i * 24: 4 + (i + 1) * 24]
                if len(rec) < 24:
                    break
                cue_id, _position, _chunk_id, _cs, _bs, sample_offset = struct.unpack("<II4sIII", rec)
                info.cues.append((cue_id, sample_offset))
        elif cid == b"LIST":
            d = f.read(csize)
            kind = d[:4]
            p = 4
            while p + 8 <= len(d):
                sid, ssize = d[p:p + 4], struct.unpack("<I", d[p + 4:p + 8])[0]
                sub = d[p + 8:p + 8 + ssize]
                if kind == b"adtl" and sid in (b"labl", b"note") and len(sub) >= 4:
                    cue_id = struct.unpack("<I", sub[:4])[0]
                    text = sub[4:].split(b"\0")[0].decode("utf-8", "replace")
                    if sid == b"labl":
                        info.labels[cue_id] = text
                elif kind == b"INFO":
                    info.info[sid.decode("ascii", "replace")] = sub.split(b"\0")[0].decode("utf-8", "replace")
                p += 8 + ssize + (ssize & 1)
        pos = body_pos + csize + (csize & 1)
    if not have_fmt or not info.data_offset:
        raise ValueError("WAV file has no fmt or data chunk")
    if info.block_align:
        info.data_size -= info.data_size % info.block_align
    return info
