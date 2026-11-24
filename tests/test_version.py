"""The version number lives in vrp/__init__.py; every copy the build tools need must match it.

Known limitation since 1.0.0: the build files keep their own copies. This test
makes a mismatch fail instead of relying on a check by hand at release time.
"""
import re
from pathlib import Path

from vrp import __version__

ROOT = Path(__file__).resolve().parent.parent


def test_every_copy_matches():
    parts = tuple(int(p) for p in __version__.split("."))
    text = {p: (ROOT / p).read_text(encoding="utf-8") for p in
            ("pyproject.toml", "build.bat", "packaging/installer.iss", "packaging/version_info.txt")}
    assert re.search(r'^version = "%s"$' % re.escape(__version__), text["pyproject.toml"], re.M)
    assert f'set "APPVER={__version__}"' in text["build.bat"]
    assert f'#define AppVersion "{__version__}"' in text["packaging/installer.iss"]
    vi = text["packaging/version_info.txt"]
    four = "(%d, %d, %d, 0)" % parts
    assert f"filevers={four}" in vi and f"prodvers={four}" in vi
    assert vi.count(f"'{__version__}'") == 2


def test_about_box_shows_it():
    from vrp.ui.dialogs import about_text
    assert f"Voice Recorder Pro {__version__}" in about_text("libs")
