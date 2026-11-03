import json
from datetime import datetime

from vrp.core import naming
from vrp.settings import Settings, validate_value


def test_default_pattern_matches_reference(tmp_path):
    when = datetime(2026, 10, 2, 14, 3, 21)
    assert naming.next_path(tmp_path, "{Mon}{dd}_{n}", ".mp3", when).name == "Oct02_1.mp3"


def test_never_overwrites_any_format(tmp_path):                    # TC-04, FR-FILE-002
    when = datetime(2026, 10, 2)
    for i in (1, 2, 3):
        (tmp_path / f"Oct02_{i}.wav").write_bytes(b"x")
    assert naming.next_path(tmp_path, "{Mon}{dd}_{n}", ".mp3", when).name == "Oct02_4.mp3"


def test_pattern_without_n_appends_suffix(tmp_path):
    when = datetime(2026, 10, 2)
    (tmp_path / "Interview.mp3").write_bytes(b"x")
    assert naming.next_path(tmp_path, "Interview", ".mp3", when).name == "Interview_2.mp3"


def test_subfolders_and_tokens(tmp_path):
    p = naming.next_path(tmp_path, "{yyyy}/{mm}/{Month}_{HH}{MM}_{n3}", ".wav", datetime(2026, 1, 5, 9, 7))
    assert p.relative_to(tmp_path).as_posix() == "2026/01/January_0907_001.wav"


def test_invalid_patterns():
    assert naming.validate_pattern("a:b")
    assert naming.validate_pattern("CON")
    assert naming.validate_pattern("") is not None
    assert naming.validate_pattern("{yyyy}/{Mon}{dd}_{n}") is None


def test_settings_roundtrip_and_backup(isolated_config):
    s = Settings()
    s.load()
    s.set("vox.threshold", 12)
    s.set("vox.threshold", 13)
    t = Settings()
    t.load()
    assert t.get("vox.threshold") == 13
    assert (isolated_config / "settings.json.bak").exists()


def test_invalid_settings_file_is_renamed(isolated_config):         # FR-CFG-003
    (isolated_config / "settings.json").write_text("{ not json", encoding="utf-8")
    s = Settings()
    s.load()
    assert s.get("vox.threshold") == 4
    assert any(p.name.startswith("settings.invalid-") for p in isolated_config.iterdir())
    assert s.load_messages


def test_out_of_range_values_are_clamped(isolated_config):         # FR-CFG-008
    data = {"schema_version": 1, "settings": {"vox.threshold": 500, "fmt.codec": "ogg", "x.unknown": 1}}
    (isolated_config / "settings.json").write_text(json.dumps(data), encoding="utf-8")
    s = Settings()
    s.load()
    assert s.get("vox.threshold") == 100
    assert s.get("fmt.codec") == "mp3"
    assert s.get("x.unknown") == 1                                   # unknown keys are kept


def test_newer_schema_is_read_only(isolated_config):               # FR-CFG-004
    data = {"schema_version": 99, "settings": {"vox.threshold": 9}}
    (isolated_config / "settings.json").write_text(json.dumps(data), encoding="utf-8")
    s = Settings()
    s.load()
    assert s.read_only and s.get("vox.threshold") == 9
    s.set("vox.threshold", 10)
    assert json.loads((isolated_config / "settings.json").read_text())["settings"]["vox.threshold"] == 9


def test_export_import(tmp_path, isolated_config):
    s = Settings()
    s.load()
    s.set("play.speed", 150)
    out = tmp_path / "x.vrpsettings"
    s.export_to(str(out))
    s.set("play.speed", 100)
    assert s.import_from(str(out)) == []
    assert s.get("play.speed") == 150


def test_validate_value_types():
    assert validate_value("rec.timestamps", "yes") == (True, False)
    assert validate_value("vox.hang_s", "abc")[0] == 2.0
