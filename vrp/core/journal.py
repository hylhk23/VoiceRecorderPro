"""Crash journal and recovery (FR-REC-006, FR-ERR-010).

While a file is being recorded, '<file>.vrp-journal' holds its format, segment
list and progress, rewritten atomically at every flush. The journal paths are
also listed in active_journals.json in the settings folder. After an unclean
exit, every listed journal points to a file that can be repaired.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path

from vrp import paths
from vrp.core import metadata, mp3frames, wavfile
from vrp.core.model import FormatSpec, Segment

log = logging.getLogger(__name__)
SUFFIX = ".vrp-journal"
_registry_lock = threading.Lock()


def registry_path() -> Path:
    return paths.config_dir() / "active_journals.json"


def _read_registry() -> list[str]:
    try:
        data = json.loads(registry_path().read_text(encoding="utf-8"))
        return [p for p in data if isinstance(p, str)]
    except (OSError, ValueError):
        return []


def _write_registry(items: list[str]) -> None:
    p = registry_path()
    tmp = p.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(items, indent=1), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        log.exception("could not write journal registry")


def register(journal: str) -> None:
    with _registry_lock:
        items = _read_registry()
        if journal not in items:
            items.append(journal)
            _write_registry(items)


def unregister(journal: str) -> None:
    with _registry_lock:
        items = [p for p in _read_registry() if p != journal]
        _write_registry(items)


class Journal:
    def __init__(self, audio_path: str):
        self.audio_path = str(audio_path)
        self.path = self.audio_path + SUFFIX
        register(self.path)

    def write(self, state: dict) -> None:
        tmp = paths.longpath(self.path + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f)
            os.replace(tmp, paths.longpath(self.path))
        except OSError:
            log.warning("journal write failed for %s", self.path)

    def close(self) -> None:
        for p in (self.path, self.path + ".tmp"):
            try:
                os.remove(paths.longpath(p))
            except OSError:
                pass
        unregister(self.path)


@dataclass
class Pending:
    journal: str
    audio: str
    state: dict
    size: int
    seconds: float


def find_pending(extra_folders: list[str] | None = None) -> list[Pending]:
    """Journals left behind by an unclean exit."""
    candidates = set(_read_registry())
    for folder in extra_folders or []:
        try:
            for p in Path(folder).glob("*" + SUFFIX):
                candidates.add(str(p))
        except OSError:
            pass
    out = []
    for j in sorted(candidates):
        if not os.path.exists(paths.longpath(j)):
            unregister(j)
            continue
        try:
            with open(paths.longpath(j), encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, ValueError):
            state = {}
        audio = state.get("path") or j[: -len(SUFFIX)]
        if not os.path.exists(paths.longpath(audio)):
            discard(j)
            continue
        size = os.path.getsize(paths.longpath(audio))
        fmt = FormatSpec.from_json(state.get("format", {}))
        bps = fmt.bytes_per_second() or 1
        out.append(Pending(j, audio, state, size, size / bps))
    return out


def discard(journal: str) -> None:
    try:
        os.remove(paths.longpath(journal))
    except OSError:
        pass
    unregister(journal)


def recover(p: Pending) -> tuple[bool, str]:
    """Repair a file left open by a crash. Returns (ok, message)."""
    state = p.state
    fmt = FormatSpec.from_json(state.get("format", {}))
    segments = [Segment.from_json(s) for s in state.get("segments", [])] or [Segment(0)]
    tags = state.get("tags", {})
    codec = fmt.codec or os.path.splitext(p.audio)[1].lstrip(".").lower()
    path = paths.longpath(p.audio)
    try:
        if codec == "wav":
            with open(path, "r+b") as f:
                f.seek(0, 2)
                size = f.tell()
                try:
                    info = wavfile.parse(f, size)
                    data_offset, fact_offset, block = info.data_offset, info.fact_offset, info.block_align
                except ValueError:
                    data_offset = int(state.get("writer", {}).get("data_offset", 44))
                    fact_offset = state.get("writer", {}).get("fact_offset")
                    block = int(state.get("writer", {}).get("block_align", 2))
                data_size = max(0, size - data_offset)
                data_size -= data_size % max(1, block)
                frames = data_size // max(1, block)
                segments = [s for s in segments if s.start <= frames] or [Segment(0)]
                metadata.wav_finalize(f, data_offset, data_size, frames, fact_offset, segments, tags)
            seconds = frames / max(1, fmt.rate)
        elif codec == "mp3":
            with open(path, "r+b") as f:
                lay = mp3frames.scan_file(f)
                f.seek(lay.end)
                f.truncate()
                if lay.xing_offset is not None and lay.template is not None:
                    f.seek(lay.xing_offset)
                    old = f.read(lay.xing_len)
                    hdr = mp3frames.parse_header(old)
                    if hdr:
                        total = lay.end - lay.xing_offset
                        sampled = [o - lay.xing_offset for o in lay.offsets[::8]]
                        from array import array
                        toc = mp3frames.make_toc(array("Q", sampled), 8, lay.frames, total)
                        f.seek(lay.xing_offset)
                        f.write(mp3frames.patch_xing(old, hdr, lay.frames, total, toc))
            frames = lay.samples()
            segments = [s for s in segments if s.start <= frames] or [Segment(0)]
            metadata.mp3_write_tag(p.audio, metadata.mp3_tag_data(segments, fmt.rate, frames, tags))
            seconds = frames / max(1, fmt.rate)
        elif codec == "flac":
            seconds = _recover_flac(p.audio, fmt, segments, tags)
        else:
            return False, "unknown format"
    except Exception as exc:          # keep the original file untouched on failure
        log.exception("recovery failed for %s", p.audio)
        return False, str(exc)
    discard(p.journal)
    log.info("recovered %s (%.1f s)", p.audio, seconds)
    return True, f"Recovered {os.path.basename(p.audio)} ({seconds:.0f} s)"


def _recover_flac(path: str, fmt: FormatSpec, segments: list[Segment], tags: dict) -> float:
    """Decode what is readable and re-encode it into a clean FLAC file."""
    import numpy as np
    import soundfile as sf

    lp = paths.longpath(path)
    tmp = lp + ".vrp-recover.flac"
    frames = 0
    with sf.SoundFile(lp) as src, sf.SoundFile(tmp, "w", samplerate=src.samplerate, channels=src.channels,
                                               subtype=src.subtype or "PCM_16", format="FLAC") as dst:
        while True:
            try:
                block = src.read(65536, dtype="float32", always_2d=True)
            except Exception:
                break
            if block.size == 0:
                break
            dst.write(np.clip(block, -1, 1))
            frames += block.shape[0]
        rate = src.samplerate
    if frames == 0:
        os.remove(tmp)
        raise ValueError("no readable audio")
    os.replace(tmp, lp)
    segments = [s for s in segments if s.start <= frames] or [Segment(0)]
    metadata.flac_write_tags(path, segments, rate, tags)
    return frames / rate
