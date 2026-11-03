"""Audio device access (Section 13).

SoundDeviceBackend wraps PortAudio through the sounddevice package. Devices are
stored by host API name + device name, never by index (FR-DEV-003).
FakeBackend produces synthetic input and swallows output so the engine runs
without sound hardware (NFR-017).
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

import numpy as np

log = logging.getLogger(__name__)


@dataclass
class DeviceInfo:
    index: int
    name: str
    host_api: str
    max_in: int
    max_out: int
    default_rate: float
    is_default_in: bool = False
    is_default_out: bool = False

    @property
    def key(self) -> str:
        return self.name


class AudioError(Exception):
    """Raised when a device cannot be opened. `privacy` hints at blocked microphone access."""

    def __init__(self, msg: str, privacy: bool = False):
        super().__init__(msg)
        self.privacy = privacy


class BaseBackend:
    name = "base"

    def host_apis(self) -> list[str]:
        raise NotImplementedError

    def default_host_api(self) -> str:
        raise NotImplementedError

    def devices(self, kind: str, host_api: str | None = None) -> list[DeviceInfo]:
        raise NotImplementedError

    def find(self, kind: str, host_api: str, name: str) -> DeviceInfo | None:
        """Resolve a stored device; empty name means the host API's default."""
        api = host_api or self.default_host_api()
        devs = self.devices(kind, api)
        if not devs:
            return None
        if not name:
            for d in devs:
                if (kind == "input" and d.is_default_in) or (kind == "output" and d.is_default_out):
                    return d
            return devs[0]
        for d in devs:
            if d.name == name:
                return d
        low = name.lower()
        for d in devs:
            if d.name.lower().startswith(low) or low.startswith(d.name.lower()):
                return d
        return None

    def check_input(self, dev: DeviceInfo, rate: int, channels: int, exclusive: bool = False) -> bool:
        raise NotImplementedError

    def check_output(self, dev: DeviceInfo, rate: int, channels: int) -> bool:
        raise NotImplementedError

    def open_input(self, dev: DeviceInfo, rate: int, channels: int, blocksize: int, latency: str,
                   callback: Callable, exclusive: bool = False):
        raise NotImplementedError

    def open_output(self, dev: DeviceInfo, rate: int, channels: int, blocksize: int, latency: str,
                    callback: Callable):
        raise NotImplementedError

    def rescan(self) -> None:
        pass

    def play_tone(self, dev: DeviceInfo | None, freq: float, ms: int, volume: float) -> None:
        pass

    # computer audio (WASAPI loopback, v1.0.2)
    def loopback_support(self) -> tuple[bool, str]:
        return False, "Recording computer audio is not supported here."

    def open_loopback(self, on_block, on_info=None):
        """Start capturing computer audio; on_block(frames x channels float32, rate, channels)."""
        from vrp.core.loopback import LoopbackError
        raise LoopbackError(self.loopback_support()[1])

    def clock_factor(self) -> float:
        """How fast the synthetic devices run compared with real time (1 for real hardware)."""
        return 1.0


# =============================================================================
class SoundDeviceBackend(BaseBackend):
    name = "portaudio"

    def __init__(self):
        import sounddevice as sd   # raises OSError when PortAudio is missing
        self.sd = sd
        self._lock = threading.Lock()

    def host_apis(self) -> list[str]:
        return [h["name"] for h in self.sd.query_hostapis()]

    def default_host_api(self) -> str:
        apis = self.sd.query_hostapis()
        try:
            idx = self.sd.default.hostapi
        except Exception:
            idx = 0
        if isinstance(idx, int) and 0 <= idx < len(apis):
            return apis[idx]["name"]
        for h in apis:
            if h["name"] == "MME":
                return "MME"
        return apis[0]["name"] if apis else ""

    def devices(self, kind: str, host_api: str | None = None) -> list[DeviceInfo]:
        apis = self.sd.query_hostapis()
        out = []
        for i, d in enumerate(self.sd.query_devices()):
            api = apis[d["hostapi"]]
            if host_api and api["name"] != host_api:
                continue
            if kind == "input" and d["max_input_channels"] < 1:
                continue
            if kind == "output" and d["max_output_channels"] < 1:
                continue
            out.append(DeviceInfo(i, d["name"], api["name"], d["max_input_channels"],
                                  d["max_output_channels"], d["default_samplerate"],
                                  i == api.get("default_input_device"),
                                  i == api.get("default_output_device")))
        return out

    def _extra(self, dev: DeviceInfo, exclusive: bool):
        if "WASAPI" in dev.host_api:
            try:
                return self.sd.WasapiSettings(exclusive=exclusive, auto_convert=not exclusive)
            except TypeError:
                return self.sd.WasapiSettings(exclusive=exclusive)
        return None

    def check_input(self, dev, rate, channels, exclusive=False) -> bool:
        try:
            self.sd.check_input_settings(device=dev.index, channels=channels, dtype="float32",
                                         samplerate=rate, extra_settings=self._extra(dev, exclusive))
            return True
        except Exception:
            return False

    def check_output(self, dev, rate, channels) -> bool:
        try:
            self.sd.check_output_settings(device=dev.index, channels=channels, dtype="float32",
                                          samplerate=rate, extra_settings=self._extra(dev, False))
            return True
        except Exception:
            return False

    @staticmethod
    def _privacy_hint(exc: Exception) -> bool:
        text = str(exc)
        return any(s in text for s in ("-9999", "Unanticipated host error", "-9996", "access", "Access"))

    def open_input(self, dev, rate, channels, blocksize, latency, callback, exclusive=False):
        try:
            s = self.sd.InputStream(samplerate=rate, blocksize=blocksize, device=dev.index, channels=channels,
                                    dtype="float32", latency=latency, callback=callback,
                                    extra_settings=self._extra(dev, exclusive))
            s.start()
            return s
        except Exception as exc:
            if exclusive:
                log.warning("exclusive mode refused (%s); falling back to shared mode", exc)
                return self.open_input(dev, rate, channels, blocksize, latency, callback, False)
            raise AudioError(f"Could not open {dev.name}: {exc}", privacy=self._privacy_hint(exc)) from exc

    def open_output(self, dev, rate, channels, blocksize, latency, callback):
        try:
            s = self.sd.OutputStream(samplerate=rate, blocksize=blocksize, device=dev.index, channels=channels,
                                     dtype="float32", latency=latency, callback=callback,
                                     extra_settings=self._extra(dev, False))
            s.start()
            return s
        except Exception as exc:
            raise AudioError(f"Could not open {dev.name}: {exc}") from exc

    def rescan(self) -> None:
        """Re-read the device list. Closes all PortAudio streams (FR-DEV-005)."""
        with self._lock:
            try:
                self.sd._terminate()
            except Exception:
                pass
            self.sd._initialize()

    def play_tone(self, dev, freq, ms, volume) -> None:
        try:
            rate = 44100
            t = np.arange(int(rate * ms / 1000)) / rate
            env = np.minimum(1, np.minimum(t, t[::-1]) / 0.005)
            tone = (volume * env * np.sin(2 * np.pi * freq * t)).astype(np.float32)
            self.sd.play(tone, rate, device=dev.index if dev else None, blocking=False)
        except Exception:
            log.debug("tone failed", exc_info=True)

    def loopback_support(self) -> tuple[bool, str]:
        from vrp.core import loopback
        return loopback.loopback_support()

    def open_loopback(self, on_block, on_info=None):
        from vrp.core import loopback
        ok, why = loopback.loopback_support()
        if not ok:
            raise loopback.LoopbackError(why)
        return loopback.WasapiLoopback(on_block, on_info).start()


# =============================================================================
class _FakeStatus:
    input_overflow = False
    output_underflow = False

    def __bool__(self):
        return False


class FakeStream:
    def __init__(self, kind: str, rate: int, channels: int, blocksize: int, callback: Callable,
                 source: Callable | None, realtime: float, sink: list | None):
        self.kind, self.samplerate, self.channels = kind, rate, channels
        self.blocksize = blocksize or 1024
        self.callback = callback
        self.source = source
        self.realtime = realtime
        self.sink = sink
        self.active = False
        self.pos = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"fake-{kind}", daemon=True)

    def start(self):
        self.active = True
        self._thread.start()

    def _run(self):
        t0 = time.monotonic()
        n = self.blocksize
        while not self._stop.is_set():
            if self.kind == "input":
                if self.source is None:
                    block = np.zeros((n, self.channels), dtype=np.float32)
                else:
                    block = self.source(self.pos, n, self.samplerate, self.channels)
                    if block is None:        # source finished: behave like a lost device
                        break
                self.callback(block, n, None, _FakeStatus())
            else:
                out = np.zeros((n, self.channels), dtype=np.float32)
                try:
                    self.callback(out, n, None, _FakeStatus())
                except Exception:
                    break
                if self.sink is not None:
                    self.sink.append(out.copy())
            self.pos += n
            if self.realtime > 0:
                due = t0 + self.pos / self.samplerate / self.realtime
                delay = due - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
            else:
                time.sleep(0)
        self.active = False

    def stop(self):
        self._stop.set()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=2)
        self.active = False

    def close(self):
        self.stop()


class FakeBackend(BaseBackend):
    """Synthetic devices for tests and demo mode (--fake-audio)."""

    name = "fake"

    def __init__(self, source: Callable | None = None, realtime: float = 1.0):
        self.source = source or tone_bursts()
        self.realtime = realtime
        self.output_sink: list | None = None
        self.opened: list = []
        self.fail_input = False
        from vrp.core.loopback import music_bursts
        self.loopback_source = music_bursts()
        self.fail_loopback = False
        self.loopbacks: list = []

    def host_apis(self):
        return ["Fake API"]

    def default_host_api(self):
        return "Fake API"

    def devices(self, kind, host_api=None):
        if kind == "input":
            return [DeviceInfo(0, "Fake Microphone", "Fake API", 2, 0, 48000.0, True, False)]
        return [DeviceInfo(1, "Fake Speakers", "Fake API", 0, 2, 48000.0, False, True)]

    def check_input(self, dev, rate, channels, exclusive=False):
        return True

    def check_output(self, dev, rate, channels):
        return True

    def open_input(self, dev, rate, channels, blocksize, latency, callback, exclusive=False):
        if self.fail_input:
            raise AudioError("Fake input failure")
        s = FakeStream("input", rate, channels, blocksize, callback, self.source, self.realtime, None)
        s.start()
        self.opened.append(s)
        return s

    def open_output(self, dev, rate, channels, blocksize, latency, callback):
        s = FakeStream("output", rate, channels, blocksize, callback, None, self.realtime, self.output_sink)
        s.start()
        self.opened.append(s)
        return s

    def loopback_support(self) -> tuple[bool, str]:
        return True, ""

    def open_loopback(self, on_block, on_info=None):
        from vrp.core.loopback import FakeLoopback, LoopbackError
        if self.fail_loopback:
            raise LoopbackError("Fake loopback failure")
        lb = FakeLoopback(on_block, self.loopback_source, realtime=self.realtime).start()
        self.loopbacks.append(lb)
        return lb

    def clock_factor(self) -> float:
        return self.realtime


def tone_bursts(level: float = 0.3, on_s: float = 1.5, off_s: float = 3.0, freq: float = 300.0):
    """Default fake microphone: speech-like tone bursts separated by quiet noise."""
    rng = np.random.default_rng(1)

    def src(pos, n, rate, ch):
        t = (pos + np.arange(n)) / rate
        period = on_s + off_s
        on = (t % period) < on_s
        sig = level * np.sin(2 * np.pi * freq * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 3 * t))
        noise = rng.normal(0, 0.002, n)
        x = np.where(on, sig, 0.0) + noise
        return np.repeat(x[:, None], ch, axis=1).astype(np.float32)
    return src


def scripted_source(segments: list[tuple[float, float, float]], total_s: float | None = None,
                    freq: float = 440.0):
    """Input defined by (start_s, end_s, amplitude) tone spans; None after total_s (device lost)."""
    def src(pos, n, rate, ch):
        if total_s is not None and pos / rate >= total_s:
            return None
        t = (pos + np.arange(n)) / rate
        x = np.zeros(n)
        for a, b, amp in segments:
            m = (t >= a) & (t < b)
            x[m] = amp * np.sin(2 * np.pi * freq * t[m])
        return np.repeat(x[:, None], ch, axis=1).astype(np.float32)
    return src


def make_backend(fake: bool = False) -> BaseBackend:
    if fake:
        return FakeBackend()
    return SoundDeviceBackend()
