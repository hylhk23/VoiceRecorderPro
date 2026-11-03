"""File-name patterns (Section 8.1, FR-FILE-002/003)."""
from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]
TOKENS = ["{yyyy}", "{yy}", "{mm}", "{Mon}", "{Month}", "{dd}", "{HH}", "{MM}", "{SS}",
          "{n}", "{n3}", "{host}", "{device}", "{tag}", "{Name}"]
_TOKEN_RE = re.compile(r"\{(yyyy|yy|mm|Mon|Month|dd|HH|MM|SS|n|n3|host|device|tag|Name)\}")
_INVALID = re.compile(r'[<>:"|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}
AUDIO_EXTS = (".mp3", ".wav", ".flac")


def safe_component(text: str) -> str:
    """Strip characters that are not allowed in a Windows file name."""
    s = _INVALID.sub("", text).replace("/", "-").replace("\\", "-")
    return s.strip().strip(".")


def expand(pattern: str, when: datetime, n: int = 1, host: str = "", device: str = "",
           tag: str = "", name: str = "") -> str:
    def repl(m: re.Match) -> str:
        t = m.group(1)
        return {
            "yyyy": f"{when.year:04d}", "yy": f"{when.year % 100:02d}", "mm": f"{when.month:02d}",
            "Mon": MONTHS[when.month - 1][:3], "Month": MONTHS[when.month - 1],
            "dd": f"{when.day:02d}", "HH": f"{when.hour:02d}", "MM": f"{when.minute:02d}",
            "SS": f"{when.second:02d}", "n": str(n), "n3": f"{n:03d}",
            "host": safe_component(host), "device": safe_component(device)[:24].replace(" ", "-"),
            "tag": safe_component(tag), "Name": safe_component(name),
        }[t]
    return _TOKEN_RE.sub(repl, pattern)


def validate_pattern(pattern: str) -> str | None:
    """Return an error message, or None when the pattern is usable."""
    if not pattern.strip():
        return "The pattern is empty."
    sample = expand(pattern, datetime(2026, 10, 2, 14, 3, 21), 1, "PC", "Mic", "Tag", "Name")
    parts = re.split(r"[\\/]", sample)
    for part in parts:
        if not part:
            return "The pattern contains an empty folder name."
        if _INVALID.search(part):
            return 'File names cannot contain < > : " | ? * characters.'
        if part.strip().strip(".") != part or part.endswith(" "):
            return "File and folder names cannot start or end with a space or dot."
        if part.split(".")[0].upper() in _RESERVED:
            return f'"{part}" is a reserved name on Windows.'
    if os.path.isabs(sample) or re.match(r"^[A-Za-z]:", sample):
        return "The pattern must be relative to the recordings folder."
    return None


def _exists_any(base: Path) -> bool:
    """True when base.<any audio ext> (or a journal for it) already exists."""
    for ext in AUDIO_EXTS:
        if base.with_name(base.name + ext).exists():
            return True
    return False


def next_path(folder: str | os.PathLike, pattern: str, ext: str, when: datetime | None = None,
              host: str = "", device: str = "", tag: str = "", name: str = "",
              start_n: int = 1) -> Path:
    """Build the next free file name; never returns an existing file (FR-FILE-002).

    {n} is the lowest number >= start_n that does not clash with an existing
    recording of any format. Without {n} in the pattern a clash appends _2, _3, ...
    """
    when = when or datetime.now()
    folder = Path(folder)
    has_n = "{n}" in pattern or "{n3}" in pattern
    if has_n:
        for n in range(start_n, start_n + 1000000):
            base = folder / expand(pattern, when, n, host, device, tag, name)
            if not _exists_any(base):
                return base.with_name(base.name + ext)
        raise RuntimeError("could not find a free file name")
    base = folder / expand(pattern, when, start_n, host, device, tag, name)
    if start_n <= 1 and not _exists_any(base):
        return base.with_name(base.name + ext)
    for k in range(max(2, start_n), 1000000):
        cand = base.with_name(f"{base.name}_{k}")
        if not _exists_any(cand):
            return cand.with_name(cand.name + ext)
    raise RuntimeError("could not find a free file name")


def preview(folder: str | os.PathLike, pattern: str, ext: str, **kw) -> str:
    err = validate_pattern(pattern)
    if err:
        return err
    return str(next_path(folder, pattern, ext, **kw))
