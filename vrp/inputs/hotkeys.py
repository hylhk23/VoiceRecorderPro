"""System-wide hot-keys (Section 11).

Windows: a dedicated thread registers every binding with RegisterHotKey
(MOD_NOREPEAT, so auto-repeat never fires an action twice - FR-HKEY-007) and
runs its own message loop. Hold-type actions (push-to-talk, scan) poll
GetAsyncKeyState after the press to report the release. Bindings the OS
refuses are reported per action (FR-HKEY-004).
"""
from __future__ import annotations

import logging
import sys
import threading

from PySide6.QtCore import QObject, Signal

from vrp.inputs import keymap
from vrp.settings import HOLD_ACTIONS, HOTKEY_ACTIONS

log = logging.getLogger(__name__)
IS_WINDOWS = sys.platform == "win32"

WM_HOTKEY = 0x0312
WM_APP = 0x8000
WM_QUIT = 0x0012
PM_REMOVE = 0x0001
QS_ALLINPUT = 0x04FF


class HotkeyManager(QObject):
    """Emits triggered(action, pressed) on the GUI thread."""

    triggered = Signal(str, bool)
    statusChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.supported = IS_WINDOWS
        self.status: dict[str, str] = {}       # action -> '' (ok) or error text
        self.suspended = False
        self._bindings: dict[str, str] = {}
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._lock = threading.Lock()
        self._pending = False
        self._ready = threading.Event()

    # ----------------------------------------------------------------- public
    def apply(self, bindings: dict, media_keys: bool) -> None:
        """bindings: {action: {'keys': 'Ctrl+Alt+R', 'scope': 'global'|'window'}}"""
        b = {}
        for action, _label, _default in HOTKEY_ACTIONS:
            entry = bindings.get(action) or {}
            keys = (entry.get("keys") or "").strip()
            if keys and entry.get("scope", "global") == "global":
                b[action] = keys
        if media_keys:
            for action, key in keymap.MEDIA_KEYS.items():
                b.setdefault(f"{action}#media", key)
        with self._lock:
            self._bindings = b
            self._pending = True
        if not self.supported:
            self.status = {a: "Global hot-keys are only available on Windows in this version." for a in b}
            self.statusChanged.emit()
            return
        self._ensure_thread()
        self._post_reload()

    def set_suspended(self, on: bool) -> None:
        self.suspended = on
        with self._lock:
            self._pending = True
        if self.supported:
            self._ensure_thread()
            self._post_reload()

    def stop(self) -> None:
        if self._thread and self._thread.is_alive() and IS_WINDOWS:
            import ctypes
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            self._thread.join(timeout=2)
        self._thread = None

    # ------------------------------------------------------------ win thread
    def _ensure_thread(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="vrp-hotkeys", daemon=True)
        self._thread.start()
        self._ready.wait(2)

    def _post_reload(self) -> None:
        import ctypes
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_APP + 1, 0, 0)

    def _run(self) -> None:  # pragma: no cover - Windows only
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetAsyncKeyState.restype = ctypes.c_short
        self._thread_id = kernel32.GetCurrentThreadId()
        msg = wintypes.MSG()
        # make sure the thread has a message queue before anyone posts to it
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
        self._ready.set()
        registered: dict[int, tuple[str, int]] = {}      # id -> (action, vk)
        held: dict[int, tuple[str, int]] = {}            # id -> (action, vk) waiting for release

        def reload():
            for hid in list(registered):
                user32.UnregisterHotKey(None, hid)
            registered.clear()
            with self._lock:
                bindings = dict(self._bindings)
                self._pending = False
            status = {}
            if not self.suspended:
                for i, (action, keys) in enumerate(sorted(bindings.items()), start=1):
                    parsed = keymap.parse(keys)
                    base = action.split("#")[0]
                    if parsed is None:
                        status[base] = "This key combination is not supported."
                        continue
                    mods, vk = parsed
                    if user32.RegisterHotKey(None, i, mods | keymap.MOD_NOREPEAT, vk):
                        registered[i] = (base, vk)
                        status.setdefault(base, "")
                    else:
                        status[base] = "In use by another application"
                        log.warning("hot-key %s for %s refused by Windows", keys, base)
            self.status = status
            self.statusChanged.emit()

        reload()
        while True:
            if held:
                # poll for key release while something is held (20 ms)
                user32.MsgWaitForMultipleObjects(0, None, False, 20, QS_ALLINPUT)
                got = user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE)
                for hid, (action, vk) in list(held.items()):
                    if not (user32.GetAsyncKeyState(vk) & 0x8000):
                        del held[hid]
                        self.triggered.emit(action, False)
                if not got:
                    continue
            else:
                r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if r <= 0:
                    break
            if msg.message == WM_QUIT:
                break
            if msg.message == WM_APP + 1:
                reload()
            elif msg.message == WM_HOTKEY:
                entry = registered.get(int(msg.wParam))
                if entry:
                    action, vk = entry
                    self.triggered.emit(action, True)
                    if action in HOLD_ACTIONS:
                        held[int(msg.wParam)] = (action, vk)
        for hid in list(registered):
            user32.UnregisterHotKey(None, hid)
        for action, _vk in held.values():
            self.triggered.emit(action, False)

