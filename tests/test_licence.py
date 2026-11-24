"""Licence files (v1.0.4): Ed25519 signatures, the file rules, and Help > Licence / About."""
import base64
import json
import os
import unicodedata
from pathlib import Path

import pytest

from conftest import pump
from vrp import ed25519, licence

DATA = Path(__file__).parent / "data"
SEED = bytes(range(32))                                   # test key only; never shipped
PUB = ed25519.public_key(SEED)
KEYS = {"TEST-1": PUB}


def fields(**over):
    f = {"product": "Voice Recorder Pro", "type": "Commercial", "id": "VRP-20261124-001",
         "name": "Chan Tai Man", "email": "tm.chan@example.com", "issued": "2026-11-24"}
    f.update(over)
    return f


def make_file(folder, fname="VoiceRecorderPro-test.key", seed=SEED, key_id="TEST-1", **over):
    p = Path(folder) / fname
    p.write_bytes(licence.make(seed, key_id, fields(**over)))
    return p


# ------------------------------------------------------------------ Ed25519
@pytest.mark.parametrize("v", json.loads((DATA / "rfc8032_ed25519.json").read_text())["vectors"],
                         ids=lambda v: v["name"])
def test_rfc8032_vectors(v):
    """Published answers: RFC 8032 section 7.1, all five Ed25519 test vectors."""
    sk, pk, msg, sig = (bytes.fromhex(v[k]) for k in ("secret", "public", "message", "signature"))
    assert ed25519.public_key(sk) == pk
    assert ed25519.sign(sk, msg) == sig
    assert ed25519.verify(pk, msg, sig)
    assert not ed25519.verify(pk, msg + b"\0", sig)
    bad = bytearray(sig)
    bad[5] ^= 1
    assert not ed25519.verify(pk, msg, bytes(bad))


def test_rejects_malformed_signatures_and_keys():
    sig = ed25519.sign(SEED, b"m")
    s = int.from_bytes(sig[32:], "little")
    high = sig[:32] + (s + ed25519.L).to_bytes(32, "little")       # S >= L must be refused
    assert not ed25519.verify(PUB, b"m", high)
    assert not ed25519.verify(PUB, b"m", sig[:63])
    assert not ed25519.verify(PUB[:31], b"m", sig)
    assert not ed25519.verify(b"\xff" * 32, b"m", sig)             # y >= p: not a point
    with pytest.raises(ValueError):
        ed25519.sign(b"short", b"m")


def test_matches_an_independent_implementation():
    """Cross-check with the 'cryptography' package (OpenSSL), when it is installed (dev only)."""
    crypto = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
    from cryptography.hazmat.primitives import serialization
    for i in range(25):
        seed = os.urandom(32)
        msg = os.urandom(i * 7)
        k = crypto.Ed25519PrivateKey.from_private_bytes(seed)
        pub = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        assert ed25519.public_key(seed) == pub
        assert ed25519.sign(seed, msg) == k.sign(msg)
        assert ed25519.verify(pub, msg, k.sign(msg))


# ------------------------------------------------------------------ licence files
def test_valid_licence_is_registered(tmp_path):
    make_file(tmp_path)
    st = licence.check_folder(tmp_path, KEYS)
    assert st.registered and not st.problems
    assert st.licence.name == "Chan Tai Man" and st.licence.email == "tm.chan@example.com"
    assert st.licence.id == "VRP-20261124-001" and st.licence.type == "Commercial"
    assert st.summary() == "Licensed to Chan Tai Man (tm.chan@example.com)"


def test_no_file_is_unregistered(tmp_path):
    st = licence.check_folder(tmp_path, KEYS)
    assert not st.registered and not st.problems
    assert st.summary() == "Unregistered copy – free for personal use"
    assert not licence.check_folder(tmp_path / "missing", KEYS).registered


@pytest.mark.parametrize("change, reason", [
    (lambda o: o["licence"].update(name="Someone Else"), "signature does not match"),
    (lambda o: o["licence"].update(email="x@example.org"), "signature does not match"),
    (lambda o: o["licence"].update(issued="2027-01-01"), "signature does not match"),
    (lambda o: o["licence"].update(product="Secure Offline Transcriber"), "not Voice Recorder Pro"),
    (lambda o: o.update(key="OTHER-1"), "key this version does not know"),
    (lambda o: o.update(signature="not base64!"), "signature is not valid"),
    (lambda o: o.update(format=2), "format is not supported"),
    (lambda o: o["licence"].pop("email"), "'email' is missing"),
    (lambda o: o["licence"].update(type="Site"), "type is not known"),
    (lambda o: o["licence"].update(id="12345"), "licence number is not valid"),
    (lambda o: o["licence"].update(issued="24/11/2026"), "issue date is not valid"),
    (lambda o: o["licence"].update(extra={"nested": 1}), "unexpected field"),
])
def test_changed_or_damaged_files_are_refused(tmp_path, change, reason):
    p = make_file(tmp_path)
    obj = json.loads(p.read_text(encoding="utf-8"))
    change(obj)
    p.write_text(json.dumps(obj), encoding="utf-8")
    st = licence.check_folder(tmp_path, KEYS)
    assert not st.registered
    assert len(st.problems) == 1 and reason in st.problems[0] and p.name in st.problems[0]


@pytest.mark.parametrize("content", [b"", b"\x00\x01\x02binary", b"[1, 2]", "{\"format\": 1}".encode(),
                                     b"x" * (licence.MAX_FILE_BYTES + 1), "﻿{bad json".encode()])
def test_junk_files_never_crash(tmp_path, content):
    (tmp_path / "junk.key").write_bytes(content)
    st = licence.check_folder(tmp_path, KEYS)
    assert not st.registered and len(st.problems) == 1


def test_unicode_names_and_bom(tmp_path):
    name = "陳大文 Chân"                         # Chinese characters and an accent
    p = make_file(tmp_path, name=unicodedata.normalize("NFD", name) + "  ")
    st = licence.check_folder(tmp_path, KEYS)
    assert st.registered and st.licence.name == unicodedata.normalize("NFC", name)
    p.write_bytes(b"\xef\xbb\xbf" + p.read_bytes())               # saved by Notepad with a BOM
    assert licence.check_folder(tmp_path, KEYS).registered


def test_first_valid_file_counts_and_others_are_reported(tmp_path):
    (tmp_path / "a-damaged.key").write_text("{}", encoding="utf-8")
    make_file(tmp_path, "b.key", name="First Person")
    make_file(tmp_path, "c.key", name="Second Person", id="VRP-20261124-002")
    (tmp_path / "notes.txt").write_text("not a key", encoding="utf-8")
    st = licence.check_folder(tmp_path, KEYS)
    assert st.licence.name == "First Person"
    assert len(st.problems) == 1 and st.problems[0].startswith("a-damaged.key")


def test_signature_from_another_key_is_refused(tmp_path):
    make_file(tmp_path, seed=bytes(32), key_id="TEST-1")           # right key id, wrong secret key
    st = licence.check_folder(tmp_path, KEYS)
    assert not st.registered and "signature does not match" in st.problems[0]


def test_make_validates_input():
    with pytest.raises(licence.LicenceError, match="e-mail"):
        licence.make(SEED, "TEST-1", fields(email="no-at-sign"))
    with pytest.raises(licence.LicenceError, match="name"):
        licence.make(SEED, "TEST-1", fields(name="   "))
    with pytest.raises(licence.LicenceError, match="name"):
        licence.make(SEED, "TEST-1", fields(name="x" * 101))
    with pytest.raises(licence.LicenceError, match="control"):
        licence.make(SEED, "TEST-1", fields(name="Bad\x07Name"))


def test_signed_bytes_are_canonical():
    a = licence.signed_bytes({"b": "2", "a": "陳"})
    assert a == licence.SIGNING_DOMAIN + '{"a":"陳","b":"2"}'.encode("utf-8")
    raw = json.loads(licence.make(SEED, "TEST-1", fields()))
    assert len(base64.b64decode(raw["signature"])) == 64


def test_shipped_public_key_is_well_formed():
    assert set(licence.PUBLIC_KEYS) == {"NTC-2026-1"}
    for key in licence.PUBLIC_KEYS.values():
        assert len(key) == 32 and ed25519._decode(key) is not None


# ------------------------------------------------------------------ Help > Licence and About
def test_about_and_licence_dialog(qapp, tmp_path):
    from vrp.ui.dialogs import AboutDialog, LicenceDialog
    status = licence.check_folder(tmp_path, KEYS)
    about = AboutDialog("libs", None, None, licence_status=status)
    assert "Unregistered copy – free for personal use" in about.licence.text()
    about.deleteLater()
    dlg = LicenceDialog(str(tmp_path), check=lambda: licence.check_folder(tmp_path, KEYS))
    dlg.show()
    pump(qapp, 0.02)
    assert "Unregistered copy" in dlg.state.text() and "https://software.4u.hk" in dlg.details.text()
    make_file(tmp_path, name="Chan <b>Tai</b> Man")              # HTML in a name is shown as text
    dlg.check_btn.click()                                         # Check Again picks up the new file
    assert dlg.status.registered and "Chan &lt;b&gt;Tai&lt;/b&gt; Man" in dlg.state.text()
    assert "VRP-20261124-001" in dlg.details.text()
    about2 = AboutDialog("libs", None, None, licence_status=dlg.status)
    assert "Licensed to Chan &lt;b&gt;Tai&lt;/b&gt; Man" in about2.licence.text()
    assert "Commercial licence VRP-20261124-001" in about2.licence.text()
    dlg.done(0)
    about2.deleteLater()


def test_help_menu_has_licence_item(qapp, tmp_path, monkeypatch):
    import vrp.app as A
    from vrp.settings import Settings
    s = Settings()
    s.update({"rec.folder": str(tmp_path / "rec"), "general.first_run_done": True})
    c = A.AppController(qapp, A._gui_args(["--fake-audio"]))
    try:
        act = c.window.actions_by_key["licence"]
        assert act.text().startswith("&Licence")
        act.trigger()
        pump(qapp, 0.05)
        assert c.window._licence.isVisible()
        assert "free for personal use" in c.window._licence.state.text()   # the source tree has no .key
        c.window._licence.done(0)
    finally:
        c.quit()


def test_about_box_shows_every_line(qapp, tmp_path):
    """Regression (found in 1.0.4, present since 1.0.0): the libraries paragraph was cut off,
    because Qt does not pass word-wrapped label heights up through nested layouts."""
    from vrp.ui.dialogs import AboutDialog
    make_file(tmp_path)
    for st in (licence.check_folder(tmp_path / "none", KEYS), licence.check_folder(tmp_path, KEYS)):
        dlg = AboutDialog("PySide6 6.12.0, numpy 2.5.3, soundfile 0.14.0", None, None, licence_status=st)
        dlg.show()
        pump(qapp, 0.02)
        for label in (dlg.title, dlg.link, dlg.licence, dlg.body, dlg.fine):
            assert label.height() >= label.heightForWidth(label.width()) - 1, label.text()[:40]
        assert dlg.body.geometry().bottom() < dlg.fine.geometry().top()
        dlg.done(0)
