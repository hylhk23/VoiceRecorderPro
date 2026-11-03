"""Small Windows helpers: sleep blocking, Recycle Bin, run-at-login, settings pages.

Every function degrades to a harmless fallback on other systems so the core
can be tested anywhere.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger(__name__)
IS_WINDOWS = sys.platform == "win32"

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_AWAYMODE_REQUIRED = 0x00000040


def keep_awake(on: bool) -> None:
    """Stop Windows sleeping while recording or armed (FR-REC-012).

    Must be called from the GUI thread; the request is per-thread.
    """
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except Exception:
        log.exception("SetThreadExecutionState failed")


def send_to_recycle_bin(path: str | os.PathLike) -> bool:
    """Move a file to the Recycle Bin, never deleting permanently (FR-FILE-010)."""
    p = os.path.abspath(os.fspath(path))
    if not os.path.exists(p):
        return True
    if IS_WINDOWS:
        try:
            import ctypes
            from ctypes import wintypes

            class SHFILEOPSTRUCTW(ctypes.Structure):
                _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
                            ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                            ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                            ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

            FO_DELETE = 3
            FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 0x4, 0x10, 0x40, 0x400
            op = SHFILEOPSTRUCTW()
            op.wFunc = FO_DELETE
            op.pFrom = p + "\0"
            op.fFlags = FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI
            res = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
            if res == 0 and not op.fAnyOperationsAborted:
                return True
            log.error("SHFileOperationW failed (%s) for %s", res, p)
            return False
        except Exception:
            log.exception("recycle bin failed for %s", p)
            return False
    # Non-Windows fallback (tests): move to a .trash folder next to the file.
    trash = Path(p).parent / ".trash"
    trash.mkdir(exist_ok=True)
    dest = trash / f"{int(time.time() * 1000)}_{Path(p).name}"
    shutil.move(p, dest)
    return True


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "VoiceRecorderPro"


def gui_command() -> str:
    """Command line that starts the GUI (frozen exe or pythonw -m vrp)."""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable)
        gui = exe.with_name("VoiceRecorderPro.exe")
        return f'"{gui if gui.exists() else exe}"'
    pyw = Path(sys.executable).with_name("pythonw.exe")
    py = pyw if pyw.exists() else Path(sys.executable)
    launcher = Path(__file__).resolve().parent.parent / "packaging" / "launcher.py"
    return f'"{py}" "{launcher}"'


def set_run_at_login(enable: bool) -> bool:
    if not IS_WINDOWS:
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if enable:
                winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, gui_command() + " --minimised")
            else:
                try:
                    winreg.DeleteValue(k, RUN_VALUE)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        log.exception("could not change run-at-login")
        return False


def open_mic_privacy_settings() -> None:
    if IS_WINDOWS:
        os.startfile("ms-settings:privacy-microphone")  # type: ignore[attr-defined]


def open_sound_settings() -> None:
    if IS_WINDOWS:
        os.startfile("ms-settings:sound")  # type: ignore[attr-defined]


def open_folder(path: str | os.PathLike, select: str | None = None) -> None:
    p = os.fspath(path)
    try:
        if IS_WINDOWS:
            if select and os.path.exists(select):
                subprocess.Popen(["explorer", "/select,", os.path.normpath(select)])
            else:
                os.startfile(p)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", p])
        else:
            subprocess.Popen(["xdg-open", p])
    except OSError:
        log.exception("could not open folder %s", p)


def drive_label(path: str | os.PathLike) -> str:
    """'E:' on Windows, the mount point elsewhere (FR-UI-008)."""
    p = os.path.abspath(os.fspath(path))
    if IS_WINDOWS:
        drive, _ = os.path.splitdrive(p)
        if drive.startswith("\\\\"):
            parts = drive.strip("\\").split("\\")
            return "\\\\" + parts[0] if parts else drive
        return drive or p[:2]
    # find mount point
    cur = Path(p)
    while not cur.exists():
        cur = cur.parent
    while not os.path.ismount(cur) and cur != cur.parent:
        cur = cur.parent
    return str(cur)


def existing_parent(path: str | os.PathLike) -> Path:
    cur = Path(os.path.abspath(os.fspath(path)))
    while not cur.exists() and cur != cur.parent:
        cur = cur.parent
    return cur


def free_bytes(path: str | os.PathLike) -> int | None:
    try:
        return shutil.disk_usage(existing_parent(path)).free
    except OSError:
        return None


def is_network_path(path: str | os.PathLike) -> bool:
    p = os.path.abspath(os.fspath(path))
    if p.startswith("\\\\"):
        return True
    if IS_WINDOWS:
        try:
            import ctypes
            drive = os.path.splitdrive(p)[0] + "\\"
            DRIVE_REMOTE = 4
            return ctypes.windll.kernel32.GetDriveTypeW(drive) == DRIVE_REMOTE
        except Exception:
            return False
    return False


def computer_name() -> str:
    name = os.environ.get("COMPUTERNAME")
    if not name and hasattr(os, "uname"):
        name = os.uname().nodename
    return name or "PC"


def set_app_user_model_id(app_id: str) -> None:
    """Give the taskbar our own icon/grouping instead of python.exe's."""
    if IS_WINDOWS:
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
        except Exception:
            pass
