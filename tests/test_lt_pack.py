"""Live Transcript pack: pack.json checked field by field, sizes, full checksum check (1.1.0)."""
import hashlib
import json

import pytest

from vrp.transcribe import pack as P

FILES = {"models/sensevoice-small-int8/model.int8.onnx": b"model" * 10,
         "models/sensevoice-small-int8/tokens.txt": b"tokens",
         "models/silero-vad/silero_vad.onnx": b"vad" * 3}


def make_pack(folder, data_files=FILES, **override):
    entries = []
    for rel, data in data_files.items():
        p = folder / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        entries.append({"path": rel, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    obj = {"format": 1, "for_app": "1.1", "version": "1.1.0", "model_name": "SenseVoiceSmall int8",
           "files": entries}
    obj.update(override)
    (folder / "pack.json").write_text(json.dumps(obj), encoding="utf-8")
    return folder


@pytest.fixture
def engine_present(monkeypatch):
    monkeypatch.setattr(P, "_engine_missing", lambda: "")


def test_not_installed(tmp_path):
    st = P.status(tmp_path)
    assert not st.ok and "not installed" in st.reason


def test_good_pack(tmp_path, engine_present):
    st = P.status(make_pack(tmp_path))
    assert st.ok and st.version == "1.1.0" and st.model.endswith("model.int8.onnx")
    assert P.verify(st) == []


def test_pack_dir_follows_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("VRP_LT_PACK", str(tmp_path))
    assert P.pack_dir() == tmp_path


@pytest.mark.parametrize("override,reason", [
    ({"format": 2}, "format"), ({"for_app": "1.0"}, "for Voice Recorder Pro 1.0"),
    ({"version": None}, "version"), ({"files": []}, "file list"),
    ({"files": [{"path": "../evil.dll", "size": 1, "sha256": "0" * 64}]}, "not valid"),
    ({"files": [{"path": "/abs", "size": 1, "sha256": "0" * 64}]}, "not valid"),
    ({"files": [{"path": "a", "size": "1", "sha256": "0" * 64}]}, "not valid"),
    ({"files": [{"path": "a", "size": 1, "sha256": "short"}]}, "not valid"),
    ({"files": ["a"]}, "not an object"),
])
def test_damaged_pack_json(tmp_path, engine_present, override, reason):
    st = P.status(make_pack(tmp_path, **override))
    assert not st.ok and "damaged" in st.reason and reason in st.reason


def test_not_json_and_too_large(tmp_path, engine_present):
    (tmp_path / "pack.json").write_text("{", encoding="utf-8")
    assert "damaged" in P.status(tmp_path).reason
    (tmp_path / "pack.json").write_text(" " * (P.MAX_PACK_JSON + 1), encoding="utf-8")
    assert "too large" in P.status(tmp_path).reason


def test_wrong_size_missing_file_and_bad_checksum(tmp_path, engine_present):
    make_pack(tmp_path)
    tok = tmp_path / "models/sensevoice-small-int8/tokens.txt"
    tok.write_bytes(b"tokenz")                                   # same size, other content
    st = P.status(tmp_path)
    assert st.ok and P.verify(st) == ["models/sensevoice-small-int8/tokens.txt"]
    tok.write_bytes(b"x")
    assert "wrong size" in P.status(tmp_path).reason
    tok.unlink()
    assert "missing" in P.status(tmp_path).reason


def test_required_model_file_not_listed(tmp_path, engine_present):
    files = dict(FILES)
    files.pop("models/silero-vad/silero_vad.onnx")
    st = P.status(make_pack(tmp_path, files))
    assert not st.ok and "silero_vad.onnx is missing" in st.reason


def test_engine_libraries_missing(tmp_path, monkeypatch):
    make_pack(tmp_path)
    monkeypatch.setattr(P.importlib.util, "find_spec", lambda name: None)
    st = P.status(tmp_path)
    assert not st.ok and "sherpa-onnx" in st.reason
