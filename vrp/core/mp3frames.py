"""MPEG audio Layer III frame parsing and Xing/Info header construction.

Used for crash-safe MP3 writing (Xing frame updated on finalise), exact
duration, and frame-accurate copy-based editing (Section 10.1).
"""
from __future__ import annotations

import struct
from array import array
from dataclasses import dataclass

_BR_V1 = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0)
_BR_V2 = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0)
_SR = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}
XING_SIZE = 4 + 4 + 4 + 4 + 100 + 4


@dataclass(frozen=True)
class FrameHeader:
    raw: int
    version_id: int      # 3 = MPEG1, 2 = MPEG2, 0 = MPEG2.5
    protected: bool      # CRC present
    bitrate: int         # kbit/s
    rate: int
    padding: int
    mode: int            # 3 = mono
    length: int          # bytes, including header

    @property
    def mpeg1(self) -> bool:
        return self.version_id == 3

    @property
    def samples(self) -> int:
        return 1152 if self.mpeg1 else 576

    @property
    def channels(self) -> int:
        return 1 if self.mode == 3 else 2

    @property
    def side_info_len(self) -> int:
        if self.mpeg1:
            return 17 if self.mode == 3 else 32
        return 9 if self.mode == 3 else 17

    @property
    def main_data_size(self) -> int:
        return self.length - 4 - (2 if self.protected else 0) - self.side_info_len


def parse_header(b: bytes, pos: int = 0) -> FrameHeader | None:
    if len(b) < pos + 4:
        return None
    h = struct.unpack_from(">I", b, pos)[0]
    if (h >> 21) & 0x7FF != 0x7FF:
        return None
    version_id = (h >> 19) & 3
    layer = (h >> 17) & 3
    if version_id == 1 or layer != 1:          # reserved version, or not Layer III
        return None
    br_idx = (h >> 12) & 0xF
    sr_idx = (h >> 10) & 3
    if br_idx in (0, 15) or sr_idx == 3:
        return None
    bitrate = (_BR_V1 if version_id == 3 else _BR_V2)[br_idx]
    rate = _SR[version_id][sr_idx]
    padding = (h >> 9) & 1
    coef = 144 if version_id == 3 else 72
    length = coef * bitrate * 1000 // rate + padding
    return FrameHeader(h, version_id, not ((h >> 16) & 1), bitrate, rate, padding, (h >> 6) & 3, length)


def main_data_begin(frame: bytes, hdr: FrameHeader) -> int:
    p = 4 + (2 if hdr.protected else 0)
    if len(frame) < p + 2:
        return 0
    if hdr.mpeg1:
        return (frame[p] << 1) | (frame[p + 1] >> 7)
    return frame[p]


def is_xing_frame(frame: bytes, hdr: FrameHeader) -> bool:
    p = 4 + (2 if hdr.protected else 0) + hdr.side_info_len
    return frame[p:p + 4] in (b"Xing", b"Info")


def read_xing(frame: bytes, hdr: FrameHeader) -> tuple[int | None, int | None]:
    """(frames, bytes) from a Xing/Info frame."""
    p = 4 + (2 if hdr.protected else 0) + hdr.side_info_len
    if frame[p:p + 4] not in (b"Xing", b"Info"):
        return None, None
    flags = struct.unpack_from(">I", frame, p + 4)[0]
    q = p + 8
    nframes = nbytes = None
    if flags & 1:
        nframes = struct.unpack_from(">I", frame, q)[0]
        q += 4
    if flags & 2:
        nbytes = struct.unpack_from(">I", frame, q)[0]
    return nframes, nbytes


def build_xing_frame(template: FrameHeader, nframes: int, nbytes: int, toc: bytes | None,
                     vbr: bool) -> bytes:
    """A silent frame carrying a Xing (VBR) or Info (CBR) header."""
    table = _BR_V1 if template.mpeg1 else _BR_V2
    coef = 144 if template.mpeg1 else 72
    side = template.side_info_len
    need = 4 + side + XING_SIZE
    br_idx = None
    for i in range(1, 15):
        if coef * table[i] * 1000 // template.rate >= need:
            br_idx = i
            break
    if br_idx is None:
        br_idx = 14
    h = template.raw
    h &= ~(0xF << 12)
    h |= br_idx << 12
    h |= 1 << 16          # no CRC
    h &= ~(1 << 9)        # no padding
    length = coef * table[br_idx] * 1000 // template.rate
    frame = bytearray(length)
    struct.pack_into(">I", frame, 0, h)
    p = 4 + side
    frame[p:p + 4] = b"Xing" if vbr else b"Info"
    struct.pack_into(">III", frame, p + 4, 0x0F, nframes & 0xFFFFFFFF, nbytes & 0xFFFFFFFF)
    frame[p + 16:p + 116] = toc if toc and len(toc) == 100 else bytes(range(0, 256, 3))[:100].ljust(100, b"\xff")
    struct.pack_into(">I", frame, p + 116, 50)
    return bytes(frame)


def patch_xing(frame: bytes, hdr: FrameHeader, nframes: int, nbytes: int, toc: bytes | None) -> bytes:
    """Update the counts (and TOC, when present) of an existing Xing/Info frame."""
    out = bytearray(frame)
    p = 4 + (2 if hdr.protected else 0) + hdr.side_info_len
    if out[p:p + 4] not in (b"Xing", b"Info"):
        return bytes(frame)
    flags = struct.unpack_from(">I", out, p + 4)[0]
    q = p + 8
    if flags & 1:
        struct.pack_into(">I", out, q, nframes & 0xFFFFFFFF)
        q += 4
    if flags & 2:
        struct.pack_into(">I", out, q, nbytes & 0xFFFFFFFF)
        q += 4
    if flags & 4 and toc and len(toc) == 100 and q + 100 <= len(out):
        out[q:q + 100] = toc
    return bytes(out)


def make_toc(sampled_offsets: array, every: int, nframes: int, total_bytes: int, base: int = 0) -> bytes:
    """100-entry seek table from frame offsets sampled every `every` frames.

    Offsets are relative to the start of the Xing frame; base is added to each.
    """
    toc = bytearray(100)
    if not sampled_offsets or nframes <= 0 or total_bytes <= 0:
        return bytes(toc)
    for i in range(100):
        fi = int(i * nframes / 100)
        k = min(len(sampled_offsets) - 1, fi // every)
        toc[i] = min(255, int((sampled_offsets[k] + base) * 256 / total_bytes))
    return bytes(toc)


class FrameCounter:
    """Incremental parser for a stream of encoder output (whole or partial frames)."""

    def __init__(self, every: int = 8):
        self.every = every
        self.frames = 0
        self.bytes = 0
        self.offsets = array("Q")     # byte offset (from stream start) of every Nth frame
        self.first: FrameHeader | None = None
        self.vbr = False
        self._carry = b""
        self._pos = 0                 # stream offset of _carry[0]

    def feed(self, data: bytes) -> None:
        if not data:
            return
        buf = self._carry + data
        p = 0
        n = len(buf)
        while p + 4 <= n:
            hdr = parse_header(buf, p)
            if hdr is None:
                p += 1                # resync (should not happen with encoder output)
                continue
            if p + hdr.length > n:
                break
            if self.frames % self.every == 0:
                self.offsets.append(self._pos + p)
            if self.first is None:
                self.first = hdr
            elif hdr.bitrate != self.first.bitrate:
                self.vbr = True
            self.frames += 1
            p += hdr.length
        self._carry = buf[p:]
        self._pos += p
        self.bytes += len(data)


@dataclass
class Mp3Layout:
    audio_start: int               # first byte after any ID3v2 tag
    xing_offset: int | None        # offset of a Xing/Info frame, if any
    xing_len: int
    first_audio: int               # first real audio frame
    end: int                       # end of the last complete frame
    offsets: array                 # offset of every audio frame (excluding Xing)
    template: FrameHeader | None
    vbr: bool

    @property
    def frames(self) -> int:
        return len(self.offsets)

    def samples(self) -> int:
        return self.frames * (self.template.samples if self.template else 1152)


def scan_file(f, start: int = 0, end: int | None = None, progress=None, cancel=None) -> Mp3Layout:
    """Scan all frames of an MP3 file opened in binary mode."""
    from vrp.core.id3 import tag_size_at_start

    f.seek(0, 2)
    size = f.tell()
    if end is None:
        end = size
        # ignore an ID3v1 tag at the end
        if size >= 128:
            f.seek(size - 128)
            if f.read(3) == b"TAG":
                end = size - 128
    f.seek(start)
    head = f.read(10)
    audio_start = start + tag_size_at_start(head) if start == 0 else start
    offsets = array("Q")
    template = None
    xing_offset, xing_len = None, 0
    vbr = False
    pos = audio_start
    chunk = 1 << 20
    f.seek(pos)
    buf = f.read(chunk)
    buf_pos = pos
    first_br = None
    last_end = audio_start
    while True:
        rel = pos - buf_pos
        if rel + 4 > len(buf) or rel + 2000 > len(buf):
            if buf_pos + len(buf) >= end and rel + 4 > len(buf):
                break
            f.seek(pos)
            buf = f.read(chunk)
            buf_pos = pos
            rel = 0
            if len(buf) < 4:
                break
        if pos + 4 > end:
            break
        hdr = parse_header(buf, rel)
        if hdr is None or (template is not None and (hdr.rate != template.rate or
                                                     hdr.version_id != template.version_id)):
            pos += 1
            continue
        if pos + hdr.length > end:
            break
        if template is None:
            # confirm with the next header to avoid false sync inside garbage
            nxt = parse_header(buf, rel + hdr.length) if rel + hdr.length + 4 <= len(buf) else None
            if nxt is None and pos + hdr.length < end - 4:
                pos += 1
                continue
            frame = buf[rel:rel + hdr.length]
            if is_xing_frame(frame, hdr):
                xing_offset, xing_len = pos, hdr.length
                template = hdr
                pos += hdr.length
                last_end = pos
                continue
            template = hdr
        if first_br is None:
            first_br = hdr.bitrate
        elif hdr.bitrate != first_br:
            vbr = True
        offsets.append(pos)
        pos += hdr.length
        last_end = pos
        if progress and len(offsets) % 20000 == 0:
            progress(pos / max(1, end))
            if cancel and cancel():
                raise InterruptedError("cancelled")
    first_audio = offsets[0] if offsets else last_end
    return Mp3Layout(audio_start, xing_offset, xing_len, first_audio, last_end, offsets, template, vbr)


def reservoir_frames_needed(f, layout: Mp3Layout, index: int) -> int:
    """How many frames before `index` hold bit-reservoir data the frame needs."""
    if index <= 0 or layout.template is None:
        return 0
    f.seek(layout.offsets[index])
    frame = f.read(64)
    hdr = parse_header(frame)
    if hdr is None:
        return 0
    need = main_data_begin(frame, hdr)
    k = 0
    while need > 0 and index - k - 1 >= 0 and k < 8:
        k += 1
        f.seek(layout.offsets[index - k])
        h = parse_header(f.read(4))
        if h is None:
            break
        need -= h.main_data_size
    return k
