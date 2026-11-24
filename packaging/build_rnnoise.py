"""Build the RNNoise library used for noise suppression (v1.0.4).

Downloads the official RNNoise 0.2 release from Xiph.Org's GitHub, checks its SHA-256,
and compiles the library with no changes to the source:

    python packaging/build_rnnoise.py windows   -> vrp/lib/rnnoise.dll   (needs: pip install ziglang)
    python packaging/build_rnnoise.py linux     -> vrp/lib/librnnoise.so (needs: gcc; for the tests)

The release tarball contains the trained model (src/rnnoise_data.c), so nothing else is
downloaded. RNNoise is BSD-3-Clause; its notice is in THIRD-PARTY-LICENSES.txt.
The Windows DLL is cross-compiled with zig (clang) for x86_64-windows-gnu; it imports
only KERNEL32 and the Universal C Runtime, which every Windows 10 and 11 PC has.
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

URL = "https://github.com/xiph/rnnoise/releases/download/v0.2/rnnoise-0.2.tar.gz"
SHA256 = "90fce4b00b9ff24c08dbfe31b82ffd43bae383d85c5535676d28b0a2b11c0d37"
SOURCES = ["denoise.c", "rnn.c", "pitch.c", "kiss_fft.c", "celt_lpc.c", "nnet.c", "nnet_default.c",
           "parse_lpcnet_weights.c", "rnnoise_data.c", "rnnoise_tables.c"]
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "vrp" / "lib"


def fetch(dest: Path) -> Path:
    tgz = dest / "rnnoise-0.2.tar.gz"
    with urllib.request.urlopen(URL) as r, open(tgz, "wb") as f:
        shutil.copyfileobj(r, f)
    digest = hashlib.sha256(tgz.read_bytes()).hexdigest()
    if digest != SHA256:
        raise SystemExit(f"SHA-256 mismatch: {digest}")
    with tarfile.open(tgz) as t:
        t.extractall(dest, filter="data")
    return dest / "rnnoise-0.2"


def build(target: str) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        src = fetch(Path(tmp))
        files = [str(src / "src" / s) for s in SOURCES]
        inc = ["-I" + str(src / "include"), "-I" + str(src / "src")]
        if target == "windows":
            out = OUT / "rnnoise.dll"
            cmd = [sys.executable, "-m", "ziglang", "cc", "-target", "x86_64-windows-gnu", "-O2", "-shared",
                   "-DWIN32", "-DRNNOISE_BUILD", "-DDLL_EXPORT", *inc, *files, "-o", str(out)]
        elif target == "linux":
            out = OUT / "librnnoise.so"
            cmd = ["gcc", "-O2", "-fPIC", "-shared", "-fvisibility=hidden", "-DRNNOISE_BUILD", *inc, *files,
                   "-lm", "-o", str(out)]
        else:
            raise SystemExit("target must be windows or linux")
        subprocess.run(cmd, check=True)
        shutil.copy(src / "COPYING", OUT / "RNNOISE-COPYING.txt")
    print(out, out.stat().st_size, hashlib.sha256(out.read_bytes()).hexdigest())
    return out


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "windows")
