"""Retention register: audio kept for checking and transcripts deleted after N days (1.1.0)."""
import json
import os

from vrp.transcribe.retention import AUDIO, DAY, TEXT, Retention


def touch(p):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(b"x")
    return p


def test_add_find_remove_and_persist(tmp_path):
    reg = str(tmp_path / "cfg" / "retention.json")
    a = touch(str(tmp_path / "rec" / "a.mp3"))
    r = Retention(reg)
    r.add(a, AUDIO, 7, now=1000)
    r.add(a, AUDIO, 3, now=1000)                       # re-adding replaces, not duplicates
    r.add(a, TEXT, 30, now=1000)
    assert [it["due"] for it in r.find(a, AUDIO)] == [1000 + 3 * DAY]
    assert len(r.find(a)) == 2
    r2 = Retention(reg)                                # saved and loaded back
    assert r2.items == r.items and not os.path.exists(reg + ".tmp")
    r2.remove(a)
    assert Retention(reg).items == []


def test_sweep_deletes_only_due_registered_files(tmp_path):
    reg = str(tmp_path / "retention.json")
    due = touch(str(tmp_path / "due.mp3"))
    later = touch(str(tmp_path / "later.mp3"))
    busy = touch(str(tmp_path / "busy.mp3"))
    stranger = touch(str(tmp_path / "not-registered.mp3"))
    gone = str(tmp_path / "already-gone.txt")
    r = Retention(reg)
    for p, days in ((due, 1), (later, 10), (busy, 1), (gone, 1)):
        r.add(p, AUDIO, days, now=0)
    done = dict(r.sweep(now=2 * DAY, busy=lambda p: p == busy))
    assert done == {due: "", busy: "in use", gone: ""}
    assert not os.path.exists(due) and os.path.exists(later) and os.path.exists(busy)
    assert os.path.exists(stranger)                    # never touched
    assert sorted(it["path"] for it in Retention(reg).items) == sorted([later, busy])


def test_sweep_keeps_item_when_delete_fails(tmp_path):
    r = Retention(str(tmp_path / "r.json"))
    p = touch(str(tmp_path / "locked.mp3"))
    r.add(p, AUDIO, 0, now=0)

    def fail(_p):
        raise PermissionError("locked")
    assert r.sweep(now=1, delete=fail) == [(p, "locked")]
    assert r.find(p)


def test_damaged_register_is_checked_field_by_field(tmp_path):
    reg = tmp_path / "retention.json"
    reg.write_text(json.dumps({"items": [
        {"path": "C:/ok.mp3", "kind": "audio", "due": 5},
        {"path": "", "kind": "audio", "due": 5}, {"path": "x", "kind": "exe", "due": 5},
        {"path": "x", "kind": "text", "due": "soon"}, "junk", 7]}), encoding="utf-8")
    assert [it["path"] for it in Retention(str(reg)).items] == ["C:/ok.mp3"]
    reg.write_text("{not json", encoding="utf-8")
    assert Retention(str(reg)).items == []
    reg.write_text("[1, 2]", encoding="utf-8")
    assert Retention(str(reg)).items == []
