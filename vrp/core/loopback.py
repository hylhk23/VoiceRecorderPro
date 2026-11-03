"""Computer audio: WASAPI loopback of the default output device (Section 6, v1.0.2).

"Computer audio" is whatever Windows plays through the default speakers or
headphones (calls, videos, music). It is captured with PyAudioWPatch, a
PortAudio build with WASAPI loopback support (the PortAudio inside sounddevice
has none).

Two Windows behaviours shape this module:

* Loopback delivers no data while nothing is playing. The recorder therefore
  treats computer audio as a stream that may pause at any time and pads the gaps
  with silence; this module only forwards what arrives.
* The default output device can change (headset plugged in, user picks other
  speakers). A watcher thread checks the default render endpoint every 2 s and
  re-opens capture on the new device. PortAudio only rescans devices when it is
  re-initialised, so the watcher owns its own PortAudio instance and does every
  open/close itself, on one thread.
"""
from __future__ import annotations

import ctypes
import importlib
import logging
import sys
import threading
import time
from typing import Callable

import numpy as np

from vrp import wincom

log = logging.getLogger(__name__)
IS_WINDOWS = sys.platform == "win32"

# Core Audio (mmdeviceapi.h)
CLSID_MM_DEVICE_ENUMERATOR = "BCDE0395-E52F-467C-8E3D-C4579291692E"
IID_IMM_DEVICE_ENUMERATOR = "A95664D2-9614-4F35-A746-DE8DB63617E6"
VT_GET_DEFAULT_AUDIO_ENDPOINT = 4     # IMMDeviceEnumerator
VT_GET_ID = 5                         # IMMDevice
E_RENDER, E_MULTIMEDIA = 0, 1

BlockCallback = Callable[[np.ndarray, int, int], None]     # (frames x channels float32, rate, channels)


class LoopbackError(Exception):
    """Computer audio could not be captured."""


def loopback_support() -> tuple[bool, str]:
    """(available, reason) for recording computer audio on this PC."""
    if not IS_WINDOWS:
        return False, "Recording computer audio needs Windows."
    try:
        importlib.import_module("pyaudiowpatch")
    except Exception as exc:
        return False, f"The PyAudioWPatch package is missing ({exc}). Reinstall Voice Recorder Pro."
    return True, ""


def default_render_id() -> str | None:
    """Endpoint ID of the default output device, or None (no device / not Windows / COM error).

    The calling thread must have initialised COM.
    """
    if not IS_WINDOWS:
        return None
    enum = dev = None
    try:
        enum = wincom.create(CLSID_MM_DEVICE_ENUMERATOR, IID_IMM_DEVICE_ENUMERATOR)
        if enum is None:
            return None
        dev = ctypes.c_void_p()
        get_default = wincom.method(enum, VT_GET_DEFAULT_AUDIO_ENDPOINT, ctypes.c_int, ctypes.c_int,
                                    ctypes.POINTER(ctypes.c_void_p))
        if get_default(enum, E_RENDER, E_MULTIMEDIA, ctypes.byref(dev)) < 0 or not dev.value:
            return None                       # E_NOTFOUND: no output device at all
        text = ctypes.c_void_p()
        if wincom.method(dev, VT_GET_ID, ctypes.POINTER(ctypes.c_void_p))(dev, ctypes.byref(text)) < 0:
            return None
        try:
            return ctypes.wstring_at(text.value) if text.value else None
        finally:
            wincom.ole32().CoTaskMemFree(text)
    except Exception:
        log.debug("default output lookup failed", exc_info=True)
        return None
    finally:
        wincom.release(dev)
        wincom.release(enum)


class WasapiLoopback:
    """Captures the default output device and follows default-device changes."""

    CHECK_S = 2.0

    def __init__(self, on_block: BlockCallback, on_info: Callable[[str], None] | None = None):
        self.on_block = on_block
        self.on_info = on_info
        self.name = ""
        self.rate = 0
        self.channels = 0
        self.last_data = 0.0
        self._pa = None
        self._stream = None
        self._stop = threading.Event()
        self._started = threading.Event()
        self._start_error: Exception | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------- public
    def start(self, timeout: float = 8.0) -> "WasapiLoopback":
        """Open capture (on the watcher thread); raises LoopbackError when it cannot start."""
        self._thread = threading.Thread(target=self._run, name="vrp-loopback", daemon=True)
        self._thread.start()
        if not self._started.wait(timeout):
            self._stop.set()
            raise LoopbackError("Windows did not answer when opening the speakers for recording.")
        if self._start_error is not None:
            err = self._start_error
            raise err if isinstance(err, LoopbackError) else LoopbackError(
                f"Computer audio could not be recorded: {err}")
        return self

    def close(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive() and threading.current_thread() is not t:
            t.join(timeout=5)
        self._thread = None

    stop = close

    # ------------------------------------------------------------- thread
    def _run(self) -> None:
        com = False
        try:
            com = wincom.co_initialize()
        except Exception:
            log.debug("CoInitializeEx failed", exc_info=True)
        try:
            try:
                self._open()
            except Exception as exc:
                self._start_error = exc
                return
            finally:
                self._started.set()
            last_id = default_render_id()
            failing = False
            while not self._stop.wait(self.CHECK_S):
                cur = default_render_id()
                reason = None
                if self._stream is None:
                    reason = "reopen"
                elif cur is not None and last_id is not None and cur != last_id:
                    reason = "the default output device changed"
                elif not self._is_active():
                    reason = "the output device stopped"
                if cur is not None:
                    last_id = cur
                if reason is None:
                    continue
                if reason != "reopen" or not failing:
                    log.info("computer audio: %s; re-opening capture", reason)
                self._close_stream()
                try:
                    self._open()
                    failing = False
                    log.info("computer audio now from %s (%d Hz x%d)", self.name, self.rate, self.channels)
                    if self.on_info is not None:
                        self.on_info(f"Recording computer audio from {self.name}.")
                except Exception as exc:
                    if not failing:
                        log.warning("computer audio unavailable: %s (retrying every %.0f s)", exc, self.CHECK_S)
                    failing = True
        finally:
            self._close_stream()
            if com:
                try:
                    wincom.co_uninitialize()
                except Exception:
                    pass

    def _is_active(self) -> bool:
        try:
            return bool(self._stream is not None and self._stream.is_active())
        except Exception:
            return False

    def _open(self) -> None:
        import pyaudiowpatch as pyaudio
        pa = pyaudio.PyAudio()          # a fresh PortAudio instance sees the current devices
        try:
            dev = None
            try:
                dev = pa.get_default_wasapi_loopback()
            except Exception:
                dev = None
            if not dev:
                try:
                    speakers = pa.get_default_wasapi_device(d_out=True)
                    for lb in pa.get_loopback_device_info_generator():
                        if speakers["name"] in lb["name"]:
                            dev = lb
                            break
                except Exception:
                    dev = None
            if not dev:
                raise LoopbackError("No speakers or headphones were found to record computer audio from.")
            ch = max(1, int(dev.get("maxInputChannels") or 2))
            rate = int(dev.get("defaultSampleRate") or 48000)
            on_block = self.on_block
            cont = pyaudio.paContinue

            def callback(in_data, frame_count, time_info, status):
                try:
                    if in_data:
                        on_block(np.frombuffer(in_data, dtype=np.float32).reshape(-1, ch), rate, ch)
                        self.last_data = time.monotonic()
                except Exception:
                    log.debug("loopback callback failed", exc_info=True)
                return (None, cont)

            stream = pa.open(format=pyaudio.paFloat32, channels=ch, rate=rate, input=True,
                             input_device_index=int(dev["index"]), frames_per_buffer=max(256, rate // 50),
                             stream_callback=callback, start=False)
            stream.start_stream()
        except Exception:
            try:
                pa.terminate()
            except Exception:
                pass
            raise
        self._pa, self._stream = pa, stream
        self.name = str(dev["name"]).replace(" [Loopback]", "")
        self.rate, self.channels = rate, ch
        self.last_data = time.monotonic()

    def _close_stream(self) -> None:
        s, pa = self._stream, self._pa
        self._stream = self._pa = None
        try:
            if s is not None:
                s.stop_stream()
                s.close()
        except Exception:
            log.debug("loopback close failed", exc_info=True)
        try:
            if pa is not None:
                pa.terminate()
        except Exception:
            log.debug("PortAudio terminate failed", exc_info=True)


# =============================================================================
class FakeLoopback:
    """Synthetic computer audio for tests and --fake-audio.

    source(pos, n, rate, ch) returns a block, or None for "nothing playing"
    (no callback at all, exactly like WASAPI loopback in silence).
    """

    def __init__(self, on_block: BlockCallback, source: Callable, rate: int = 48000, channels: int = 2,
                 realtime: float = 1.0, block: int = 480):
        self.on_block = on_block
        self.source = source
        self.rate, self.channels = rate, channels
        self.realtime = realtime
        self.block = block
        self.name = "Fake Speakers"
        self.pos = 0
        self.delivered = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="fake-loopback", daemon=True)

    def start(self) -> "FakeLoopback":
        self._thread.start()
        return self

    def _run(self) -> None:
        t0 = time.monotonic()
        while not self._stop.is_set():
            data = self.source(self.pos, self.block, self.rate, self.channels)
            if data is not None:
                self.on_block(np.ascontiguousarray(data, dtype=np.float32), self.rate, self.channels)
                self.delivered += self.block
            self.pos += self.block
            due = t0 + self.pos / self.rate / max(1e-6, self.realtime)
            delay = due - time.monotonic()
            if delay > 0:
                time.sleep(delay)

    def close(self) -> None:
        self._stop.set()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=2)

    stop = close


def music_bursts(level: float = 0.25, on_s: float = 2.0, off_s: float = 2.0, freq: float = 660.0):
    """Default fake computer audio: tone with a stereo difference, then nothing playing."""
    def src(pos, n, rate, ch):
        t = (pos + np.arange(n)) / rate
        if (t[0] % (on_s + off_s)) >= on_s:
            return None
        left = level * np.sin(2 * np.pi * freq * t)
        right = level * np.sin(2 * np.pi * freq * 1.5 * t)
        x = np.stack([left, right], axis=1) if ch >= 2 else left[:, None]
        if ch > 2:
            x = np.concatenate([x, np.zeros((n, ch - 2))], axis=1)
        return x.astype(np.float32)
    return src
