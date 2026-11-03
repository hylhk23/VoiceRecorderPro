"""Direct-to-file edit commands (Section 10).

WAV is cut by copying whole sample frames, MP3 by copying whole MPEG frames
(plus the frames holding bit-reservoir data for the first one), so neither is
re-encoded. Other formats are decoded and re-encoded at the original settings.
Every edit writes a temporary file in the same folder, fsyncs it, and renames
it into place; the original is untouched on any error or cancel (FR-EDIT-005).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
from array import array
from pathlib import Path
from typing import Callable

import numpy as np

from vrp import paths, winutil
from vrp.core import id3, metadata, mp3frames, wavfile
from vrp.core.audiofile import codec_of, probe, tags_from_model
from vrp.core.encoders import (ID3_RESERVE, MP3_DEC_DELAY, MP3_ENC_DELAY, create_writer)
from vrp.core.model import FileModel, FormatSpec, Segment

log = logging.getLogger(__name__)

Progress = Callable[[float], None] | None
Cancel = Callable[[], bool] | None
UNDO_DIR = ".vrp-undo"


class EditError(Exception):
    pass


class FormatMismatch(EditError):
    pass


def _noop(_f: float) -> None:
    pass


def _check(cancel: Cancel) -> None:
    if cancel and cancel():
        raise InterruptedError("cancelled")


def _tmp_for(dst: str) -> str:
    return paths.longpath(dst + ".vrp-tmp")


def _finish(tmp: str, dst: str) -> None:
    with open(tmp, "r+b") as f:
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, paths.longpath(dst))


def _cleanup(tmp: str) -> None:
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
    except OSError:
        pass


def unique_path(path: str) -> str:
    p = Path(path)
    if not p.exists():
        return str(p)
    k = 2
    while True:
        cand = p.with_name(f"{p.stem}_{k}{p.suffix}")
        if not cand.exists():
            return str(cand)
        k += 1


def check_space(folder: str, needed: int) -> None:
    free = winutil.free_bytes(folder)
    if free is not None and free < needed + 10 * 1024 * 1024:
        raise EditError(f"Not enough free disk space: {needed / 1e6:.1f} MB needed, "
                        f"{free / 1e6:.1f} MB free.")


# ============================================================ segment mapping
def _shift_ts(ts: str | None, seconds: float) -> str | None:
    if not ts or not seconds:
        return ts
    try:
        from datetime import datetime, timedelta
        return (datetime.fromisoformat(ts) + timedelta(seconds=seconds)).isoformat(timespec="milliseconds")
    except ValueError:
        return None


def map_segments(segments: list[Segment], ranges: list[tuple[int, int]], total: int,
                 rate: int = 0) -> list[Segment]:
    """Map segment starts through kept ranges (in output order).

    Segments wholly inside removed audio disappear; a segment that starts in
    removed audio but continues into kept audio starts where the kept audio does.
    """
    out: list[Segment] = []
    starts = [s.start for s in segments] + [total]
    offset = 0
    for a, b in ranges:
        for i, seg in enumerate(segments):
            s, e = seg.start, starts[i + 1]
            lo, hi = max(s, a), min(e, b)
            if hi <= lo:
                continue
            pos = offset + (lo - a)
            if not out or out[-1].start != pos:
                ts = seg.timestamp if lo == s else _shift_ts(seg.timestamp, (lo - s) / rate if rate else 0)
                out.append(Segment(pos, ts))
        offset += b - a
    if not out or out[0].start != 0:
        out.insert(0, Segment(0))
    # drop duplicates at the same position
    clean: list[Segment] = []
    for s in out:
        if clean and clean[-1].start == s.start:
            continue
        clean.append(s)
    return clean


# ===================================================================== WAV
def _wav_ranges(src: str, ranges: list[tuple[int, int]], dst_tmp: str, progress: Progress,
                cancel: Cancel) -> wavfile.WavInfo:
    progress = progress or _noop
    with open(paths.longpath(src), "rb") as f:
        info = wavfile.parse(f)
        ba = info.block_align
        total = sum(b - a for a, b in ranges)
        with open(dst_tmp, "xb") as o:
            hdr = wavfile.header_bytes(info.subtype if info.subtype in wavfile.SUBTYPE_TAG else "PCM_16",
                                       info.channels, info.rate) if info.subtype in wavfile.SUBTYPE_TAG else None
            if hdr is None:
                raise EditError("unsupported WAV encoding for direct editing")
            o.write(hdr)
            data_offset, fact_offset = wavfile.header_layout(info.subtype)
            done = 0
            for a, b in ranges:
                f.seek(info.data_offset + a * ba)
                remaining = (b - a) * ba
                while remaining > 0:
                    _check(cancel)
                    chunk = f.read(min(remaining, 8 << 20))
                    if not chunk:
                        break
                    o.write(chunk)
                    remaining -= len(chunk)
                    done += len(chunk) // ba
                    progress(done / max(1, total))
            data_size = o.tell() - data_offset
            wavfile.update_sizes(o, data_offset, data_size, data_size // ba, fact_offset)
    return info


# ===================================================================== MP3
class _Mp3Source:
    def __init__(self, path: str, progress: Progress = None, cancel: Cancel = None):
        self.path = path
        self.f = open(paths.longpath(path), "rb")
        self.layout = mp3frames.scan_file(self.f, progress=progress, cancel=cancel)
        if self.layout.template is None or not self.layout.frames:
            self.f.close()
            raise EditError("the MP3 file has no audio frames")
        self.spf = self.layout.template.samples

    def frame_at(self, decoded_pos: int) -> int:
        return max(0, min(self.layout.frames, (decoded_pos + MP3_DEC_DELAY) // self.spf))

    def frame_end(self, decoded_pos: int) -> int:
        return max(0, min(self.layout.frames, -(-(decoded_pos + MP3_DEC_DELAY) // self.spf)))

    def decoded_of_frame(self, idx: int) -> int:
        return max(0, idx * self.spf - MP3_DEC_DELAY)

    def frame_bytes(self, i0: int, i1: int) -> tuple[int, int]:
        offs = self.layout.offsets
        start = offs[i0]
        end = offs[i1] if i1 < len(offs) else self.layout.end
        return start, end

    def close(self) -> None:
        self.f.close()


def _mp3_write_frames(srcs: list[tuple[_Mp3Source, int, int]], dst_tmp: str, progress: Progress,
                      cancel: Cancel) -> tuple[int, mp3frames.FrameHeader]:
    """Write frame ranges [(source, first, end)] to a new MP3 with Info/Xing frame. Returns frames."""
    progress = progress or _noop
    template = srcs[0][0].layout.template
    vbr = any(s.layout.vbr for s, _, _ in srcs) or len({s.layout.template.bitrate for s, _, _ in srcs}) > 1
    total_frames = sum(e - b for _, b, e in srcs)
    with open(dst_tmp, "xb") as o:
        o.write(id3.empty_tag(ID3_RESERVE))
        xing_off = o.tell()
        xing = mp3frames.build_xing_frame(template, 0, 0, None, vbr)
        o.write(xing)
        sampled = array("Q")
        written = 0
        for src, b, e in srcs:
            if e <= b:
                continue
            start, end = src.frame_bytes(b, e)
            # sample offsets of every 8th frame for the seek table
            for k in range(b, e):
                if written % 8 == 0:
                    sampled.append(o.tell() - xing_off + (src.layout.offsets[k] - start))
                written += 1
            src.f.seek(start)
            remaining = end - start
            while remaining > 0:
                _check(cancel)
                chunk = src.f.read(min(remaining, 8 << 20))
                if not chunk:
                    break
                o.write(chunk)
                remaining -= len(chunk)
            progress(written / max(1, total_frames))
        total_bytes = o.tell() - xing_off
        toc = mp3frames.make_toc(sampled, 8, written, total_bytes)
        o.seek(xing_off)
        o.write(mp3frames.build_xing_frame(template, written, total_bytes, toc, vbr))
    return written, template


def _mp3_ranges(src: str, ranges: list[tuple[int, int]], dst_tmp: str, progress: Progress,
                cancel: Cancel) -> tuple[list[tuple[int, int]], int]:
    """Copy decoded-position ranges by whole frames. Returns the snapped ranges and output frames."""
    s = _Mp3Source(src)
    try:
        parts = []
        snapped = []
        for a, b in ranges:
            i0, i1 = s.frame_at(a), s.frame_end(b)
            if i1 <= i0:
                continue
            k = mp3frames.reservoir_frames_needed(s.f, s.layout, i0)
            parts.append((s, i0 - k, i1))
            snapped.append((s.decoded_of_frame(i0 - k), s.decoded_of_frame(i1)))
        if not parts:
            raise EditError("the selection is shorter than one MP3 frame")
        n, _ = _mp3_write_frames(parts, dst_tmp, progress, cancel)
        return snapped, n * s.spf - MP3_DEC_DELAY
    finally:
        s.close()


# ================================================================== generic
def _decode_ranges(model: FileModel, ranges: list[tuple[int, int]], dst_tmp_final: str, fmt: FormatSpec,
                   progress: Progress, cancel: Cancel) -> int:
    """Decode ranges and encode them with our writers into dst (for FLAC/conversions)."""
    import soundfile as sf

    from vrp.core.dsp import Resampler

    progress = progress or _noop
    total = sum(b - a for a, b in ranges)
    w = create_writer(dst_tmp_final, fmt)
    try:
        rs = Resampler(model.rate, fmt.rate, fmt.channels) if model.rate != fmt.rate else None
        done = 0
        with sf.SoundFile(paths.longpath(model.path)) as f:
            for a, b in ranges:
                f.seek(a)
                remaining = b - a
                while remaining > 0:
                    _check(cancel)
                    block = f.read(min(remaining, 65536), dtype="float32", always_2d=True)
                    if block.shape[0] == 0:
                        break
                    remaining -= block.shape[0]
                    done += block.shape[0]
                    x = _fit_channels(block, fmt.channels)
                    if rs is not None:
                        x = rs.process(x)
                    w.write(x)
                    progress(done / max(1, total))
            if rs is not None:
                w.write(rs.process(np.zeros((0, fmt.channels), np.float32), last=True))
        return w
    except BaseException:
        w.abort()
        raise


def _fit_channels(x: np.ndarray, ch: int) -> np.ndarray:
    if x.shape[1] == ch:
        return x
    if ch == 1:
        return x[:, :2].mean(axis=1, keepdims=True)
    if x.shape[1] == 1:
        return np.repeat(x, 2, axis=1)
    return x[:, :ch]


def format_of(model: FileModel) -> FormatSpec:
    """Encoder settings matching an existing file."""
    if model.codec == "mp3":
        kbps = model.kbps or 64
        from vrp.core.model import mp3_kbps_choices, nearest
        return FormatSpec("mp3", model.rate, min(2, model.channels), mp3_mode="cbr",
                          mp3_kbps=nearest(kbps, mp3_kbps_choices(model.rate)))
    if model.codec == "flac":
        bits = 24 if "24" in model.subtype else 16
        return FormatSpec("flac", model.rate, min(2, model.channels), flac_bits=bits)
    sub = model.subtype if model.subtype in wavfile.SUBTYPE_TAG else "PCM_16"
    return FormatSpec("wav", model.rate, min(2, model.channels), wav_subtype=sub)


# ================================================================ public API
def write_ranges(model: FileModel, ranges: list[tuple[int, int]], dst: str, fmt: FormatSpec | None = None,
                 progress: Progress = None, cancel: Cancel = None, title: str | None = None) -> FileModel:
    """Write the given ranges of model's file to dst (atomically). Returns the new file's model."""
    ranges = [(max(0, a), min(model.frames, b)) for a, b in ranges if b > a]
    if not ranges:
        raise EditError("nothing to write")
    dst_codec = codec_of(dst)
    if fmt is None and dst_codec in ("mp3", "wav", "flac") and dst_codec != model.codec:
        fmt = format_of(model)                  # the extension decides the output format
        fmt.codec = dst_codec
        fmt = fmt.normalised()
    tags = tags_from_model(model)
    tags["title"] = title or os.path.splitext(os.path.basename(dst))[0]
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    est = int(model.size * sum(b - a for a, b in ranges) / max(1, model.frames)) + 64 * 1024
    check_space(os.path.dirname(os.path.abspath(dst)), est)
    tmp = _tmp_for(dst)
    _cleanup(tmp)
    same_format = fmt is None or (fmt.codec == model.codec and fmt.rate == model.rate and
                                  fmt.channels == model.channels)
    try:
        if same_format and model.codec == "wav" and model.subtype in wavfile.SUBTYPE_TAG:
            _wav_ranges(model.path, ranges, tmp, progress, cancel)
            total = sum(b - a for a, b in ranges)
            segs = map_segments(model.segments, ranges, model.frames, model.rate)
            metadata.write_tags_for_file(tmp, "wav", segs, model.rate, total, tags)
            _finish(tmp, dst)
        elif same_format and model.codec == "mp3":
            snapped, total = _mp3_ranges(model.path, ranges, tmp, progress, cancel)
            segs = map_segments(model.segments, snapped, model.frames, model.rate)
            metadata.mp3_write_tag(tmp, metadata.mp3_tag_data(segs, model.rate, total, tags))
            _finish(tmp, dst)
        else:
            fmt = fmt or format_of(model)
            w = _decode_ranges(model, ranges, tmp, fmt, progress, cancel)
            ratio = fmt.rate / model.rate
            segs = map_segments(model.segments, ranges, model.frames, model.rate)
            segs = [Segment(int(s.start * ratio) + (MP3_ENC_DELAY if fmt.codec == "mp3" and s.start else 0),
                            s.timestamp) for s in segs]
            tags["format"] = fmt.describe()
            w.finalize(segs, tags)
            _finish(tmp, dst)
    except BaseException:
        _cleanup(tmp)
        raise
    return probe(dst)


def extract(model: FileModel, start: int, end: int, dst: str | None = None, fmt: FormatSpec | None = None,
            progress: Progress = None, cancel: Cancel = None) -> FileModel:
    """Copy the selection to a new file; the original is unchanged (FR-EDIT, Extract)."""
    if dst is None:
        base, ext = os.path.splitext(model.path)
        k = 1
        while True:
            dst = f"{base}_x{k}{ext}"
            if not os.path.exists(dst):
                break
            k += 1
    return write_ranges(model, [(start, end)], dst, fmt, progress, cancel)


# ------------------------------------------------------------------ undo
def _undo_index(folder: str) -> Path:
    return Path(folder) / UNDO_DIR / "index.json"


def _read_undo(folder: str) -> dict:
    try:
        return json.loads(_undo_index(folder).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_undo(folder: str, data: dict) -> None:
    p = _undo_index(folder)
    p.parent.mkdir(exist_ok=True)
    p.write_text(json.dumps(data, indent=1), encoding="utf-8")
    if winutil.IS_WINDOWS:
        try:
            import ctypes
            ctypes.windll.kernel32.SetFileAttributesW(str(p.parent), 0x2)   # hidden
        except Exception:
            pass


def backup(path: str) -> str:
    """Keep a copy for Undo Last Edit (one level per file, FR-EDIT-006)."""
    folder = os.path.dirname(os.path.abspath(path))
    d = Path(folder) / UNDO_DIR
    d.mkdir(exist_ok=True)
    name = os.path.basename(path)
    idx = _read_undo(folder)
    old = idx.get(name)
    if old and os.path.exists(d / old["backup"]):
        winutil.send_to_recycle_bin(d / old["backup"])
    bak = f"{name}.{time.strftime('%Y%m%d-%H%M%S')}.bak"
    check_space(folder, os.path.getsize(paths.longpath(path)))
    shutil.copy2(paths.longpath(path), paths.longpath(str(d / bak)))
    idx[name] = {"backup": bak, "time": time.time()}
    _write_undo(folder, idx)
    return str(d / bak)


def can_undo(path: str) -> bool:
    folder = os.path.dirname(os.path.abspath(path))
    e = _read_undo(folder).get(os.path.basename(path))
    return bool(e) and os.path.exists(Path(folder) / UNDO_DIR / e["backup"])


def undo(path: str) -> bool:
    folder = os.path.dirname(os.path.abspath(path))
    idx = _read_undo(folder)
    name = os.path.basename(path)
    e = idx.get(name)
    if not e:
        return False
    bak = Path(folder) / UNDO_DIR / e["backup"]
    if not bak.exists():
        return False
    tmp = _tmp_for(path)
    shutil.copy2(paths.longpath(str(bak)), tmp)
    _finish(tmp, path)
    winutil.send_to_recycle_bin(bak)
    del idx[name]
    _write_undo(folder, idx)
    return True


def cleanup_undo(folder: str, open_files: set[str] | None = None, max_age_h: float = 24) -> None:
    """Remove backups older than 24 h or of files no longer open (to the Recycle Bin)."""
    idx = _read_undo(folder)
    if not idx:
        return
    keep = {os.path.basename(p) for p in (open_files or set())}
    now = time.time()
    changed = False
    for name, e in list(idx.items()):
        if name in keep and now - e.get("time", 0) < max_age_h * 3600:
            continue
        bak = Path(folder) / UNDO_DIR / e.get("backup", "")
        if bak.exists():
            winutil.send_to_recycle_bin(bak)
        del idx[name]
        changed = True
    if changed:
        _write_undo(folder, idx)


# ---------------------------------------------------------- destructive edits
def _replace_with_ranges(model: FileModel, ranges, progress, cancel) -> FileModel:
    if model.readonly:
        raise EditError("The file is read-only or in use by another program.")
    backup(model.path)
    # write next to the original, then swap
    tmp_dst = model.path + ".vrp-edit" + os.path.splitext(model.path)[1]
    try:
        write_ranges(model, ranges, tmp_dst, None, progress, cancel,
                     title=os.path.splitext(os.path.basename(model.path))[0])
        os.replace(paths.longpath(tmp_dst), paths.longpath(model.path))
    except BaseException:
        _cleanup(paths.longpath(tmp_dst))
        raise
    return probe(model.path)


def delete_range(model: FileModel, start: int, end: int, progress: Progress = None,
                 cancel: Cancel = None) -> FileModel:
    ranges = [(0, start), (end, model.frames)]
    ranges = [(a, b) for a, b in ranges if b > a]
    if not ranges:
        raise EditError("Deleting the whole file is not allowed; use Delete File instead.")
    return _replace_with_ranges(model, ranges, progress, cancel)


def crop(model: FileModel, start: int, end: int, progress: Progress = None, cancel: Cancel = None) -> FileModel:
    return _replace_with_ranges(model, [(start, end)], progress, cancel)


def split(model: FileModel, at: int, remove_original: bool = False, progress: Progress = None,
          cancel: Cancel = None) -> tuple[str, str]:
    if not 0 < at < model.frames:
        raise EditError("Move the position inside the file to split it.")
    base, ext = os.path.splitext(model.path)
    p1, p2 = unique_path(f"{base}_part1{ext}"), unique_path(f"{base}_part2{ext}")
    half = (lambda f: progress(f / 2)) if progress else None
    half2 = (lambda f: progress(0.5 + f / 2)) if progress else None
    write_ranges(model, [(0, at)], p1, None, half, cancel)
    try:
        write_ranges(model, [(at, model.frames)], p2, None, half2, cancel)
    except BaseException:
        _cleanup(p1)
        raise
    if remove_original:
        winutil.send_to_recycle_bin(model.path)
    return p1, p2


def export_segments(model: FileModel, folder: str | None = None, progress: Progress = None,
                    cancel: Cancel = None) -> list[str]:
    folder = folder or os.path.dirname(model.path)
    base, ext = os.path.splitext(os.path.basename(model.path))
    starts = model.segment_starts() + [model.frames]
    out = []
    n = len(model.segments)
    for i in range(n):
        a, b = starts[i], starts[i + 1]
        if b <= a:
            continue
        dst = unique_path(os.path.join(folder, f"{base}_seg{i + 1:02d}{ext}"))
        sub = (lambda f, i=i: progress((i + f) / n)) if progress else None
        write_ranges(model, [(a, b)], dst, None, sub, cancel)
        out.append(dst)
    return out


def insert_boundary(model: FileModel, at: int) -> FileModel:
    """Start a new segment at `at`; the audio is untouched, tags are rewritten."""
    if model.readonly:
        raise EditError("The file is read-only or in use by another program.")
    segs = [s for s in model.segments if s.start != at] + [Segment(at)]
    segs.sort(key=lambda s: s.start)
    metadata.write_tags_for_file(model.path, model.codec, segs, model.rate, model.frames, tags_from_model(model))
    return probe(model.path)


def join(paths_in: list[str], dst: str, convert: bool = False, progress: Progress = None,
         cancel: Cancel = None) -> FileModel:
    """Concatenate files into dst (FR-EDIT-008)."""
    if len(paths_in) < 2:
        raise EditError("Choose at least two files to join.")
    models = [probe(p) for p in paths_in]
    first = models[0]
    same = all(m.codec == first.codec and m.rate == first.rate and m.channels == first.channels and
               (m.codec != "wav" or m.subtype == first.subtype) for m in models)
    if not same and not convert:
        raise FormatMismatch("The files have different formats.")
    check_space(os.path.dirname(os.path.abspath(dst)), sum(m.size for m in models))
    tmp = _tmp_for(dst)
    _cleanup(tmp)
    tags = tags_from_model(first)
    tags["title"] = os.path.splitext(os.path.basename(dst))[0]
    try:
        segs: list[Segment] = []
        if same and first.codec == "mp3":
            srcs = [_Mp3Source(m.path) for m in models]
            try:
                offset = 0
                for m, s in zip(models, srcs):
                    for seg in m.segments:
                        segs.append(Segment(offset + seg.start, seg.timestamp))
                    offset += s.layout.frames * s.spf
                n, _ = _mp3_write_frames([(s, 0, s.layout.frames) for s in srcs], tmp, progress, cancel)
                total = n * srcs[0].spf - MP3_DEC_DELAY
            finally:
                for s in srcs:
                    s.close()
            metadata.mp3_write_tag(tmp, metadata.mp3_tag_data(segs, first.rate, total, tags))
        elif same and first.codec == "wav" and first.subtype in wavfile.SUBTYPE_TAG:
            ba = None
            with open(tmp, "xb") as o:
                o.write(wavfile.header_bytes(first.subtype, first.channels, first.rate))
                data_offset, fact_offset = wavfile.header_layout(first.subtype)
                offset = 0
                total_frames = sum(m.frames for m in models)
                for m in models:
                    with open(paths.longpath(m.path), "rb") as f:
                        info = wavfile.parse(f)
                        ba = info.block_align
                        for seg in m.segments:
                            segs.append(Segment(offset + seg.start, seg.timestamp))
                        f.seek(info.data_offset)
                        remaining = info.data_size
                        while remaining > 0:
                            _check(cancel)
                            chunk = f.read(min(remaining, 8 << 20))
                            if not chunk:
                                break
                            o.write(chunk)
                            remaining -= len(chunk)
                        offset += info.frames
                    if progress:
                        progress(offset / max(1, total_frames))
                data_size = o.tell() - data_offset
                wavfile.update_sizes(o, data_offset, data_size, data_size // (ba or 1), fact_offset)
            metadata.write_tags_for_file(tmp, "wav", _dedupe(segs), first.rate, offset, tags)
        else:
            fmt = format_of(first)
            w = create_writer(tmp, fmt)
            try:
                import soundfile as sf

                from vrp.core.dsp import Resampler
                total_frames = sum(m.frames for m in models)
                done = 0
                offset = 0
                for m in models:
                    ratio = fmt.rate / m.rate
                    for seg in m.segments:
                        segs.append(Segment(offset + int(seg.start * ratio) +
                                            (MP3_ENC_DELAY if fmt.codec == "mp3" and offset + seg.start else 0),
                                            seg.timestamp))
                    rs = Resampler(m.rate, fmt.rate, fmt.channels) if m.rate != fmt.rate else None
                    with sf.SoundFile(paths.longpath(m.path)) as f:
                        while True:
                            _check(cancel)
                            block = f.read(65536, dtype="float32", always_2d=True)
                            if block.shape[0] == 0:
                                break
                            x = _fit_channels(block, fmt.channels)
                            if rs is not None:
                                x = rs.process(x)
                            w.write(x)
                            done += block.shape[0]
                            if progress:
                                progress(done / max(1, total_frames))
                    if rs is not None:
                        w.write(rs.process(np.zeros((0, fmt.channels), np.float32), last=True))
                    offset = w.frames
                tags["format"] = fmt.describe()
                w.finalize(_dedupe(segs), tags)
            except BaseException:
                w.abort()
                raise
        _finish(tmp, dst)
    except BaseException:
        _cleanup(tmp)
        raise
    return probe(dst)


def _dedupe(segs: list[Segment]) -> list[Segment]:
    out: list[Segment] = []
    for s in sorted(segs, key=lambda s: s.start):
        if out and out[-1].start == s.start:
            continue
        out.append(s)
    return out or [Segment(0)]


def convert(model: FileModel, dst: str, fmt: FormatSpec, progress: Progress = None,
            cancel: Cancel = None) -> FileModel:
    """Save a copy in another format (File > Save Copy As, vrp convert)."""
    return write_ranges(model, [(0, model.frames)], dst, fmt, progress, cancel)
