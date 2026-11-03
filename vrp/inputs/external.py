"""External device control (Section 12): the modern replacement for the game port.

Sources
  joystick  Windows joystick API (winmm) - USB game controllers, button boxes,
            arcade encoders: the direct successor of the PC game port.
  serial    RS-232 / USB-serial control lines CTS, DSR, DCD, RI (pyserial);
            DTR/RTS can be driven as outputs.
  hid       USB HID devices such as transcription foot pedals (hidapi).

A polling thread reads every source (10 ms; serial 20 ms), debounces inputs,
applies mappings and emits actionEvent(action, pressed) on the GUI thread.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from dataclasses import asdict, dataclass

from PySide6.QtCore import QObject, Signal

log = logging.getLogger(__name__)
IS_WINDOWS = sys.platform == "win32"

TRIGGERS = {"press": "On press", "release": "On release", "hold": "While held (press + release)",
            "level": "Level (active / inactive)"}
SERIAL_INPUTS = ["CTS", "DSR", "DCD", "RI"]
SERIAL_OUTPUTS = ["DTR", "RTS"]
OUTPUT_STATES = {"recording": "Recording", "armed": "Armed (Vox waiting)", "playing": "Playing"}


@dataclass
class DeviceRef:
    kind: str          # joystick | serial | hid
    id: str            # joystick index, COM port, or 'vid:pid'
    name: str

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.id}"


@dataclass
class Mapping:
    device_kind: str = "hid"
    device_id: str = ""
    device_name: str = ""
    input: str = ""
    trigger: str = "press"
    invert: bool = False
    debounce_ms: int = 20
    threshold: int = 50          # axes only, % of travel
    action: str = "play"

    @staticmethod
    def from_json(d: dict) -> "Mapping":
        m = Mapping()
        for k, v in d.items():
            if hasattr(m, k):
                setattr(m, k, v)
        return m

    def to_json(self) -> dict:
        return asdict(self)

    @property
    def device_key(self) -> str:
        return f"{self.device_kind}:{self.device_id}"


BUILTIN_PROFILES = {
    "Transcription pedal - VEC Infinity IN-USB-2/3": {
        "mappings": [
            Mapping("hid", "05f3:00ff", "VEC USB Footpedal", "Bit 0", "hold", False, 20, 50, "rewind").to_json(),
            Mapping("hid", "05f3:00ff", "VEC USB Footpedal", "Bit 1", "hold", False, 20, 50, "play_held").to_json(),
            Mapping("hid", "05f3:00ff", "VEC USB Footpedal", "Bit 2", "hold", False, 20, 50, "ffwd").to_json(),
        ],
        "outputs": [],
    },
    "Radio squelch on COM1 CTS (record while active)": {
        "mappings": [Mapping("serial", "COM1", "COM1", "CTS", "level", False, 50, 50, "record_active").to_json()],
        "outputs": [{"state": "recording", "port": "COM1", "line": "RTS"}],
    },
    "Game controller - buttons 1-3 as Rewind / Play / Fast Forward": {
        "mappings": [
            Mapping("joystick", "0", "Game controller 1", "Button 1", "hold", False, 20, 50, "rewind").to_json(),
            Mapping("joystick", "0", "Game controller 1", "Button 2", "hold", False, 20, 50, "play_held").to_json(),
            Mapping("joystick", "0", "Game controller 1", "Button 3", "hold", False, 20, 50, "ffwd").to_json(),
        ],
        "outputs": [],
    },
}


# ======================================================================= sources
class Source:
    kind = ""

    def __init__(self, ref: DeviceRef):
        self.ref = ref
        self.ok = False
        self.error = ""

    def open(self) -> None:
        raise NotImplementedError

    def read(self) -> dict[str, float]:
        """Current input values: 0/1 for buttons/lines, -1..1 for axes."""
        raise NotImplementedError

    def close(self) -> None:
        pass

    def set_output(self, line: str, on: bool) -> None:
        pass


class JoystickSource(Source):
    kind = "joystick"

    def open(self) -> None:
        import ctypes
        from ctypes import wintypes

        class JOYINFOEX(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("dwFlags", wintypes.DWORD), ("dwXpos", wintypes.DWORD),
                        ("dwYpos", wintypes.DWORD), ("dwZpos", wintypes.DWORD), ("dwRpos", wintypes.DWORD),
                        ("dwUpos", wintypes.DWORD), ("dwVpos", wintypes.DWORD), ("dwButtons", wintypes.DWORD),
                        ("dwButtonNumber", wintypes.DWORD), ("dwPOV", wintypes.DWORD),
                        ("dwReserved1", wintypes.DWORD), ("dwReserved2", wintypes.DWORD)]
        self._ct = ctypes
        self._info = JOYINFOEX()
        self._info.dwSize = ctypes.sizeof(JOYINFOEX)
        self._info.dwFlags = 0xFF          # JOY_RETURNALL
        self._winmm = ctypes.windll.winmm
        self._idx = int(self.ref.id)
        if self._winmm.joyGetPosEx(self._idx, ctypes.byref(self._info)) != 0:
            raise OSError("game controller not connected")
        self.ok = True

    def read(self) -> dict[str, float]:
        if self._winmm.joyGetPosEx(self._idx, self._ct.byref(self._info)) != 0:
            raise OSError("game controller disconnected")
        i = self._info
        vals = {f"Button {b + 1}": float((i.dwButtons >> b) & 1) for b in range(32)}
        for name, raw in (("X", i.dwXpos), ("Y", i.dwYpos), ("Z", i.dwZpos), ("R", i.dwRpos),
                          ("U", i.dwUpos), ("V", i.dwVpos)):
            v = (raw - 32767.5) / 32767.5
            vals[f"{name}+"] = max(0.0, v)
            vals[f"{name}-"] = max(0.0, -v)
        pov = i.dwPOV
        for name, deg in (("POV Up", 0), ("POV Right", 9000), ("POV Down", 18000), ("POV Left", 27000)):
            vals[name] = 1.0 if pov != 0xFFFF and min(abs(pov - deg), 36000 - abs(pov - deg)) <= 4500 else 0.0
        return vals

    @staticmethod
    def enumerate() -> list[DeviceRef]:
        if not IS_WINDOWS:
            return []
        import ctypes
        from ctypes import wintypes

        class JOYCAPSW(ctypes.Structure):
            _fields_ = [("wMid", wintypes.WORD), ("wPid", wintypes.WORD), ("szPname", wintypes.WCHAR * 32),
                        ("wXmin", wintypes.UINT), ("wXmax", wintypes.UINT), ("wYmin", wintypes.UINT),
                        ("wYmax", wintypes.UINT), ("wZmin", wintypes.UINT), ("wZmax", wintypes.UINT),
                        ("wNumButtons", wintypes.UINT), ("wPeriodMin", wintypes.UINT),
                        ("wPeriodMax", wintypes.UINT), ("wRmin", wintypes.UINT), ("wRmax", wintypes.UINT),
                        ("wUmin", wintypes.UINT), ("wUmax", wintypes.UINT), ("wVmin", wintypes.UINT),
                        ("wVmax", wintypes.UINT), ("wCaps", wintypes.UINT), ("wMaxAxes", wintypes.UINT),
                        ("wNumAxes", wintypes.UINT), ("wMaxButtons", wintypes.UINT),
                        ("szRegKey", wintypes.WCHAR * 32), ("szOEMVxD", wintypes.WCHAR * 260)]
        out = []
        try:
            winmm = ctypes.windll.winmm
            n = winmm.joyGetNumDevs()
            probe = JoystickSource(DeviceRef("joystick", "0", ""))
            for idx in range(min(n, 16)):
                caps = JOYCAPSW()
                if winmm.joyGetDevCapsW(idx, ctypes.byref(caps), ctypes.sizeof(caps)) != 0:
                    continue
                probe.ref = DeviceRef("joystick", str(idx), "")
                try:
                    probe.open()
                except OSError:
                    continue
                name = caps.szPname or "Game controller"
                out.append(DeviceRef("joystick", str(idx), f"Game controller {idx + 1} ({name}, "
                                                          f"{caps.wNumButtons} buttons)"))
        except Exception:
            log.debug("joystick enumeration failed", exc_info=True)
        return out


class SerialSource(Source):
    kind = "serial"

    def __init__(self, ref: DeviceRef, drive_dtr: bool = True):
        super().__init__(ref)
        self.drive_dtr = drive_dtr
        self.port = None

    def open(self) -> None:
        import serial
        self.port = serial.Serial()
        self.port.port = self.ref.id
        self.port.baudrate = 9600
        self.port.timeout = 0
        self.port.dsrdtr = False
        self.port.rtscts = False
        self.port.dtr = self.drive_dtr        # DTR as source voltage for contacts (FR-EXT-010)
        self.port.rts = False
        self.port.open()
        self.ok = True

    def read(self) -> dict[str, float]:
        p = self.port
        return {"CTS": float(p.cts), "DSR": float(p.dsr), "DCD": float(p.cd), "RI": float(p.ri)}

    def set_output(self, line: str, on: bool) -> None:
        if self.port is None:
            return
        if line == "DTR":
            self.port.dtr = on
        elif line == "RTS":
            self.port.rts = on

    def close(self) -> None:
        if self.port is not None:
            try:
                self.port.rts = False
                self.port.close()
            except Exception:
                pass
        self.port = None

    @staticmethod
    def enumerate() -> list[DeviceRef]:
        try:
            from serial.tools import list_ports
            return [DeviceRef("serial", p.device, f"{p.device} ({p.description})") for p in list_ports.comports()]
        except Exception:
            return []


class HidSource(Source):
    kind = "hid"

    def __init__(self, ref: DeviceRef):
        super().__init__(ref)
        self.dev = None
        self.state = b""

    def open(self) -> None:
        import hid
        vid, pid = (int(x, 16) for x in self.ref.id.split(":")[:2])
        self.dev = hid.device()
        self.dev.open(vid, pid)
        self.dev.set_nonblocking(1)
        self.state = b""
        self.ok = True

    def read(self) -> dict[str, float]:
        while True:
            data = self.dev.read(64)
            if not data:
                break
            self.state = bytes(data)
        vals = {}
        for bi, byte in enumerate(self.state[:8]):
            for bit in range(8):
                vals[f"Bit {bi * 8 + bit}"] = float((byte >> bit) & 1)
        return vals

    def close(self) -> None:
        if self.dev is not None:
            try:
                self.dev.close()
            except Exception:
                pass
        self.dev = None

    @staticmethod
    def enumerate() -> list[DeviceRef]:
        try:
            import hid
        except Exception:
            return []
        out, seen = [], set()
        try:
            for d in hid.enumerate():
                # skip keyboards and mice (usage page 1, usage 2 = mouse, 6 = keyboard)
                if d.get("usage_page") == 1 and d.get("usage") in (2, 6):
                    continue
                vid, pid = d.get("vendor_id", 0), d.get("product_id", 0)
                key = f"{vid:04x}:{pid:04x}"
                if key in seen:
                    continue
                seen.add(key)
                name = (d.get("product_string") or "").strip() or "HID device"
                maker = (d.get("manufacturer_string") or "").strip()
                out.append(DeviceRef("hid", key, f"{name}{' - ' + maker if maker else ''} [{key}]"))
        except Exception:
            log.debug("hid enumeration failed", exc_info=True)
        return out


SOURCE_TYPES = {"joystick": JoystickSource, "serial": SerialSource, "hid": HidSource}


def enumerate_devices() -> list[DeviceRef]:
    return JoystickSource.enumerate() + SerialSource.enumerate() + HidSource.enumerate()


def available_kinds() -> dict[str, bool]:
    out = {"joystick": IS_WINDOWS}
    import importlib.util
    out["serial"] = importlib.util.find_spec("serial") is not None
    out["hid"] = importlib.util.find_spec("hid") is not None
    return out


# ======================================================================= manager
class ExternalManager(QObject):
    actionEvent = Signal(str, bool)
    deviceStatus = Signal(str, bool, str)      # device key, connected, message
    learned = Signal(str, str, str, str)       # kind, id, name, input

    def __init__(self, parent=None):
        super().__init__(parent)
        self.mappings: list[Mapping] = []
        self.outputs: list[dict] = []
        self.on_loss = "keep"
        self.suspended = False
        self.enabled = True
        self.sources: dict[str, Source] = {}
        self.missing: dict[str, float] = {}          # key -> last retry time
        self.values: dict[str, dict[str, float]] = {}
        self._active: dict[tuple[str, str], bool] = {}     # (dev key, input) -> debounced state
        self._candidate: dict[tuple[str, str], tuple[bool, float]] = {}
        self._watch: set[str] = set()                # devices opened for the test panel/learn
        self._learn_baseline: dict | None = None
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._out_state: dict[tuple[str, str], bool] = {}
        self._app_state = ""

    # --------------------------------------------------------------- config
    def configure(self, mappings: list[dict], outputs: list[dict], on_loss: str, enabled: bool = True) -> None:
        with self._lock:
            self.mappings = [Mapping.from_json(m) for m in mappings]
            self.outputs = [dict(o) for o in outputs]
            self.on_loss = on_loss
            self.enabled = enabled
            needed = self._needed_keys()
            for key in list(self.sources):
                if key not in needed:
                    self._close(key)
            self.missing = {k: 0.0 for k in needed if k not in self.sources}
        self._ensure_thread()

    def _needed_keys(self) -> set[str]:
        keys = set(self._watch)
        if self.enabled:
            keys |= {m.device_key for m in self.mappings if m.device_id}
            keys |= {f"serial:{o['port']}" for o in self.outputs if o.get("port")}
        return keys

    def watch(self, key: str, on: bool) -> None:
        """Open a device for the Test panel / Learn even without mappings."""
        with self._lock:
            if on:
                self._watch.add(key)
                if key not in self.sources:
                    self.missing[key] = 0.0
            else:
                self._watch.discard(key)
                if key not in self._needed_keys():
                    self._close(key)
                    self.missing.pop(key, None)
        self._ensure_thread()

    def start_learn(self) -> None:
        with self._lock:
            self._learn_baseline = {k: dict(v) for k, v in self.values.items()}

    def stop_learn(self) -> None:
        with self._lock:
            self._learn_baseline = None

    def set_suspended(self, on: bool) -> None:
        self.suspended = on

    def set_app_state(self, state: str) -> None:
        """Drive output lines from the engine state (FR-EXT-008)."""
        self._app_state = state
        self._apply_outputs()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._thread = None
        with self._lock:
            for key in list(self.sources):
                self._close(key)

    # ---------------------------------------------------------------- thread
    def _ensure_thread(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="vrp-external", daemon=True)
        self._thread.start()

    def _open(self, key: str) -> None:
        kind, _, ident = key.partition(":")
        cls = SOURCE_TYPES.get(kind)
        if cls is None:
            return
        ref = DeviceRef(kind, ident, ident)
        if kind == "serial":
            drives_dtr = not any(o.get("port") == ident and o.get("line") == "DTR" for o in self.outputs)
            src = SerialSource(ref, drive_dtr=drives_dtr)
        else:
            src = cls(ref)
        src.open()
        self.sources[key] = src
        self.missing.pop(key, None)
        self.deviceStatus.emit(key, True, "Connected")
        log.info("external device opened: %s", key)
        self._apply_outputs()

    def _close(self, key: str) -> None:
        src = self.sources.pop(key, None)
        if src is not None:
            try:
                src.close()
            except Exception:
                pass
        self.values.pop(key, None)

    def _run(self) -> None:
        tick = 0
        while not self._stop.is_set():
            t0 = time.monotonic()
            with self._lock:
                # reopen missing devices every 2 s (FR-EXT-007)
                for key, last in list(self.missing.items()):
                    if t0 - last >= 2.0:
                        self.missing[key] = t0
                        try:
                            self._open(key)
                        except Exception as exc:
                            self.deviceStatus.emit(key, False, f"Missing ({exc})")
                for key, src in list(self.sources.items()):
                    if src.kind == "serial" and tick % 2:
                        continue
                    try:
                        vals = src.read()
                    except Exception as exc:
                        log.warning("external device lost: %s (%s)", key, exc)
                        self._close(key)
                        self.missing[key] = t0
                        self.deviceStatus.emit(key, False, "Disconnected")
                        self._device_lost(key)
                        continue
                    self.values[key] = vals
                    self._evaluate(key, vals, t0)
            tick += 1
            dt = time.monotonic() - t0
            self._stop.wait(max(0.0, 0.01 - dt))
        with self._lock:
            for key in list(self.sources):
                self._close(key)

    def _evaluate(self, key: str, vals: dict, now: float) -> None:
        if self._learn_baseline is not None:
            base = self._learn_baseline.get(key)
            if base is None:
                self._learn_baseline[key] = dict(vals)
            else:
                for name, v in vals.items():
                    if abs(v - base.get(name, 0.0)) >= 0.5:
                        kind, _, ident = key.partition(":")
                        self._learn_baseline = None
                        self.learned.emit(kind, ident, ident, name)
                        break
        if not self.enabled or self.suspended:
            return
        for m in self.mappings:
            if m.device_key != key or m.input not in vals:
                continue
            raw = vals[m.input]
            on = raw >= (m.threshold / 100.0 if m.input[-1:] in "+-" else 0.5)
            if m.invert:
                on = not on
            k = (key, m.input + "|" + m.action + "|" + m.trigger)
            cur = self._active.get(k, False)
            cand = self._candidate.get(k)
            if on == cur:
                self._candidate.pop(k, None)
                continue
            if cand is None or cand[0] != on:
                self._candidate[k] = (on, now)
                cand = self._candidate[k]
            if (now - cand[1]) * 1000 >= m.debounce_ms:
                self._active[k] = on
                self._candidate.pop(k, None)
                self._fire(m, on)

    def _fire(self, m: Mapping, on: bool) -> None:
        if m.trigger == "press" and on:
            self.actionEvent.emit(m.action, True)
        elif m.trigger == "release" and not on:
            self.actionEvent.emit(m.action, True)
        elif m.trigger in ("hold", "level"):
            self.actionEvent.emit(m.action, on)

    def _device_lost(self, key: str) -> None:
        """FR-EXT-006: keep state, or treat the loss as 'input released'."""
        for m in self.mappings:
            if m.device_key != key:
                continue
            k = (key, m.input + "|" + m.action + "|" + m.trigger)
            if self._active.get(k):
                self._active[k] = False
                if self.on_loss == "release" and m.trigger in ("hold", "level"):
                    self.actionEvent.emit(m.action, False)

    def _apply_outputs(self) -> None:
        with self._lock:
            for o in self.outputs:
                port, line = o.get("port"), o.get("line", "RTS")
                on = o.get("state") == self._app_state
                src = self.sources.get(f"serial:{port}")
                if src is None:
                    continue
                if self._out_state.get((port, line)) != on:
                    try:
                        src.set_output(line, on)
                        self._out_state[(port, line)] = on
                    except Exception:
                        log.debug("output %s %s failed", port, line, exc_info=True)

    def test_output(self, port: str, line: str, on: bool) -> bool:
        with self._lock:
            src = self.sources.get(f"serial:{port}")
            if src is None:
                return False
            src.set_output(line, on)
            self._out_state[(port, line)] = on
            return True

    def live_values(self, key: str) -> dict[str, float]:
        with self._lock:
            return dict(self.values.get(key, {}))
