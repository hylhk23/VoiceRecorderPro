"""The Live Transcript pack: where it is and whether it is complete.

The pack is a separate zip that is unzipped into the program folder. It adds
  python\\Lib\\site-packages\\sherpa_onnx...       the speech engine (and ONNX Runtime)
  python\\Lib\\site-packages\\opencc...            Chinese character conversion
  transcribe\\pack.json                          this description, checked field by field
  transcribe\\models\\sensevoice-small-int8\\     model.int8.onnx, tokens.txt
  transcribe\\models\\silero-vad\\                silero_vad.onnx
  transcribe\\LICENCES\\                          the licences of all of the above
Nothing is downloaded: the program never goes online for the live transcript.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from vrp import paths

PACK_FORMAT = 1
FOR_APP = "1.1"
MODEL_FOLDER = "sensevoice-small-int8"
VAD_FOLDER = "silero-vad"
MAX_PACK_JSON = 64 * 1024


@dataclass
class PackStatus:
    ok: bool
    reason: str = ""
    folder: str = ""
    model: str = ""
    tokens: str = ""
    vad: str = ""
    version: str = ""
    model_name: str = ""
    files: list = field(default_factory=list)      # [(relative path, size, sha256)]


def pack_dir() -> Path:
    env = os.environ.get("VRP_LT_PACK")
    return Path(env) if env else paths.app_dir() / "transcribe"


def _engine_missing() -> str:
    for mod, what in (("sherpa_onnx", "the speech engine (sherpa-onnx)"), ("opencc", "OpenCC")):
        try:
            if importlib.util.find_spec(mod) is None:
                return what
        except (ImportError, ValueError):
            return what
    return ""


def read_pack_json(folder: Path) -> dict:
    """pack.json, checked field by field; raises ValueError with the reason."""
    p = folder / "pack.json"
    if p.stat().st_size > MAX_PACK_JSON:
        raise ValueError("pack.json is too large")
    obj = json.loads(p.read_text(encoding="utf-8-sig"))
    if not isinstance(obj, dict):
        raise ValueError("pack.json is not an object")
    if obj.get("format") != PACK_FORMAT:
        raise ValueError(f"pack format {obj.get('format')!r} is not supported")
    if not isinstance(obj.get("for_app"), str) or not obj["for_app"].startswith(FOR_APP):
        raise ValueError(f"the pack is for Voice Recorder Pro {obj.get('for_app')}, not {FOR_APP}")
    if not isinstance(obj.get("version"), str) or len(obj["version"]) > 20:
        raise ValueError("pack version missing")
    files = obj.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("pack file list missing")
    clean = []
    for f in files:
        if not isinstance(f, dict):
            raise ValueError("pack file entry is not an object")
        rel, size, sha = f.get("path"), f.get("size"), f.get("sha256")
        if (not isinstance(rel, str) or rel.startswith(("/", "\\")) or ".." in rel.replace("\\", "/").split("/")
                or not isinstance(size, int) or size <= 0 or not isinstance(sha, str) or len(sha) != 64):
            raise ValueError(f"pack file entry {rel!r} is not valid")
        clean.append((rel.replace("\\", "/"), size, sha.lower()))
    obj["files"] = clean
    return obj


def status(folder: Path | None = None) -> PackStatus:
    folder = Path(folder) if folder else pack_dir()
    if not (folder / "pack.json").exists():
        return PackStatus(False, "The Live Transcript pack is not installed.", str(folder))
    try:
        info = read_pack_json(folder)
    except (OSError, ValueError) as exc:
        return PackStatus(False, f"The Live Transcript pack is damaged: {exc}.", str(folder))
    for rel, size, _sha in info["files"]:
        p = folder / rel
        try:
            if p.stat().st_size != size:
                return PackStatus(False, f"The Live Transcript pack is incomplete: {rel} has the wrong size. "
                                         "Unzip the pack again.", str(folder))
        except OSError:
            return PackStatus(False, f"The Live Transcript pack is incomplete: {rel} is missing. "
                                     "Unzip the pack again.", str(folder))
    missing = _engine_missing()
    if missing:
        return PackStatus(False, f"The Live Transcript pack is incomplete: {missing} is missing from the "
                                 "python folder. Unzip the pack again into the program folder.", str(folder))
    m = folder / "models" / MODEL_FOLDER
    v = folder / "models" / VAD_FOLDER
    for need in (m / "model.int8.onnx", m / "tokens.txt", v / "silero_vad.onnx"):
        if not need.is_file():
            return PackStatus(False, f"The Live Transcript pack is incomplete: {need.name} is missing. "
                                     "Unzip the pack again.", str(folder))
    return PackStatus(True, "", str(folder), str(m / "model.int8.onnx"), str(m / "tokens.txt"),
                      str(v / "silero_vad.onnx"), info["version"], str(info.get("model_name", "SenseVoiceSmall")),
                      info["files"])


def verify(st: PackStatus, progress=lambda _f: None) -> list[str]:
    """Full SHA-256 check of every pack file; returns the files that do not match."""
    bad = []
    for i, (rel, _size, sha) in enumerate(st.files):
        h = hashlib.sha256()
        try:
            with open(paths.longpath(Path(st.folder) / rel), "rb") as f:
                for block in iter(lambda: f.read(1 << 20), b""):
                    h.update(block)
        except OSError:
            bad.append(rel)
            continue
        if h.hexdigest() != sha:
            bad.append(rel)
        progress((i + 1) / len(st.files))
    return bad
