"""Recording badge on the Windows taskbar button.

Uses ITaskbarList3::SetOverlayIcon, the small icon Windows draws over the
bottom-right corner of a taskbar button (the mechanism mail and chat apps use
for unread badges). It shows even when the taskbar button comes from a pinned
shortcut, where a changed window icon would not.

Windows forgets the badge whenever it re-creates the taskbar button (window
shown again from the tray, Always-on-Top toggled, Explorer restarted). It then
sends the registered "TaskbarButtonCreated" message; the main window passes
that on to on_native_message() and the badge is put back.

Everything here is a harmless no-op on other systems.
"""
from __future__ import annotations

import ctypes
import logging
import sys
import uuid

log = logging.getLogger(__name__)
IS_WINDOWS = sys.platform == "win32"

CLSID_TASKBAR_LIST = "56FDF344-FD6D-11D0-958A-006097C9A090"
IID_ITASKBAR_LIST3 = "EA1AFB91-9E28-4B86-90E9-9E9F8A5EEFAF"
VT_RELEASE = 2           # IUnknown::Release
VT_HRINIT = 3            # ITaskbarList::HrInit
VT_SET_OVERLAY_ICON = 18  # ITaskbarList3::SetOverlayIcon
SM_CXSMICON = 49
MSGFLT_ADD = 1
CLSCTX_INPROC_SERVER = 1
COINIT_APARTMENTTHREADED = 2


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16), ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_ubyte * 8)]


def make_guid(text: str) -> GUID:
    return GUID.from_buffer_copy(uuid.UUID(text).bytes_le)


def bgra_rows(img) -> tuple[int, int, bytes]:
    """Pixels of a QImage as tightly packed top-down BGRA rows (straight alpha), as icons expect."""
    from PySide6.QtGui import QImage
    img = img.convertToFormat(QImage.Format_ARGB32)       # little-endian memory order is B, G, R, A
    w, h, bpl = img.width(), img.height(), img.bytesPerLine()
    raw = bytes(img.constBits())
    if bpl != 4 * w:
        raw = b"".join(raw[y * bpl:y * bpl + 4 * w] for y in range(h))
    return w, h, raw[:4 * w * h]


class _Win32:
    """Private ctypes handles with explicit prototypes (64-bit safe, isolated from other modules)."""

    def __init__(self) -> None:
        from ctypes import wintypes as wt
        self.wt = wt
        self.user32 = u = ctypes.WinDLL("user32", use_last_error=True)
        self.gdi32 = g = ctypes.WinDLL("gdi32")
        self.ole32 = o = ctypes.WinDLL("ole32")

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
                        ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]

        class BITMAPINFO(ctypes.Structure):
            _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]

        class ICONINFO(ctypes.Structure):
            _fields_ = [("fIcon", wt.BOOL), ("xHotspot", wt.DWORD), ("yHotspot", wt.DWORD),
                        ("hbmMask", wt.HBITMAP), ("hbmColor", wt.HBITMAP)]

        self.BITMAPINFOHEADER, self.BITMAPINFO, self.ICONINFO = BITMAPINFOHEADER, BITMAPINFO, ICONINFO

        u.RegisterWindowMessageW.argtypes = [wt.LPCWSTR]
        u.RegisterWindowMessageW.restype = wt.UINT
        u.ChangeWindowMessageFilter.argtypes = [wt.UINT, wt.DWORD]
        u.ChangeWindowMessageFilter.restype = wt.BOOL
        u.GetSystemMetrics.argtypes = [ctypes.c_int]
        u.GetSystemMetrics.restype = ctypes.c_int
        try:
            u.GetDpiForWindow.argtypes = [wt.HWND]
            u.GetDpiForWindow.restype = wt.UINT
            u.GetSystemMetricsForDpi.argtypes = [ctypes.c_int, wt.UINT]
            u.GetSystemMetricsForDpi.restype = ctypes.c_int
            self.per_dpi = True
        except AttributeError:
            self.per_dpi = False
        u.GetDC.argtypes = [wt.HWND]
        u.GetDC.restype = wt.HDC
        u.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
        u.ReleaseDC.restype = ctypes.c_int
        u.CreateIconIndirect.argtypes = [ctypes.POINTER(ICONINFO)]
        u.CreateIconIndirect.restype = wt.HICON
        u.DestroyIcon.argtypes = [wt.HICON]
        u.DestroyIcon.restype = wt.BOOL
        g.CreateDIBSection.argtypes = [wt.HDC, ctypes.POINTER(BITMAPINFO), wt.UINT,
                                       ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD]
        g.CreateDIBSection.restype = wt.HBITMAP
        g.CreateBitmap.argtypes = [ctypes.c_int, ctypes.c_int, wt.UINT, wt.UINT, ctypes.c_void_p]
        g.CreateBitmap.restype = wt.HBITMAP
        g.DeleteObject.argtypes = [wt.HGDIOBJ]
        g.DeleteObject.restype = wt.BOOL
        o.CoInitializeEx.argtypes = [ctypes.c_void_p, wt.DWORD]
        o.CoInitializeEx.restype = ctypes.c_long
        o.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p, wt.DWORD,
                                       ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
        o.CoCreateInstance.restype = ctypes.c_long

    def small_icon_size(self, hwnd: int) -> int:
        if self.per_dpi:
            try:
                dpi = self.user32.GetDpiForWindow(hwnd)
                if dpi:
                    return self.user32.GetSystemMetricsForDpi(SM_CXSMICON, dpi) or 16
            except OSError:
                pass
        return self.user32.GetSystemMetrics(SM_CXSMICON) or 16

    def hicon_from_image(self, img) -> int | None:
        """32-bit HICON (with alpha) from a QImage; the caller destroys it."""
        w, h, raw = bgra_rows(img)
        bmi = self.BITMAPINFO()
        hdr = bmi.bmiHeader
        hdr.biSize = ctypes.sizeof(self.BITMAPINFOHEADER)
        hdr.biWidth, hdr.biHeight = w, -h            # negative height: top-down rows
        hdr.biPlanes, hdr.biBitCount, hdr.biCompression = 1, 32, 0
        bits = ctypes.c_void_p()
        hdc = self.user32.GetDC(None)
        try:
            color = self.gdi32.CreateDIBSection(hdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        finally:
            self.user32.ReleaseDC(None, hdc)
        if not color or not bits.value:
            return None
        mask = None
        try:
            ctypes.memmove(bits, raw, len(raw))
            stride = ((w + 15) // 16) * 2                 # monochrome rows are WORD aligned
            mask_bits = (ctypes.c_ubyte * (stride * h))()  # all zero: the alpha channel decides
            mask = self.gdi32.CreateBitmap(w, h, 1, 1, mask_bits)
            info = self.ICONINFO(True, 0, 0, mask, color)
            return self.user32.CreateIconIndirect(ctypes.byref(info)) or None
        finally:
            self.gdi32.DeleteObject(color)
            if mask:
                self.gdi32.DeleteObject(mask)


class _TaskbarList3:
    """Minimal ctypes wrapper around the shell's ITaskbarList3 COM object."""

    def __init__(self, api: _Win32):
        self.api = api
        api.ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)   # S_FALSE / changed mode are fine
        ptr = ctypes.c_void_p()
        clsid, iid = make_guid(CLSID_TASKBAR_LIST), make_guid(IID_ITASKBAR_LIST3)
        hr = api.ole32.CoCreateInstance(ctypes.byref(clsid), None, CLSCTX_INPROC_SERVER,
                                        ctypes.byref(iid), ctypes.byref(ptr))
        if hr < 0 or not ptr.value:
            raise OSError(f"CoCreateInstance(TaskbarList) failed: 0x{hr & 0xFFFFFFFF:08X}")
        self.ptr = ptr
        hr = self._method(VT_HRINIT)(self.ptr)
        if hr < 0:
            self._method(VT_RELEASE, restype=ctypes.c_ulong)(self.ptr)
            raise OSError(f"ITaskbarList::HrInit failed: 0x{hr & 0xFFFFFFFF:08X}")

    def _method(self, index: int, *argtypes, restype=ctypes.c_long):
        vtbl = ctypes.cast(self.ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
        return proto(vtbl[index])

    def set_overlay_icon(self, hwnd: int, hicon: int | None, description: str) -> int:
        wt = self.api.wt
        fn = self._method(VT_SET_OVERLAY_ICON, wt.HWND, wt.HICON, wt.LPCWSTR)
        return fn(self.ptr, hwnd, hicon, description)


class TaskbarBadge:
    """Keeps the taskbar button's badge in step with the recorder state."""

    def __init__(self, window, painter):
        """painter(kind, size) -> QImage draws the badge for 'recording', 'armed' or 'paused'."""
        self.window = window
        self.painter = painter
        self.wanted: str | None = None
        self.shown: str | None = None
        self.msg_id = 0
        self._msg_offset = 0
        self._api: _Win32 | None = None
        self._tbl: _TaskbarList3 | None = None
        self._broken = not IS_WINDOWS
        if IS_WINDOWS:
            try:
                self._api = _Win32()
                self._msg_offset = self._api.wt.MSG.message.offset
                self.msg_id = self._api.user32.RegisterWindowMessageW("TaskbarButtonCreated")
                # let the message through even if the app runs as administrator
                self._api.user32.ChangeWindowMessageFilter(self.msg_id, MSGFLT_ADD)
            except Exception:
                log.exception("taskbar badge unavailable")
                self._broken = True

    DESCRIPTIONS = {"recording": "Recording", "armed": "Waiting for sound (Vox)", "paused": "Recording paused"}

    def set(self, kind: str | None) -> None:
        self.wanted = kind
        if kind != self.shown:
            self.apply()

    def on_native_message(self, message: int) -> bool:
        """Called for each window message; True if it was TaskbarButtonCreated."""
        if not self.msg_id or not message:
            return False
        try:
            if ctypes.c_uint.from_address(int(message) + self._msg_offset).value != self.msg_id:
                return False
        except (TypeError, ValueError, OSError):
            return False
        self.shown = None                  # the new button has no badge yet
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, self.apply)   # not from inside the window procedure
        return True

    def apply(self) -> None:
        if self._broken or not self.window.isVisible():
            return                          # no taskbar button now; re-applied when it is created
        want = self.wanted
        try:
            if self._tbl is None:
                self._tbl = _TaskbarList3(self._api)
            hwnd = int(self.window.winId())
            hicon = None
            if want:
                size = max(16, self._api.small_icon_size(hwnd))
                hicon = self._api.hicon_from_image(self.painter(want, size))
            try:
                hr = self._tbl.set_overlay_icon(hwnd, hicon, self.DESCRIPTIONS.get(want, "") if want else "")
            finally:
                if hicon:
                    self._api.user32.DestroyIcon(hicon)   # the taskbar keeps its own copy
            if hr >= 0:
                self.shown = want
            else:
                log.debug("SetOverlayIcon failed: 0x%08X", hr & 0xFFFFFFFF)
        except Exception:
            log.exception("taskbar badge disabled")
            self._broken = True
