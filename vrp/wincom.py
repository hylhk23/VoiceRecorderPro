"""Minimal COM helpers over ctypes (no comtypes/pywin32 dependency).

Only what the app needs: GUIDs, CoInitializeEx/CoCreateInstance, calling a
method by its vtable index and releasing an interface. Windows only; importing
the module is harmless elsewhere.
"""
from __future__ import annotations

import ctypes
import sys
import uuid

IS_WINDOWS = sys.platform == "win32"
CLSCTX_ALL = 0x17
COINIT_APARTMENTTHREADED = 0x2
VT_RELEASE = 2
S_FALSE = 1


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16), ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_ubyte * 8)]


def make_guid(text: str) -> GUID:
    return GUID.from_buffer_copy(uuid.UUID(text).bytes_le)


_ole32 = None


def ole32():
    """Private ole32 handle with explicit prototypes (64-bit safe)."""
    global _ole32
    if _ole32 is None:
        o = ctypes.WinDLL("ole32")
        o.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        o.CoInitializeEx.restype = ctypes.c_long
        o.CoUninitialize.argtypes = []
        o.CoUninitialize.restype = None
        o.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p, ctypes.c_ulong,
                                       ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
        o.CoCreateInstance.restype = ctypes.c_long
        o.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        o.CoTaskMemFree.restype = None
        _ole32 = o
    return _ole32


def co_initialize() -> bool:
    """Initialise COM on this thread; True when the caller must call co_uninitialize()."""
    hr = ole32().CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    return hr in (0, S_FALSE)


def co_uninitialize() -> None:
    ole32().CoUninitialize()


def create(clsid: str, iid: str) -> ctypes.c_void_p | None:
    ptr = ctypes.c_void_p()
    hr = ole32().CoCreateInstance(ctypes.byref(make_guid(clsid)), None, CLSCTX_ALL,
                                  ctypes.byref(make_guid(iid)), ctypes.byref(ptr))
    return ptr if hr >= 0 and ptr.value else None


def method(obj: ctypes.c_void_p, index: int, *argtypes, restype=ctypes.c_long):
    """The vtable entry `index` of a COM interface pointer, as a callable taking (obj, *args)."""
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtbl[index])


def release(obj: ctypes.c_void_p | None) -> None:
    if obj is not None and obj.value:
        method(obj, VT_RELEASE, restype=ctypes.c_ulong)(obj)
