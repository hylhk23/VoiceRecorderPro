"""Streaming file writers for WAV, MP3 and FLAC (Section 6).

All writers keep the file playable while recording: WAV headers are rewritten
at every flush (FR-REC-004), MP3 is written frame by frame with an Info/Xing
frame finished on close (FR-REC-005), FLAC is synced through libsndfile.
"""
from __future__ import annotations

import logging
import os
from array import array

import numpy as np

from vrp import paths
from vrp.core import dsp, id3, metadata, mp3frames, wavfile
from vrp.core.model import FormatSpec, Segment

log = logging.getLogger(__name__)

ID3_RESERVE = 32 * 1024      # space kept at the start of MP3 files for tags and chapters
MP3_ENC_DELAY = 576          # LAME encoder delay (samples of silence before the audio)
MP3_DEC_DELAY = 529          # decoder delay removed by mpg123/libsndfile
WAV_LIMIT = int(3.9 * 1024 ** 3)


class WriterError(Exception):
    pass


class BaseWriter:
    codec = ""

    def __init__(self, path: str, fmt: FormatSpec):
        self.path = str(path)
        self.fmt = fmt
        self.frames = 0             # sample frames written (at fmt.rate)
        self.existing_segments: list[Segment] = []
        self.closed = False

    # interface -----------------------------------------------------------
    def write(self, x: np.ndarray) -> None:
        raise NotImplementedError

    def flush(self) -> None:
        raise NotImplementedError

    def fsync(self) -> None:
        raise NotImplementedError

    def finalize(self, segments: list[Segment], tags: dict) -> None:
        raise NotImplementedError

    def abort(self) -> None:
        raise NotImplementedError

    @property
    def size(self) -> int:
        raise NotImplementedError

    def journal_state(self) -> dict:
        return {}

    def segment_start(self) -> int:
        """Position (in the file's playback timeline) where the next written sample lands."""
        return self.frames

    # helpers --------------------------------------------------------------
    @staticmethod
    def _open_exclusive(path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        return open(paths.longpath(path), "x+b")


# ============================================================================ WAV
class WavWriter(BaseWriter):
    codec = "wav"

    def __init__(self, path: str, fmt: FormatSpec, append: bool = False):
        super().__init__(path, fmt)
        self.subtype = fmt.wav_subtype
        self.block_align = fmt.channels * dsp.SUBTYPE_BITS[self.subtype] // 8
        if append:
            self.f = open(paths.longpath(path), "r+b")
            info = wavfile.parse(self.f)
            if (info.subtype != self.subtype or info.channels != fmt.channels or info.rate != fmt.rate):
                self.f.close()
                raise WriterError("the open file has a different format")
            self.data_offset, self.fact_offset = info.data_offset, info.fact_offset
            self.data_size = info.data_size
            self.frames = info.frames
            labels = info.labels
            self.existing_segments = [Segment(fr, _label_ts(labels.get(cid, "")))
                                      for cid, fr in sorted(info.cues, key=lambda c: c[1])]
            # drop anything after the data chunk; it is rewritten on finalise
            self.f.seek(self.data_offset + self.data_size)
            self.f.truncate()
            if self.fact_offset is None and self.subtype not in ("PCM_U8", "PCM_16", "PCM_24"):
                self.fact_offset = None
        else:
            self.f = self._open_exclusive(path)
            hdr = wavfile.header_bytes(self.subtype, fmt.channels, fmt.rate)
            self.f.write(hdr)
            self.data_offset, self.fact_offset = wavfile.header_layout(self.subtype)
            self.data_size = 0
        self._dirty = False

    def write(self, x: np.ndarray) -> None:
        if x.size == 0:
            return
        data = dsp.to_pcm_bytes(x, self.subtype)
        self.f.write(data)
        self.data_size += len(data)
        self.frames += x.shape[0]
        self._dirty = True

    def flush(self) -> None:
        if self._dirty:
            wavfile.update_sizes(self.f, self.data_offset, self.data_size, self.frames, self.fact_offset)
            self._dirty = False
        self.f.flush()

    def fsync(self) -> None:
        self.f.flush()
        os.fsync(self.f.fileno())

    @property
    def size(self) -> int:
        return self.data_offset + self.data_size

    @property
    def near_limit(self) -> bool:
        return self.size >= WAV_LIMIT

    def finalize(self, segments: list[Segment], tags: dict) -> None:
        if self.closed:
            return
        metadata.wav_finalize(self.f, self.data_offset, self.data_size, self.frames, self.fact_offset,
                              segments, tags)
        self.f.flush()
        os.fsync(self.f.fileno())
        self.f.close()
        self.closed = True

    def abort(self) -> None:
        if not self.closed:
            try:
                self.flush()
                self.f.close()
            except OSError:
                pass
            self.closed = True

    def journal_state(self) -> dict:
        return {"data_offset": self.data_offset, "fact_offset": self.fact_offset,
                "subtype": self.subtype, "block_align": self.block_align}


def _label_ts(label: str) -> str | None:
    """A WAV cue label is the ISO time stamp when time-stamping was on."""
    if len(label) >= 19 and label[4] == "-" and "T" in label:
        return label
    return None


# ============================================================================ MP3
class Mp3Writer(BaseWriter):
    codec = "mp3"

    def __init__(self, path: str, fmt: FormatSpec, append: bool = False):
        super().__init__(path, fmt)
        import lameenc

        self.enc = lameenc.Encoder()
        self.enc.set_in_sample_rate(fmt.rate)
        self.enc.set_out_sample_rate(fmt.rate)
        self.enc.set_channels(fmt.channels)
        self.enc.set_quality(2)
        if fmt.mp3_mode == "vbr":
            self.enc.set_vbr(4)
            self.enc.set_vbr_quality(int(fmt.mp3_vbr_q))
        else:
            self.enc.set_bit_rate(int(fmt.mp3_kbps))
        self.counter = mp3frames.FrameCounter()
        self.xing_offset: int | None = None
        self.xing_len = 0
        self.prior_frames = 0          # complete frames already in the file (append)
        self.prior_bytes = 0
        self.prior_offsets = array("Q")
        self.template: mp3frames.FrameHeader | None = None
        self.appending = append
        if append:
            self.f = open(paths.longpath(path), "r+b")
            lay = mp3frames.scan_file(self.f)
            if lay.template is None:
                self.f.close()
                raise WriterError("the open MP3 file has no audio frames")
            if lay.template.rate != fmt.rate or lay.template.channels != fmt.channels:
                self.f.close()
                raise WriterError("the open file has a different format")
            self.f.seek(0)
            head = self.f.read(lay.audio_start) if lay.audio_start else b""
            tag = id3.parse_tag(head) if head else id3.TagData()
            self.audio_start = lay.audio_start
            self.xing_offset, self.xing_len = lay.xing_offset, lay.xing_len
            self.template = lay.template
            self.prior_frames = lay.frames
            self.prior_offsets = array("Q", (o - (self.xing_offset if self.xing_offset is not None
                                                 else lay.first_audio)
                                             for o in lay.offsets[::self.counter.every]))
            self.counter.vbr = lay.vbr
            self.frames = max(0, lay.samples() - MP3_DEC_DELAY)   # decoded length
            self.existing_segments = [Segment(int(round(c.start_ms * fmt.rate / 1000)), c.timestamp)
                                      for c in tag.chapters] or [Segment(0)]
            self.f.seek(lay.end)
            self.f.truncate()
            self.data_start = self.xing_offset if self.xing_offset is not None else lay.first_audio
            self.prior_bytes = lay.end - self.data_start
        else:
            self.f = self._open_exclusive(path)
            self.f.write(id3.empty_tag(ID3_RESERVE))
            self.audio_start = ID3_RESERVE
            self.data_start = ID3_RESERVE
        self._pending = b""

    def _emit(self, data: bytes) -> None:
        if not data:
            return
        if self.xing_offset is None and not self.appending:
            # first output: put an Info/Xing placeholder frame in front of it
            hdr = mp3frames.parse_header(data)
            if hdr is None:
                self._pending += data
                hdr = mp3frames.parse_header(self._pending)
                if hdr is None:
                    return
                data, self._pending = self._pending, b""
            self.template = hdr
            frame = mp3frames.build_xing_frame(hdr, 0, 0, None, self.fmt.mp3_mode == "vbr")
            self.xing_offset = self.f.tell()
            self.xing_len = len(frame)
            self.data_start = self.xing_offset
            self.f.write(frame)
        self.f.write(data)
        self.counter.feed(data)

    def write(self, x: np.ndarray) -> None:
        if x.size == 0:
            return
        pcm = dsp.to_int16(x)
        self._emit(bytes(self.enc.encode(pcm.tobytes())))
        self.frames += x.shape[0]

    def flush(self) -> None:
        self.f.flush()

    def fsync(self) -> None:
        self.f.flush()
        os.fsync(self.f.fileno())

    def segment_start(self) -> int:
        # The encoder delays its output; decoded audio starts MP3_ENC_DELAY later.
        return self.frames + MP3_ENC_DELAY if self.frames > 0 else 0

    @property
    def size(self) -> int:
        try:
            return self.f.tell()
        except (OSError, ValueError):
            return 0

    def _update_xing(self) -> None:
        """Write frame count, byte count and seek table into the Info/Xing frame."""
        if self.xing_offset is None or self.template is None:
            return
        # all offsets are relative to the start of the Xing frame
        base_new = self.prior_bytes if self.appending else self.xing_len
        total = base_new + self.counter.bytes
        nframes = self.prior_frames + self.counter.frames
        sampled = array("Q", self.prior_offsets)
        sampled.extend(o + base_new for o in self.counter.offsets)
        toc = mp3frames.make_toc(sampled, self.counter.every, nframes, total)
        pos = self.f.tell()
        if self.appending:
            self.f.seek(self.xing_offset)
            old = self.f.read(self.xing_len)
            hdr = mp3frames.parse_header(old)
            frame = mp3frames.patch_xing(old, hdr, nframes, total, toc) if hdr else old
        else:
            vbr = self.fmt.mp3_mode == "vbr" or self.counter.vbr
            frame = mp3frames.build_xing_frame(self.template, nframes, total, toc, vbr)
        self.f.seek(self.xing_offset)
        self.f.write(frame)
        self.f.seek(pos)

    def finalize(self, segments: list[Segment], tags: dict) -> None:
        if self.closed:
            return
        self._emit(bytes(self.enc.flush()))
        self._update_xing()
        self.f.flush()
        os.fsync(self.f.fileno())
        self.f.close()
        self.closed = True
        metadata.mp3_write_tag(self.path, metadata.mp3_tag_data(segments, self.fmt.rate,
                                                                self.frames + MP3_ENC_DELAY, tags))

    def abort(self) -> None:
        if not self.closed:
            try:
                self.f.flush()
                self.f.close()
            except OSError:
                pass
            self.closed = True

    def journal_state(self) -> dict:
        return {"xing_offset": self.xing_offset, "audio_start": self.audio_start}


# =========================================================================== FLAC
class FlacWriter(BaseWriter):
    codec = "flac"

    def __init__(self, path: str, fmt: FormatSpec, append: bool = False):
        super().__init__(path, fmt)
        if append:
            raise WriterError("FLAC files cannot be appended to")
        import soundfile as sf

        self.raw = self._open_exclusive(path)
        subtype = "PCM_24" if fmt.flac_bits >= 24 else "PCM_16"
        self.sf = sf.SoundFile(self.raw, "w", samplerate=fmt.rate, channels=fmt.channels,
                               subtype=subtype, format="FLAC",
                               compression_level=max(0.0, min(1.0, fmt.flac_level / 8.0)))

    def write(self, x: np.ndarray) -> None:
        if x.size == 0:
            return
        self.sf.write(np.clip(x, -1.0, 1.0))
        self.frames += x.shape[0]

    def flush(self) -> None:
        self.sf.flush()
        self.raw.flush()

    def fsync(self) -> None:
        self.flush()
        os.fsync(self.raw.fileno())

    @property
    def size(self) -> int:
        try:
            return os.fstat(self.raw.fileno()).st_size
        except (OSError, ValueError):
            return 0

    def finalize(self, segments: list[Segment], tags: dict) -> None:
        if self.closed:
            return
        self.sf.close()
        self.raw.flush()
        os.fsync(self.raw.fileno())
        self.raw.close()
        self.closed = True
        metadata.flac_write_tags(self.path, segments, self.fmt.rate, tags)

    def abort(self) -> None:
        if not self.closed:
            try:
                self.sf.close()
                self.raw.close()
            except Exception:
                pass
            self.closed = True


WRITERS = {"wav": WavWriter, "mp3": Mp3Writer, "flac": FlacWriter}


def create_writer(path: str, fmt: FormatSpec, append: bool = False) -> BaseWriter:
    return WRITERS[fmt.codec](path, fmt, append=append)


def can_append(codec: str) -> bool:
    return codec in ("wav", "mp3")


def wav_header_probe(path: str) -> wavfile.WavInfo:
    with open(paths.longpath(path), "rb") as f:
        return wavfile.parse(f)


__all__ = ["create_writer", "can_append", "WriterError", "BaseWriter", "WavWriter", "Mp3Writer",
           "FlacWriter"]
