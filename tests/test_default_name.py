"""1.0.4: default file name VOX<yyyymmdd>-<###> and migration of the old default."""
import json
from datetime import datetime

from vrp.core import naming
from vrp.settings import (DEFAULT_FILE_PATTERN, OLD_DEFAULT_FILE_PATTERN, SETTINGS_REV, Settings,
                          defaults)


def test_new_default_pattern():
    assert DEFAULT_FILE_PATTERN == "VOX{yyyy}{mm}{dd}-{n3}"
    assert defaults()["rec.pattern"] == DEFAULT_FILE_PATTERN
    assert naming.validate_pattern(DEFAULT_FILE_PATTERN) is None


def test_names_count_up_and_restart_each_day(tmp_path):
    day1 = datetime(2026, 11, 23, 9, 30)
    first = naming.next_path(tmp_path, DEFAULT_FILE_PATTERN, ".mp3", day1)
    assert first.name == "VOX20261123-001.mp3"
    first.write_bytes(b"x")
    assert naming.next_path(tmp_path, DEFAULT_FILE_PATTERN, ".wav", day1).name == "VOX20261123-002.wav"
    (tmp_path / "VOX20261123-002.flac").write_bytes(b"x")          # any format counts as taken
    assert naming.next_path(tmp_path, DEFAULT_FILE_PATTERN, ".mp3", day1).name == "VOX20261123-003.mp3"
    # an untouched copy (1.0.4) does not take a number
    (tmp_path / "VOX20261123-003-original.mp3").write_bytes(b"x")
    assert naming.next_path(tmp_path, DEFAULT_FILE_PATTERN, ".mp3", day1).name == "VOX20261123-003.mp3"
    day2 = datetime(2026, 11, 24, 0, 0, 1)
    assert naming.next_path(tmp_path, DEFAULT_FILE_PATTERN, ".mp3", day2).name == "VOX20261124-001.mp3"
    # 999 files on one day: the number grows to four digits rather than failing
    for i in range(1, 1000):
        (tmp_path / f"VOX20261125-{i:03d}.mp3").write_bytes(b"")
    assert naming.next_path(tmp_path, DEFAULT_FILE_PATTERN, ".mp3", datetime(2026, 11, 25)).name == \
        "VOX20261125-1000.mp3"


def _write_settings(path, values: dict):
    path.write_text(json.dumps({"schema_version": 1, "app": "VoiceRecorderPro", "settings": values}),
                    encoding="utf-8")


def test_old_default_is_migrated(isolated_config):
    s = Settings()
    _write_settings(s.path, {"rec.pattern": OLD_DEFAULT_FILE_PATTERN, "fmt.codec": "wav"})
    s.load()
    assert s.get("rec.pattern") == DEFAULT_FILE_PATTERN
    assert s.get("fmt.codec") == "wav"                        # everything else is kept
    assert s.get("general.settings_rev") == SETTINGS_REV
    assert not s.load_messages                                # no pop-up for this change
    s.save()
    s2 = Settings()
    s2.load()
    assert s2.get("rec.pattern") == DEFAULT_FILE_PATTERN


def test_own_pattern_is_kept(isolated_config):
    s = Settings()
    _write_settings(s.path, {"rec.pattern": "{yyyy}/{Mon}{dd}_{n}"})
    s.load()
    assert s.get("rec.pattern") == "{yyyy}/{Mon}{dd}_{n}"


def test_migration_runs_once(isolated_config):
    # a user who changes back to the old pattern after migrating keeps it
    s = Settings()
    _write_settings(s.path, {"rec.pattern": OLD_DEFAULT_FILE_PATTERN, "general.settings_rev": SETTINGS_REV})
    s.load()
    assert s.get("rec.pattern") == OLD_DEFAULT_FILE_PATTERN


def test_revision_not_exported(isolated_config, tmp_path):
    s = Settings()
    out = tmp_path / "x.vrpsettings"
    s.export_to(str(out))
    assert "general.settings_rev" not in json.loads(out.read_text(encoding="utf-8"))["settings"]
