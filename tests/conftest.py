import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Every test gets its own settings/log folder."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setenv("VRP_CONFIG_DIR", str(cfg))
    yield cfg


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtWidgets import QApplication
    app = QCoreApplication.instance() or QApplication([])
    yield app


def pump(app, seconds: float) -> None:
    import time
    t0 = time.time()
    while time.time() - t0 < seconds:
        app.processEvents()
        time.sleep(0.005)


def wait_for(app, pred, timeout: float = 5.0) -> bool:
    import time
    t0 = time.time()
    while time.time() - t0 < timeout:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.01)
    return pred()
