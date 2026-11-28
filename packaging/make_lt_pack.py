"""Build the Live Transcript pack: VoiceRecorderPro-LiveTranscript-pack-<version>-win64.zip.

The pack is a separate, optional download that the user extracts over the program folder. It adds:

  VoiceRecorderPro\\python\\Lib\\site-packages\\sherpa_onnx\\...   speech engine (sherpa-onnx, ONNX Runtime DLLs)
  VoiceRecorderPro\\python\\Lib\\site-packages\\opencc\\...        Chinese character conversion
  VoiceRecorderPro\\transcribe\\pack.json                          version, file sizes and SHA-256
  VoiceRecorderPro\\transcribe\\models\\sensevoice-small-int8\\     model.int8.onnx, tokens.txt
  VoiceRecorderPro\\transcribe\\models\\silero-vad\\                silero_vad.onnx
  VoiceRecorderPro\\transcribe\\LICENCES\\                         licence texts of everything above
  VoiceRecorderPro\\transcribe\\README.txt

Usage (any OS with pip; nothing is downloaded by this script):
  python packaging/make_lt_pack.py --wheels <folder with the three .whl files> --models <folder>
         --licences <folder> --out <zip>

<models> holds model.int8.onnx, tokens.txt and silero_vad.onnx; <licences> holds the licence texts named in
LICENCES below. The script refuses any .exe and checks every model file's SHA-256 against the pinned values.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from vrp import __version__  # noqa: E402

WHEELS = ("sherpa_onnx-2.0.1-cp313-cp313-win_amd64.whl", "sherpa_onnx_core-2.0.1-py3-none-win_amd64.whl",
          "opencc_python_reimplemented-0.1.7-py2.py3-none-any.whl")
# SenseVoiceSmall int8 as exported by the sherpa-onnx project (Hugging Face
# csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17, revision 2365baea) and Silero VAD
# (k2-fsa/sherpa-onnx release "asr-models", silero_vad.onnx)
MODELS = {
    "models/sensevoice-small-int8/model.int8.onnx":
        (239233841, "c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51"),
    "models/sensevoice-small-int8/tokens.txt":
        (315894, "f449eb28dc567533d7fa59be34e2abca8784f771850c78a47fb731a31429a1dc"),
    "models/silero-vad/silero_vad.onnx":
        (643854, "9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6"),
}
LICENCES = {
    "FUNASR_MODEL_LICENSE": "SenseVoiceSmall model - FunASR Model License 1.1.txt",
    "SILERO_VAD_LICENSE": "Silero VAD - MIT.txt",
    "ONNXRUNTIME_LICENSE": "ONNX Runtime - MIT.txt",
    "ONNXRUNTIME_ThirdPartyNotices.txt": "ONNX Runtime - third-party notices.txt",
}

README = """Voice Recorder Pro - Live Transcript pack {version}
=================================================

This pack lets Voice Recorder Pro {app} show and save a live transcript in English,
Cantonese and Mandarin. Speech is recognised on this computer; nothing is sent over
the internet, and no graphics card is needed.

Install: close Voice Recorder Pro, then extract the pack zip into the folder that holds
your VoiceRecorderPro folder (the place you extracted the program), choosing Replace if
asked. Start the program and choose Transcript > Live Transcript On.

Remove: delete this "transcribe" folder.

Contents and licences (full texts in the LICENCES folder):
- sherpa-onnx 2.0.1, speech recognition toolkit - Apache License 2.0
  https://github.com/k2-fsa/sherpa-onnx
- ONNX Runtime 1.28 (DLLs inside sherpa-onnx) - MIT License
  https://github.com/microsoft/onnxruntime
- SenseVoiceSmall speech model, int8 ONNX export by the sherpa-onnx project -
  FunASR Model License 1.1. The model is used unchanged; its name and copyright notice
  are kept. https://github.com/FunAudioLLM/SenseVoice
- Silero VAD voice activity detector - MIT License
  https://github.com/snakers4/silero-vad
- opencc-python-reimplemented 0.1.7, Chinese character conversion - Apache License 2.0
  https://github.com/yichen0831/opencc-python

The pack is provided "as is", without warranty. Speech recognition makes mistakes:
check important passages against the audio before you rely on them.
"""


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def build(wheels: Path, models: Path, licences: Path, out: Path) -> Path:
    stage = Path(tempfile.mkdtemp(prefix="ltpack-"))
    top = stage / "VoiceRecorderPro"
    site = top / "python" / "Lib" / "site-packages"
    tr = top / "transcribe"
    site.mkdir(parents=True)
    whl = [str(wheels / w) for w in WHEELS]
    for w in whl:
        if not os.path.isfile(w):
            raise SystemExit(f"missing wheel: {w}")
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--target", str(site), "--platform",
                    "win_amd64", "--python-version", "3.13", "--implementation", "cp", "--abi", "cp313",
                    "--only-binary=:all:", "--no-deps", "--no-compile", *whl], check=True)
    # pip copies the DLLs a second time as the wheel's data scripts (Scripts\ or bin\): not needed; nor are
    # the C headers and import libraries, or direct_url.json, which would record the build folder's path
    for extra in ("Scripts", "bin", "sherpa_onnx/include"):
        shutil.rmtree(site / extra, ignore_errors=True)
    for p in list(site.rglob("__pycache__")):
        shutil.rmtree(p, ignore_errors=True)
    for p in list(site.rglob("*.lib")) + list(site.rglob("direct_url.json")):
        p.unlink()
    exes = [p for p in stage.rglob("*") if p.suffix.lower() == ".exe"]
    if exes:
        raise SystemExit("the pack must not contain programs: " + ", ".join(str(p) for p in exes))
    files = []
    for rel, (size, digest) in MODELS.items():
        src = models / Path(rel).name
        if src.stat().st_size != size or sha256(src) != digest:
            raise SystemExit(f"{src} is not the pinned file (size or SHA-256 differs)")
        dst = tr / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        files.append({"path": rel, "size": size, "sha256": digest})
    lic = tr / "LICENCES"
    lic.mkdir(parents=True)
    for src, name in LICENCES.items():
        shutil.copyfile(licences / src, lic / name)
    for dist in site.glob("*.dist-info"):
        for p in dist.rglob("*"):
            if p.is_file() and ("LICEN" in p.name.upper() or "COPYING" in p.name.upper()):
                shutil.copyfile(p, lic / f"{dist.name.split('-')[0]} - {Path(p.name).stem}.txt")
    pack = {"format": 1, "for_app": ".".join(__version__.split(".")[:2]), "version": __version__,
            "model_name": "SenseVoiceSmall int8", "engine": "sherpa-onnx 2.0.1", "files": files}
    (tr / "pack.json").write_text(json.dumps(pack, indent=1), encoding="utf-8")
    (tr / "README.txt").write_text(README.format(version=__version__, app=pack["for_app"]).replace("\n", "\r\n"),
                                   encoding="utf-8", newline="")
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in sorted(stage.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(stage).as_posix())
    shutil.rmtree(stage, ignore_errors=True)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--wheels", type=Path, required=True)
    ap.add_argument("--models", type=Path, required=True)
    ap.add_argument("--licences", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=ROOT / "dist" /
                    f"VoiceRecorderPro-LiveTranscript-pack-v{__version__}-win64.zip")
    a = ap.parse_args()
    out = build(a.wheels, a.models, a.licences, a.out)
    print(f"{out}  {out.stat().st_size:,} bytes  SHA-256 {sha256(out)}")


if __name__ == "__main__":
    main()
