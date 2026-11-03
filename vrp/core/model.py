"""Shared data types: segments, output format and the open-file model."""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field, replace
from datetime import datetime

# MP3 sample rates and bitrate limits per MPEG version (layer III).
MP3_RATES_V1 = (32000, 44100, 48000)
MP3_RATES_V2 = (16000, 22050, 24000)
MP3_RATES_V25 = (8000, 11025, 12000)
MP3_RATES = MP3_RATES_V25 + MP3_RATES_V2 + MP3_RATES_V1
MP3_KBPS_V1 = (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
MP3_KBPS_V2 = (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)
WAV_RATES = (8000, 11025, 16000, 22050, 32000, 44100, 48000, 96000)
FLAC_RATES = (8000, 11025, 16000, 22050, 32000, 44100, 48000, 96000)
WAV_SUBTYPES = {
    "PCM_U8": "8-bit PCM", "PCM_16": "16-bit PCM", "PCM_24": "24-bit PCM",
    "FLOAT": "32-bit float", "ULAW": "µ-law 8-bit (telephone)", "ALAW": "A-law 8-bit (telephone)",
}
CODEC_EXT = {"mp3": ".mp3", "wav": ".wav", "flac": ".flac"}


def mp3_kbps_choices(rate: int) -> tuple[int, ...]:
    return MP3_KBPS_V1 if rate in MP3_RATES_V1 else MP3_KBPS_V2


def nearest(value: int, choices) -> int:
    return min(choices, key=lambda c: (abs(c - value), c))


@dataclass
class Segment:
    start: int                      # sample frame where the segment starts
    timestamp: str | None = None    # ISO 8601 wall-clock start, if time-stamping is on

    def to_json(self) -> dict:
        return {"start": self.start, "timestamp": self.timestamp}

    @staticmethod
    def from_json(d: dict) -> "Segment":
        return Segment(int(d.get("start", 0)), d.get("timestamp"))


@dataclass
class FormatSpec:
    codec: str = "mp3"            # mp3 | wav | flac
    rate: int = 16000
    channels: int = 1
    wav_subtype: str = "PCM_16"
    flac_bits: int = 16
    flac_level: int = 5
    mp3_mode: str = "cbr"
    mp3_kbps: int = 32
    mp3_vbr_q: int = 5

    @property
    def ext(self) -> str:
        return CODEC_EXT[self.codec]

    def normalised(self) -> "FormatSpec":
        """Clamp combinations the encoders cannot produce."""
        f = replace(self)
        f.channels = 2 if f.channels >= 2 else 1
        if f.codec == "mp3":
            f.rate = nearest(f.rate, MP3_RATES)
            f.mp3_kbps = nearest(f.mp3_kbps, mp3_kbps_choices(f.rate))
        elif f.codec == "wav":
            f.rate = nearest(f.rate, WAV_RATES) if f.rate not in WAV_RATES else f.rate
            if f.wav_subtype in ("ULAW", "ALAW") and f.rate > 16000:
                f.rate = 16000
        else:
            f.rate = nearest(f.rate, FLAC_RATES) if f.rate not in FLAC_RATES else f.rate
            f.flac_bits = 24 if f.flac_bits >= 24 else 16
        return f

    @staticmethod
    def from_settings(s) -> "FormatSpec":
        return FormatSpec(
            codec=s.get("fmt.codec"), rate=s.get("fmt.rate"), channels=s.get("fmt.channels"),
            wav_subtype=s.get("fmt.wav_subtype"), flac_bits=s.get("fmt.flac_bits"),
            flac_level=s.get("fmt.flac_level"), mp3_mode=s.get("fmt.mp3.mode"),
            mp3_kbps=s.get("fmt.mp3.kbps"), mp3_vbr_q=s.get("fmt.mp3.vbr_q"),
        ).normalised()

    def to_json(self) -> dict:
        return dict(self.__dict__)

    @staticmethod
    def from_json(d: dict) -> "FormatSpec":
        f = FormatSpec()
        for k, v in d.items():
            if hasattr(f, k):
                setattr(f, k, v)
        return f

    def describe(self) -> str:
        if self.codec == "mp3":
            q = f"{self.mp3_kbps} kbit/s CBR" if self.mp3_mode == "cbr" else f"VBR V{self.mp3_vbr_q}"
            return f"MP3 {fmt_rate(self.rate)} {'stereo' if self.channels == 2 else 'mono'} {q}"
        if self.codec == "wav":
            return f"WAV {WAV_SUBTYPES.get(self.wav_subtype, self.wav_subtype)} {fmt_rate(self.rate)} " \
                   f"{'stereo' if self.channels == 2 else 'mono'}"
        return f"FLAC {self.flac_bits}-bit {fmt_rate(self.rate)} {'stereo' if self.channels == 2 else 'mono'}"

    def bytes_per_second(self) -> float:
        if self.codec == "mp3":
            kbps = self.mp3_kbps if self.mp3_mode == "cbr" else {0: 245, 1: 225, 2: 190, 3: 175, 4: 165,
                                                                 5: 130, 6: 115, 7: 100, 8: 85, 9: 65}[self.mp3_vbr_q]
            if self.mp3_mode == "vbr":
                kbps = kbps * min(1.0, self.rate / 44100) * (1.0 if self.channels == 2 else 0.6)
            return kbps * 1000 / 8
        if self.codec == "wav":
            bits = {"PCM_U8": 8, "PCM_16": 16, "PCM_24": 24, "FLOAT": 32, "ULAW": 8, "ALAW": 8}[self.wav_subtype]
            return self.rate * self.channels * bits / 8
        return self.rate * self.channels * self.flac_bits / 8 * 0.6


@dataclass
class FileModel:
    path: str
    codec: str                   # mp3 | wav | flac | other
    rate: int
    channels: int
    frames: int
    size: int
    subtype: str = ""            # libsndfile subtype or wav subtype
    kbps: int | None = None
    segments: list[Segment] = field(default_factory=lambda: [Segment(0)])
    recording: bool = False
    readonly: bool = False
    title: str = ""
    comment: str = ""
    tags: dict = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return self.frames / self.rate if self.rate else 0.0

    def segment_starts(self) -> list[int]:
        return [s.start for s in self.segments]

    def segment_index_at(self, frame: int) -> int:
        """0-based index of the segment that contains frame."""
        starts = self.segment_starts()
        if not starts:
            return 0
        return max(0, bisect.bisect_right(starts, frame) - 1)

    def sample_text(self) -> str:
        """'16 kHz ST', '44.1 kHz 16b MO' (FR-UI-007 Sample field)."""
        rate = fmt_rate(self.rate)
        mo = "ST" if self.channels >= 2 else "MO"
        bits = ""
        st = self.subtype.upper()
        if self.codec == "wav" or self.codec == "flac":
            b = {"PCM_U8": 8, "PCM_S8": 8, "PCM_16": 16, "PCM_24": 24, "PCM_32": 32, "FLOAT": 32,
                 "DOUBLE": 64, "ULAW": 8, "ALAW": 8}.get(st)
            if b:
                bits = f" {b}b"
        return f"{rate}{bits} {mo}"


def fmt_rate(rate: int) -> str:
    k = rate / 1000
    return f"{k:g} kHz"


def fmt_duration(seconds: float) -> str:
    """m:ss under an hour, h:mm:ss above (FR-UI-007)."""
    s = int(max(0, seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def fmt_time_precise(seconds: float) -> str:
    seconds = max(0.0, seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(int(m), 60)
    return f"{h}:{m:02d}:{s:04.1f}" if h else f"{m}:{s:04.1f}"


def fmt_size(nbytes: int) -> str:
    """KB under 1 MB, MB to three decimals under 1 GB, then GB (FR-UI-007)."""
    if nbytes < 1024 * 1024:
        return f"{nbytes / 1024:.0f}KB"
    if nbytes < 1024 ** 3:
        return f"{nbytes / 1024 / 1024:.3f}MB"
    return f"{nbytes / 1024 ** 3:.2f}GB"


def iso_now(dt: datetime | None = None) -> str:
    dt = (dt or datetime.now()).astimezone()
    return dt.isoformat(timespec="milliseconds")


def parse_time(text: str) -> float:
    """Parse ss, m:ss, h:mm:ss or decimal seconds (CLI times)."""
    text = text.strip()
    if not text:
        raise ValueError("empty time")
    parts = text.split(":")
    if len(parts) > 3:
        raise ValueError(f"bad time: {text}")
    total = 0.0
    for p in parts:
        total = total * 60 + float(p)
    return total
