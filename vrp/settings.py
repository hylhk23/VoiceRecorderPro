"""Settings schema, defaults, validation and persistence (Section 15).

Settings are a flat dict of dotted keys stored as UTF-8 JSON in settings.json.
Writes are atomic and keep settings.json.bak (FR-CFG-002). Invalid files are
renamed and replaced by defaults or the backup (FR-CFG-003).
"""
from __future__ import annotations

import copy
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from vrp import paths

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1


@dataclass
class Spec:
    default: Any
    kind: type
    lo: float | None = None
    hi: float | None = None
    choices: tuple | None = None


def S(default, kind=None, lo=None, hi=None, choices=None) -> Spec:
    return Spec(default, kind or type(default), lo, hi, tuple(choices) if choices else None)


# Hot-key actions and their default global bindings (Section 11.1).
HOTKEY_ACTIONS: list[tuple[str, str, str]] = [
    # action id, label, default binding
    ("record", "Record / Pause recording", "Ctrl+Alt+R"),
    ("ptt", "Push-to-talk (records while held)", ""),
    ("stop", "Stop", "Ctrl+Alt+S"),
    ("play", "Play / Pause", "Ctrl+Alt+P"),
    ("rewind", "Rewind (tap jumps, hold scans)", "Ctrl+Alt+B"),
    ("ffwd", "Fast Forward (tap jumps, hold scans)", "Ctrl+Alt+F"),
    ("skip_back", "Skip back", ""),
    ("skip_fwd", "Skip forward", ""),
    ("prev_segment", "Previous segment", ""),
    ("next_segment", "Next segment", ""),
    ("vox_toggle", "Vox on/off", "Ctrl+Alt+V"),
    ("source_mic", "Microphone on/off (recording source)", ""),
    ("source_system", "Computer audio on/off (recording source)", ""),
    ("mark_in", "Set In mark", "Ctrl+Alt+I"),
    ("mark_out", "Set Out mark", "Ctrl+Alt+O"),
    ("extract", "Extract selection to file", ""),
    ("insert_boundary", "Insert segment boundary", "Ctrl+Alt+M"),
    ("new_recording", "New recording", ""),
    ("speed_down", "Speed down (5%)", ""),
    ("speed_up", "Speed up (5%)", ""),
    ("volume_down", "Volume down (10%)", ""),
    ("volume_up", "Volume up (10%)", ""),
    ("show_hide", "Show / hide main window", "Ctrl+Alt+H"),
    ("suspend_hotkeys", "Suspend / resume all hot-keys", ""),
]

# Actions usable from hot-keys and external devices but with momentary
# (press + release) behaviour.
HOLD_ACTIONS = {"ptt", "rewind", "ffwd", "play_held", "record_active"}

EXTERNAL_ONLY_ACTIONS = [
    ("play_held", "Play while held (release pauses)"),
    ("record_active", "Record while active"),
]


def default_hotkeys() -> dict:
    return {a: {"keys": k, "scope": "global"} for a, _, k in HOTKEY_ACTIONS}


SCHEMA: dict[str, Spec] = {
    # General
    "general.language": S("system", choices=["system", "en"]),
    "general.theme": S("system", choices=["system", "light", "dark"]),
    "general.start_minimised": S(False),
    "general.run_at_login": S(False),
    "general.close_to_tray": S(True),
    "general.always_on_top": S(False),
    "general.notifications": S(True),
    "general.user_name": S(""),
    "general.first_run_done": S(False),
    "general.recent_files": S([], list),
    "general.window_geometry": S(""),
    "general.last_open_dir": S(""),
    # Recording
    "rec.folder": S(""),
    "rec.fallback_folder": S(""),
    "rec.pattern": S("{Mon}{dd}_{n}"),
    "rec.tag": S(""),
    "rec.comment": S(""),
    "rec.open_file_action": S("append", choices=["append", "new", "ask"]),
    "rec.timestamps": S(True),
    "rec.rollover.size_mb": S(0, int, 0, 3900),
    "rec.rollover.minutes": S(0, int, 0, 1440),
    "rec.rollover.midnight": S(False),
    "rec.rollover.every_hours": S(0, int, 0, 24),
    "rec.size_action": S("rollover", choices=["rollover", "stop"]),
    "rec.flush_s": S(1, int, 1, 30),
    "rec.fsync_s": S(1, int, 1, 30),
    "rec.disk_warn_mb": S(500, int, 10, 100000),
    "rec.disk_stop_mb": S(100, int, 5, 100000),
    "rec.sidecar_json": S(False),
    "rec.after_command": S(""),
    "rec.autostop.silence_min": S(0, int, 0, 120),
    "rec.autostop.clock": S(""),
    "rec.network_warned": S(False),
    # Recording sources (v1.0.2): microphone, computer audio (WASAPI loopback), or both
    "rec.src_mic": S(True),
    "rec.src_system": S(False),
    "rec.system_gain_db": S(0, int, -20, 20),
    # Format
    "fmt.codec": S("mp3", choices=["mp3", "wav", "flac"]),
    "fmt.rate": S(16000, int, 8000, 96000),
    "fmt.channels": S(1, int, 1, 2),
    "fmt.wav_subtype": S("PCM_16", choices=["PCM_U8", "PCM_16", "PCM_24", "FLOAT", "ULAW", "ALAW"]),
    "fmt.flac_bits": S(16, int, 16, 24),
    "fmt.flac_level": S(5, int, 0, 8),
    "fmt.mp3.mode": S("cbr", choices=["cbr", "vbr"]),
    "fmt.mp3.kbps": S(32, int, 8, 320),
    "fmt.mp3.vbr_q": S(5, int, 0, 9),
    "fmt.gain_db": S(0, int, -20, 20),
    "fmt.highpass": S(False),
    # Vox
    "vox.enabled": S(True),
    "vox.threshold": S(4, int, 0, 100),
    "vox.window_ms": S(20, int, 5, 50),
    "vox.attack_ms": S(20, int, 0, 500),
    "vox.hang_s": S(2.0, float, 0.2, 30.0),
    "vox.preroll_ms": S(300, int, 0, 5000),
    "vox.min_seg_s": S(0.5, float, 0.0, 5.0),
    "vox.mode": S("segment", choices=["segment", "file"]),
    "vox.gap_ms": S(0, int, 0, 2000),
    "vox.disabled": S(False),
    "vox.hidden": S(False),
    "ui.meter_scale": S("linear", choices=["linear", "db"]),
    # Playback
    "play.volume": S(100, int, 0, 100),
    "play.speed": S(100, int, 50, 200),
    "play.backspace_s": S(2.0, float, 0.0, 10.0),
    "play.skip_s": S(5.0, float, 1.0, 60.0),
    "play.audible_scan": S(True),
    "play.at_end": S("stay", choices=["stay", "start"]),
    # Hot-keys
    "hotkeys.bindings": S(default_hotkeys(), dict),
    "hotkeys.media_keys": S(False),
    "hotkeys.tones": S("hidden", choices=["hidden", "always", "never"]),
    "hotkeys.tone_volume": S(30, int, 0, 100),
    # External devices
    "ext.enabled": S(True),
    "ext.mappings": S([], list),
    "ext.outputs": S([], list),
    "ext.profiles": S({}, dict),
    "ext.on_loss": S("keep", choices=["keep", "release"]),
    "ext.suspend_with_hotkeys": S(False),
    # Sound devices
    "dev.host_api": S(""),
    "dev.input": S(""),
    "dev.output": S(""),
    "dev.input_channels": S([], list),
    "dev.exclusive": S(False),
    "dev.wait": S(False),
    "dev.fallback": S("default", choices=["default", "stop"]),
    # Advanced
    "adv.blocksize": S(1024, int, 128, 8192),
    "adv.latency": S("high", choices=["high", "low"]),
    "adv.log_level": S("INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"]),
    "adv.log_days": S(14, int, 1, 365),
    # Schedules
    "sched.entries": S([], list),
    "sched.late_minutes": S(5, int, 0, 60),
}

# Which Options tab each key belongs to (for "Restore defaults" per tab).
TAB_PREFIXES = {
    "General": ("general.",),
    "Recording": ("rec.",),
    "Format": ("fmt.",),
    "Vox": ("vox.", "ui.meter_scale"),
    "Playback": ("play.",),
    "Hot-keys": ("hotkeys.",),
    "External devices": ("ext.",),
    "Sound devices": ("dev.",),
    "Advanced": ("adv.",),
}

# Keys never touched by "Restore defaults" or export/import.
PRIVATE_KEYS = {"general.first_run_done", "general.recent_files", "general.window_geometry",
                "general.last_open_dir", "rec.network_warned"}


def defaults() -> dict:
    return {k: copy.deepcopy(s.default) for k, s in SCHEMA.items()}


def validate_value(key: str, value: Any) -> tuple[Any, bool]:
    """Return (clean value, changed?) for one key; unknown keys pass through."""
    spec = SCHEMA.get(key)
    if spec is None:
        return value, False
    try:
        if spec.kind is bool:
            if isinstance(value, bool):
                v = value
            elif isinstance(value, (int, float)):
                v = bool(value)
            elif isinstance(value, str):
                v = value.strip().lower() in ("1", "true", "yes", "on")
            else:
                raise ValueError
        elif spec.kind is int:
            v = int(round(float(value)))
        elif spec.kind is float:
            v = float(value)
        elif spec.kind is str:
            if not isinstance(value, str):
                raise ValueError
            v = value
        elif spec.kind is list:
            if not isinstance(value, list):
                raise ValueError
            v = value
        elif spec.kind is dict:
            if not isinstance(value, dict):
                raise ValueError
            v = value
        else:
            v = value
    except (TypeError, ValueError):
        return copy.deepcopy(spec.default), True
    changed = False
    if spec.lo is not None and isinstance(v, (int, float)) and v < spec.lo:
        v, changed = type(v)(spec.lo), True
    if spec.hi is not None and isinstance(v, (int, float)) and v > spec.hi:
        v, changed = type(v)(spec.hi), True
    if spec.choices is not None and v not in spec.choices:
        v, changed = copy.deepcopy(spec.default), True
    return v, changed


class Settings:
    """Thread-safe settings store with change listeners."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else paths.config_dir() / "settings.json"
        self._data: dict = defaults()
        self._lock = threading.RLock()
        self._listeners: list[Callable[[set], None]] = []
        self.read_only = False
        self.load_messages: list[str] = []

    # ------------------------------------------------------------------ access
    def get(self, key: str, default=None):
        with self._lock:
            if key in self._data:
                return copy.deepcopy(self._data[key])
            spec = SCHEMA.get(key)
            return copy.deepcopy(spec.default) if spec else default

    def __getitem__(self, key: str):
        return self.get(key)

    def set(self, key: str, value, save: bool = True) -> None:
        self.update({key: value}, save=save)

    def update(self, values: dict, save: bool = True) -> None:
        changed = set()
        with self._lock:
            for k, v in values.items():
                clean, _ = validate_value(k, v)
                if self._data.get(k) != clean:
                    self._data[k] = copy.deepcopy(clean)
                    changed.add(k)
        if changed:
            if save:
                self.save()
            for cb in list(self._listeners):
                try:
                    cb(changed)
                except Exception:  # pragma: no cover - listener bugs must not break saving
                    log.exception("settings listener failed")

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._data)

    def add_listener(self, cb: Callable[[set], None]) -> None:
        self._listeners.append(cb)

    def remove_listener(self, cb: Callable[[set], None]) -> None:
        if cb in self._listeners:
            self._listeners.remove(cb)

    # --------------------------------------------------------------- defaults
    def restore_defaults(self, prefixes: tuple[str, ...]) -> None:
        d = defaults()
        vals = {k: v for k, v in d.items()
                if k.startswith(prefixes) and k not in PRIVATE_KEYS}
        self.update(vals)

    # ---------------------------------------------------------- persistence
    @staticmethod
    def _parse(text: str) -> dict:
        obj = json.loads(text)
        if not isinstance(obj, dict) or not isinstance(obj.get("settings"), dict):
            raise ValueError("not a VRP settings file")
        return obj

    def _apply_loaded(self, obj: dict) -> None:
        ver = int(obj.get("schema_version", 1))
        data = defaults()
        clamped = []
        for k, v in obj["settings"].items():
            clean, changed = validate_value(k, v)
            if changed:
                clamped.append(k)
            data[k] = clean
        if clamped:
            log.warning("settings values clamped or reset: %s", ", ".join(sorted(clamped)))
            self.load_messages.append(
                "Some settings were out of range and were corrected: " + ", ".join(sorted(clamped)))
        if ver > SCHEMA_VERSION:
            self.read_only = True
            self.load_messages.append(
                "These settings were saved by a newer version of the app. They are used "
                "read-only; changes will not be saved.")
        with self._lock:
            self._data = data

    def load(self) -> None:
        self.load_messages.clear()
        if not self.path.exists():
            log.info("no settings file, using defaults")
            return
        try:
            obj = self._parse(self.path.read_text(encoding="utf-8"))
            ver = int(obj.get("schema_version", 1))
            if ver < SCHEMA_VERSION:
                bak = self.path.with_suffix(f".v{ver}.json")
                bak.write_text(self.path.read_text(encoding="utf-8"), encoding="utf-8")
            self._apply_loaded(obj)
            return
        except Exception as exc:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            bad = self.path.with_name(f"settings.invalid-{stamp}.json")
            try:
                os.replace(self.path, bad)
            except OSError:
                pass
            log.error("settings file unreadable (%s); moved to %s", exc, bad)
            msg = f"The settings file could not be read and was renamed to {bad.name}."
            bak_path = self.path.with_name("settings.json.bak")
            if bak_path.exists():
                try:
                    self._apply_loaded(self._parse(bak_path.read_text(encoding="utf-8")))
                    self.load_messages.append(msg + " The previous backup was loaded instead.")
                    self.save()
                    return
                except Exception:
                    pass
            self.load_messages.append(msg + " Default settings are in use.")

    def to_json(self, include_private: bool = True) -> str:
        data = self.snapshot()
        if not include_private:
            data = {k: v for k, v in data.items() if k not in PRIVATE_KEYS}
        return json.dumps({"schema_version": SCHEMA_VERSION, "app": "VoiceRecorderPro",
                           "settings": data}, indent=2, ensure_ascii=False, sort_keys=True)

    def save(self) -> None:
        if self.read_only:
            return
        text = self.to_json()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            if self.path.exists():
                try:
                    os.replace(self.path, self.path.with_name("settings.json.bak"))
                except OSError:
                    pass
            os.replace(tmp, self.path)
        except OSError:
            log.exception("could not save settings")

    def export_to(self, path: str) -> None:
        Path(path).write_text(self.to_json(include_private=False), encoding="utf-8")

    def import_from(self, path: str) -> list[str]:
        """Import a .vrpsettings file; returns keys that were clamped."""
        obj = self._parse(Path(path).read_text(encoding="utf-8"))
        vals, clamped = {}, []
        for k, v in obj["settings"].items():
            if k in PRIVATE_KEYS:
                continue
            clean, changed = validate_value(k, v)
            if changed:
                clamped.append(k)
            vals[k] = clean
        self.update(vals)
        return clamped

    # -------------------------------------------------------------- helpers
    def recordings_folder(self) -> Path:
        f = self.get("rec.folder")
        return Path(f) if f else paths.default_recordings_dir()

    def fallback_folder(self) -> Path:
        f = self.get("rec.fallback_folder")
        return Path(f) if f else paths.default_fallback_dir()

    def add_recent(self, path: str) -> None:
        recent = [p for p in self.get("general.recent_files") if os.path.normcase(p) != os.path.normcase(path)]
        recent.insert(0, path)
        self.set("general.recent_files", recent[:10])

    def recent_files(self) -> list[str]:
        """Recent files that still exist (FR-FILE-009)."""
        recent = self.get("general.recent_files")
        alive = [p for p in recent if os.path.exists(p)]
        if alive != recent:
            self.set("general.recent_files", alive)
        return alive
