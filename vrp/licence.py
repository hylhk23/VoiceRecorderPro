"""Licence files (v1.0.4).

Voice Recorder Pro is free for personal use. A commercial licence is a small text file
(UTF-8 JSON, extension .key) that the buyer copies into the program's folder. It holds
the licensee's name and e-mail address and is signed by Neo Time Consulting with Ed25519:

    {
      "format": 1,
      "licence": {"product": "Voice Recorder Pro", "type": "Commercial",
                  "id": "VRP-20261124-001", "name": "...", "email": "...",
                  "issued": "2026-11-24"},
      "key": "NTC-2026-1",
      "signature": "<base64 of 64 bytes>"
    }

The signature covers SIGNING_DOMAIN followed by the "licence" object as canonical JSON
(keys sorted, no spaces, UTF-8). Only the public key is in this program, so a licence
cannot be made or changed without the private key, which stays with the publisher.
Every field is checked on its own; the file's content is never executed.

This module is pure apart from check_folder(), which reads files. It does not import the
rest of Voice Recorder Pro, so the Licence Generator uses an identical copy.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from . import ed25519

PRODUCT = "Voice Recorder Pro"
ID_PREFIX = "VRP"
SIGNING_DOMAIN = b"Neo Time Consulting licence v1\n"
FORMAT = 1
TYPES = ("Commercial",)
MAX_FILE_BYTES = 16 * 1024
FILE_SUFFIX = ".key"

# Public keys of Neo Time Consulting, by key id. A new key is added here; an old one is
# only removed if it must be withdrawn (its licences then stop being accepted).
PUBLIC_KEYS: dict[str, bytes] = {
    "NTC-2026-1": bytes.fromhex("a830884341048b5a935b8f55355c3d9b14b008d1f92a5edfe9c73ee848c37775"),
}

UNREGISTERED = "Unregistered copy – free for personal use"

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_ID_RE = re.compile(r"^[A-Z]{2,5}-\d{8}-\d{3,6}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class LicenceError(ValueError):
    """The licence file is not valid; the message says why, in plain words."""


@dataclass(frozen=True)
class Licence:
    product: str
    type: str
    id: str
    name: str
    email: str
    issued: str
    key_id: str
    file: str = ""


@dataclass
class LicenceStatus:
    licence: Licence | None = None
    problems: list[str] = field(default_factory=list)     # files found but not accepted, with reasons
    folder: str = ""

    @property
    def registered(self) -> bool:
        return self.licence is not None

    def summary(self) -> str:
        if self.licence is None:
            return UNREGISTERED
        return f"Licensed to {self.licence.name} ({self.licence.email})"


# ------------------------------------------------------------------ field rules
def clean_name(name: str) -> str:
    """The form a licensee name is stored in: NFC, single spaces, no control characters."""
    return " ".join(unicodedata.normalize("NFC", str(name)).split())


def check_name(name: str) -> str | None:
    if not isinstance(name, str):
        return "the name is not text"
    if _CONTROL.search(name):
        return "the name contains control characters"
    if name != clean_name(name):
        return "the name is not in its stored form"
    if not 1 <= len(name) <= 100:
        return "the name must be 1 to 100 characters"
    return None


def check_email(email: str) -> str | None:
    if not isinstance(email, str):
        return "the e-mail address is not text"
    if not 3 <= len(email) <= 254 or not _EMAIL_RE.match(email) or _CONTROL.search(email):
        return "the e-mail address is not valid"
    return None


def check_fields(lic: dict, product: str = PRODUCT) -> str | None:
    """None when the licence object is well formed, else the first problem."""
    if not isinstance(lic, dict):
        return "the licence details are missing"
    if len(lic) > 20:
        return "the licence has too many fields"
    for key in ("product", "type", "id", "name", "email", "issued"):
        if not isinstance(lic.get(key), str):
            return f"the field '{key}' is missing"
    for key, value in lic.items():
        if not isinstance(key, str) or not isinstance(value, (str, int, bool)) or \
                (isinstance(value, str) and len(value) > 500):
            return "the licence contains an unexpected field"
    if lic["product"] != product:
        return f"it is a licence for {lic['product'][:60]}, not {product}"
    if lic["type"] not in TYPES:
        return "the licence type is not known"
    if not _ID_RE.match(lic["id"]):
        return "the licence number is not valid"
    err = check_name(lic["name"]) or check_email(lic["email"])
    if err:
        return err
    try:
        date.fromisoformat(lic["issued"])
    except ValueError:
        return "the issue date is not valid"
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", lic["issued"]):
        return "the issue date is not valid"
    return None


def signed_bytes(lic: dict) -> bytes:
    """What the signature covers."""
    return SIGNING_DOMAIN + json.dumps(lic, ensure_ascii=False, sort_keys=True,
                                       separators=(",", ":")).encode("utf-8")


# ------------------------------------------------------------------ parse / make
def parse(data: bytes, keys: dict[str, bytes] | None = None, product: str = PRODUCT,
          file: str = "") -> Licence:
    """Check one licence file's bytes; raises LicenceError with a reason."""
    keys = PUBLIC_KEYS if keys is None else keys
    if len(data) > MAX_FILE_BYTES:
        raise LicenceError("the file is too large to be a licence")
    try:
        text = data.decode("utf-8-sig")
        obj = json.loads(text)
    except (UnicodeDecodeError, ValueError):
        raise LicenceError("the file is not a licence file (it is not readable licence text)") from None
    if not isinstance(obj, dict):
        raise LicenceError("the file is not a licence file")
    if obj.get("format") != FORMAT:
        raise LicenceError("the licence file format is not supported by this version")
    lic = obj.get("licence")
    err = check_fields(lic, product)
    if err:
        raise LicenceError(err)
    key_id, sig_text = obj.get("key"), obj.get("signature")
    if not isinstance(key_id, str) or key_id not in keys:
        raise LicenceError("it is signed with a key this version does not know")
    if not isinstance(sig_text, str) or len(sig_text) > 200:
        raise LicenceError("the signature is missing")
    try:
        sig = base64.b64decode(sig_text, validate=True)
    except (binascii.Error, ValueError):
        raise LicenceError("the signature is not valid") from None
    if not ed25519.verify(keys[key_id], signed_bytes(lic), sig):
        raise LicenceError("the signature does not match: the file was changed, or it is not from "
                           "Neo Time Consulting")
    return Licence(lic["product"], lic["type"], lic["id"], lic["name"], lic["email"], lic["issued"],
                   key_id, file)


def make(seed: bytes, key_id: str, licence_fields: dict) -> bytes:
    """A signed licence file (used by the Licence Generator, never by Voice Recorder Pro)."""
    lic = dict(licence_fields)
    lic["name"] = clean_name(lic.get("name", ""))
    lic["email"] = str(lic.get("email", "")).strip()
    err = check_fields(lic, lic.get("product", ""))
    if err:
        raise LicenceError(err)
    sig = ed25519.sign(seed, signed_bytes(lic))
    obj = {"format": FORMAT, "licence": lic, "key": key_id,
           "signature": base64.b64encode(sig).decode("ascii")}
    return (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


# ------------------------------------------------------------------ the program folder
def check_folder(folder: str | Path, keys: dict[str, bytes] | None = None,
                 product: str = PRODUCT) -> LicenceStatus:
    """Look for licence files (*.key) in `folder`; the first valid one, by name, counts."""
    status = LicenceStatus(folder=str(folder))
    try:
        candidates = sorted(p for p in Path(folder).iterdir()
                            if p.suffix.lower() == FILE_SUFFIX and p.is_file())
    except OSError:
        return status
    for p in candidates:
        try:
            if p.stat().st_size > MAX_FILE_BYTES:
                raise LicenceError("the file is too large to be a licence")
            lic = parse(p.read_bytes(), keys, product, str(p))
        except LicenceError as exc:
            status.problems.append(f"{p.name}: {exc}")
            continue
        except OSError as exc:
            status.problems.append(f"{p.name}: the file could not be read ({exc.strerror or exc})")
            continue
        if status.licence is None:
            status.licence = lic
    return status
