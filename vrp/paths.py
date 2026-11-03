"""Locations of settings, logs and recordings (FR-CFG-001, FR-CFG-007, FR-FILE-001)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from vrp import APP_ID

IS_WINDOWS = sys.platform == "win32"


def app_dir() -> Path:
    """Folder holding the executable (frozen) or the project root (source)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_dir() -> Path:
    """Folder holding bundled read-only files (docs). PyInstaller 6 puts them in _internal."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", app_dir()))
    return app_dir()


def is_portable() -> bool:
    return (app_dir() / "portable.flag").exists()


def _known_folder(guid: str) -> Path | None:
    if not IS_WINDOWS:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

        import uuid
        u = uuid.UUID(guid)
        g = GUID(u.fields[0], u.fields[1], u.fields[2],
                 (ctypes.c_ubyte * 8).from_buffer_copy(u.bytes[8:]))
        ptr = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(ptr)) != 0:
            return None
        path = ptr.value
        ctypes.windll.ole32.CoTaskMemFree(ptr)
        return Path(path)
    except Exception:
        return None


def documents_dir() -> Path:
    p = _known_folder("FDD39AD0-238F-46AF-ADB4-6C85480369C7")  # FOLDERID_Documents
    if p and p.exists():
        return p
    home = Path.home()
    docs = home / "Documents"
    return docs if docs.exists() else home


def config_dir() -> Path:
    if is_portable():
        d = app_dir() / "data"
    elif IS_WINDOWS:
        d = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / APP_ID
    else:
        d = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_ID
    override = os.environ.get("VRP_CONFIG_DIR")
    if override:
        d = Path(override)
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_dir() -> Path:
    if is_portable():
        d = app_dir() / "data" / "Logs"
    elif IS_WINDOWS:
        d = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / APP_ID / "Logs"
    else:
        d = config_dir() / "logs"
    override = os.environ.get("VRP_CONFIG_DIR")
    if override:
        d = Path(override) / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def default_recordings_dir() -> Path:
    return documents_dir() / "VRP Recordings"


def default_fallback_dir() -> Path:
    if IS_WINDOWS:
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / APP_ID
        return base / "Recordings (fallback)"
    return documents_dir() / "VRP Recordings (local)"


def longpath(path: str | os.PathLike) -> str:
    """Return a path usable beyond 260 characters on Windows (FR-FILE-012)."""
    s = os.fspath(path)
    if IS_WINDOWS and len(s) >= 240 and not s.startswith("\\\\?\\"):
        s = os.path.abspath(s)
        if s.startswith("\\\\"):
            return "\\\\?\\UNC\\" + s[2:]
        return "\\\\?\\" + s
    return s
