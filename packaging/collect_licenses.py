"""Copy the licence files of every bundled library into <dist>/licenses (Section 20.2)."""
import shutil
import sys
from importlib import metadata
from pathlib import Path

PACKAGES = ["PySide6-Essentials", "shiboken6", "numpy", "soundfile", "sounddevice", "lameenc", "soxr",
            "pyserial", "hidapi", "cffi", "pycparser", "PyAudioWPatch"]


def main(dist: str) -> None:
    out = Path(dist) / "licenses"
    out.mkdir(parents=True, exist_ok=True)
    index = []
    for name in PACKAGES:
        try:
            d = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        lic = d.metadata.get("License-Expression") or d.metadata.get("License") or ""
        copied = 0
        for f in d.files or []:
            low = f.name.lower()
            if any(k in low for k in ("license", "licence", "copying", "notice", "authors")):
                src = Path(d.locate_file(f))
                if src.is_file():
                    shutil.copy2(src, out / f"{name}-{f.name}")
                    copied += 1
        index.append(f"{name} {d.version}: {lic.splitlines()[0] if lic else 'see files'} ({copied} file(s))")
    (out / "INDEX.txt").write_text("\n".join(index) + "\n", encoding="utf-8")
    print(f"licences collected in {out}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "dist/VoiceRecorderPro")
