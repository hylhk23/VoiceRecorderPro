"""Out-of-process tests: kill during recording (TC-06), exit codes and IPC (TC-16)."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def vrp(*args, env=None, timeout=60):
    return subprocess.run([sys.executable, "-m", "vrp", *args], cwd=ROOT, env=env, capture_output=True,
                          text=True, timeout=timeout)


@pytest.fixture
def env(tmp_path, isolated_config):
    e = dict(os.environ)
    e["VRP_CONFIG_DIR"] = str(isolated_config)
    e["QT_QPA_PLATFORM"] = "offscreen"
    settings = {"schema_version": 1, "settings": {"rec.folder": str(tmp_path / "rec"), "vox.enabled": False,
                                                  "general.first_run_done": True}}
    (isolated_config / "settings.json").write_text(json.dumps(settings))
    return e


@pytest.mark.skipif(sys.platform == "win32", reason="uses SIGKILL")
@pytest.mark.parametrize("codec", ["wav", "mp3"])
def test_tc06_kill_and_recover(tmp_path, env, codec):                    # TC-06, NFR-007
    p = subprocess.Popen([sys.executable, "-m", "vrp", "record", "--headless", "--fake-audio", "--format", codec],
                         cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    recorded = 0.0
    deadline = time.time() + 20
    while time.time() < deadline and recorded < 3.0:          # let ~3 s reach the file
        r = vrp("status", "--json", env=env)
        if r.returncode == 0:
            recorded = json.loads(r.stdout)["duration_s"]
        time.sleep(0.2)
    p.send_signal(signal.SIGKILL)
    p.wait(10)
    os.environ["VRP_CONFIG_DIR"] = env["VRP_CONFIG_DIR"]
    from vrp.core import journal
    from vrp.core.audiofile import probe
    pending = journal.find_pending([str(tmp_path / "rec")])
    assert len(pending) == 1
    ok, msg = journal.recover(pending[0])
    assert ok, msg
    m = probe(pending[0].audio)
    assert recorded >= 3.0
    assert m.duration >= recorded - 1.3          # at most one flush interval (1 s) is lost
    assert not journal.find_pending([str(tmp_path / "rec")])


def test_exit_codes(tmp_path, env):
    assert vrp("status", env=env).returncode == 4
    assert vrp("stop", env=env).returncode == 4
    assert vrp("extract", "missing.wav", "--from", "1", "--to", "2", env=env).returncode == 3
    assert vrp("extract", env=env).returncode == 1
    assert vrp("--help", env=env).returncode == 0


def test_tc16_control_running_instance(tmp_path, env):                   # TC-16, FR-CLI-001/002
    p = subprocess.Popen([sys.executable, "-m", "vrp", "record", "--headless", "--fake-audio", "--format", "wav"],
                         cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.time() + 15
        status = None
        while time.time() < deadline:
            r = vrp("status", "--json", env=env)
            if r.returncode == 0:
                status = json.loads(r.stdout)
                break
            time.sleep(0.3)
        assert status and status["state"] == "recording" and status["schema"] == 1
        assert vrp("record", env=env).returncode == 5           # already recording
        assert vrp("pause", env=env).returncode == 0
        assert vrp("resume", env=env).returncode == 0
        assert vrp("stop", env=env).returncode == 0
        out, _err = p.communicate(timeout=15)
        assert p.returncode == 0
        assert out.strip().endswith(".wav") and os.path.exists(out.strip())
    finally:
        if p.poll() is None:
            p.kill()
