"""Render the app icon to packaging/vrp.ico (PNG-compressed ICO entries)."""
import os
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSize  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

app = QGuiApplication([])
from vrp.ui.icons import app_icon  # noqa: E402

icon = app_icon()
images = []
for size in (16, 20, 24, 32, 40, 48, 64, 128, 256):
    pm = icon.pixmap(QSize(size, size))
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.WriteOnly)
    pm.save(buf, "PNG")
    images.append((size, bytes(ba)))
header = struct.pack("<HHH", 0, 1, len(images))
offset = 6 + 16 * len(images)
entries, data = b"", b""
for size, png in images:
    s = 0 if size >= 256 else size
    entries += struct.pack("<BBBBHHII", s, s, 0, 0, 1, 32, len(png), offset + len(data))
    data += png
out = ROOT / "packaging" / "vrp.ico"
out.write_bytes(header + entries + data)
print(out)
