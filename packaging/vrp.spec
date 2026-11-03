# PyInstaller spec: one folder, two programs sharing the same libraries.
#   VoiceRecorderPro.exe  windowed GUI
#   vrp.exe               console command line (same code, prints to the console)
# One-folder mode starts faster and keeps LGPL libraries as replaceable files (Section 20).
import os

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
ICON = os.path.join(ROOT, "packaging", "vrp.ico")

datas = [
    (os.path.join(ROOT, "docs"), "docs"),
    (os.path.join(ROOT, "README.md"), "."),
    (os.path.join(ROOT, "THIRD-PARTY-LICENSES.txt"), "."),
]
datas += collect_data_files("soundfile")       # _soundfile_data: libsndfile DLL (with MP3 support)
datas += collect_data_files("sounddevice")     # _sounddevice_data: PortAudio DLL
binaries = collect_dynamic_libs("soxr") + collect_dynamic_libs("hid")

a = Analysis(
    [os.path.join(ROOT, "packaging", "launcher.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=["vrp.ui.options_dialog", "vrp.ui.dialogs", "vrp.headless", "vrp.control",
                   "serial.tools.list_ports", "hid", "lameenc", "soxr", "pyaudiowpatch",
                   "vrp.core.loopback"],
    excludes=["tkinter", "matplotlib", "scipy", "PIL", "yaml", "IPython", "pandas", "pytest",
              "PySide6.QtWebEngineCore", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.Qt3DCore",
              "PySide6.QtMultimedia", "PySide6.QtPdf", "PySide6.QtOpenGL", "PySide6.QtSql"],
    noarchive=False,
)
# Qt files a widgets-only app never loads: software OpenGL, Qt Quick/QML, PDF, translations.
_UNUSED = ("opengl32sw", "qt6quick", "qt6qml", "qt6pdf", "qt6virtualkeyboard", "qt6opengl", "qt6sql",
           "/translations/", "\\translations\\")
a.binaries = [b for b in a.binaries if not any(u in b[0].lower().replace("\\", "/") for u in _UNUSED)]
a.datas = [d for d in a.datas if not any(u in d[0].lower().replace("\\", "/") for u in _UNUSED)]
pyz = PYZ(a.pure)

gui = EXE(pyz, a.scripts, [], exclude_binaries=True, name="VoiceRecorderPro", console=False, icon=ICON,
          version=os.path.join(ROOT, "packaging", "version_info.txt"), upx=False)
cli = EXE(pyz, a.scripts, [], exclude_binaries=True, name="vrp", console=True, icon=ICON,
          version=os.path.join(ROOT, "packaging", "version_info.txt"), upx=False)

coll = COLLECT(gui, cli, a.binaries, a.datas, strip=False, upx=False, name="VoiceRecorderPro")
