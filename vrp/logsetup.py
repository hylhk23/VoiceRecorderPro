"""Rotating log files (NFR-010)."""
from __future__ import annotations

import logging
import logging.handlers
import os
import platform
import sys
import time

from vrp import __version__, paths

_configured = False


def setup(level: str = "INFO", keep_days: int = 14, console: bool = False) -> str:
    global _configured
    log_dir = paths.log_dir()
    # delete logs older than keep_days
    cutoff = time.time() - keep_days * 86400
    for p in log_dir.glob("vrp*.log*"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass
    root = logging.getLogger()
    if _configured:
        root.setLevel(getattr(logging, level, logging.INFO))
        return str(log_dir)
    root.setLevel(getattr(logging, level, logging.INFO))
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(threadName)s %(name)s: %(message)s")
    try:
        fh = logging.handlers.RotatingFileHandler(log_dir / "vrp.log", maxBytes=5 * 1024 * 1024,
                                                  backupCount=10, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError:
        pass
    if console:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        sh.setLevel(logging.WARNING)
        root.addHandler(sh)
    _configured = True
    log_startup_info()
    return str(log_dir)


def log_startup_info() -> None:
    log = logging.getLogger("vrp")
    log.info("Voice Recorder Pro %s starting (pid %s)", __version__, os.getpid())
    log.info("OS: %s %s (%s); Python %s", platform.system(), platform.version(), platform.machine(),
             sys.version.split()[0])
    libs = []
    for name in ("numpy", "soundfile", "sounddevice", "lameenc", "soxr", "PySide6", "serial", "hid"):
        try:
            mod = __import__(name)
            libs.append(f"{name} {getattr(mod, '__version__', '?')}")
        except Exception:
            libs.append(f"{name} (missing)")
    log.info("Libraries: %s", ", ".join(libs))
