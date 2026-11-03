"""Convert key-combination strings ('Ctrl+Alt+R') to Windows modifier flags and virtual-key codes."""
from __future__ import annotations

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000

_MODS = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT,
         "meta": MOD_WIN, "win": MOD_WIN}

VK: dict[str, int] = {}
for _c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789":
    VK[_c] = ord(_c)
for _i in range(1, 25):
    VK[f"F{_i}"] = 0x6F + _i
for _i in range(10):
    VK[f"Num+{_i}"] = 0x60 + _i
VK.update({
    "Num+*": 0x6A, "Num++": 0x6B, "Num+-": 0x6D, "Num+.": 0x6E, "Num+/": 0x6F, "Num+Enter": 0x0D,
    "Left": 0x25, "Up": 0x26, "Right": 0x27, "Down": 0x28,
    "Ins": 0x2D, "Insert": 0x2D, "Del": 0x2E, "Delete": 0x2E, "Home": 0x24, "End": 0x23,
    "PgUp": 0x21, "PageUp": 0x21, "PgDown": 0x22, "PageDown": 0x22,
    "Space": 0x20, "Return": 0x0D, "Enter": 0x0D, "Tab": 0x09, "Backspace": 0x08, "Esc": 0x1B,
    "Pause": 0x13, "Print": 0x2C, "ScrollLock": 0x91,
    ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF, "`": 0xC0,
    "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
    "Media Play": 0xB3, "Media Pause": 0xB3, "Toggle Media Play/Pause": 0xB3, "Media Play/Pause": 0xB3,
    "Media Stop": 0xB2, "Media Next": 0xB0, "Media Previous": 0xB1,
    "Volume Up": 0xAF, "Volume Down": 0xAE, "Volume Mute": 0xAD,
})

MEDIA_KEYS = {"play": "Media Play/Pause", "stop": "Media Stop", "prev_segment": "Media Previous",
              "next_segment": "Media Next"}


def parse(binding: str) -> tuple[int, int] | None:
    """'Ctrl+Alt+R' -> (MOD_CONTROL|MOD_ALT, 0x52). None if not usable."""
    if not binding:
        return None
    text = binding.strip()
    mods = 0
    # Keys that contain '+' themselves (Num++, Media Play/Pause) need care: peel modifiers first.
    while True:
        head, sep, rest = text.partition("+")
        if sep and head.lower() in _MODS and rest:
            mods |= _MODS[head.lower()]
            text = rest
            continue
        break
    key = text
    if key.startswith("Num+") and key not in VK and key[4:] in VK:
        key = key[4:]
    vk = VK.get(key) or VK.get(key.upper()) or VK.get(key.title())
    if vk is None:
        return None
    return mods, vk


def is_safe_global(binding: str) -> tuple[bool, str]:
    """FR-HKEY-002/006: global bindings need a modifier; warn about AltGr clashes."""
    p = parse(binding)
    if p is None:
        return False, "This key cannot be used."
    mods, vk = p
    is_media_or_f = 0x70 <= vk <= 0x87 or 0xAD <= vk <= 0xB3 or 0x60 <= vk <= 0x6F or vk in (0x13, 0x2C, 0x91)
    if not (mods & (MOD_CONTROL | MOD_ALT | MOD_WIN)) and not is_media_or_f:
        return False, "Global hot-keys need Ctrl, Alt or Win (letters and digits alone would block typing)."
    if (mods & MOD_CONTROL) and (mods & MOD_ALT) and (0x41 <= vk <= 0x5A or 0x30 <= vk <= 0x39 or vk >= 0xBA):
        return True, ("On many European keyboard layouts Ctrl+Alt+key is AltGr and types a character; "
                      "consider Ctrl+Shift or Win instead.")
    return True, ""


def normalise(binding: str) -> str:
    """Canonical text for comparing bindings."""
    p = parse(binding)
    if p is None:
        return binding.strip()
    mods, vk = p
    parts = []
    for name, flag in (("Ctrl", MOD_CONTROL), ("Alt", MOD_ALT), ("Shift", MOD_SHIFT), ("Win", MOD_WIN)):
        if mods & flag:
            parts.append(name)
    return "+".join(parts + [f"VK{vk:02X}"])
