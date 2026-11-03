"""Taskbar recording badge: drawing, pixel packing for the Windows icon, state mapping."""
import uuid

from conftest import pump, wait_for


def test_badge_images(qapp):
    from PySide6.QtGui import QColor
    from vrp.ui import icons
    for size in (16, 20, 24, 32):
        rec = icons.badge_image("recording", size)
        assert (rec.width(), rec.height()) == (size, size)
        c = rec.pixelColor(size // 2, size // 2)
        assert (c.red(), c.green(), c.blue(), c.alpha()) == (0xd9, 0x30, 0x25, 255)    # red dot
        assert rec.pixelColor(0, 0).alpha() == 0                                         # round, not square
        armed = icons.badge_image("armed", size).pixelColor(size // 2, size // 2)
        assert armed == QColor("#f2a900")
    paused = icons.badge_image("paused", 32)
    assert paused.pixelColor(13, 16) == QColor("#ffffff")                               # pause bar
    assert icons.BADGE_FOR_STATE.get("recording") == "recording"
    for st in ("idle", "stopped", "playing", "play_paused", "seeking"):
        assert icons.BADGE_FOR_STATE.get(st) is None


def test_bgra_rows_and_guid(qapp):
    from PySide6.QtGui import QColor, QImage
    from vrp.ui import taskbar
    img = QImage(3, 2, QImage.Format_ARGB32)
    img.fill(QColor(0, 0, 0, 0))
    img.setPixelColor(0, 0, QColor(10, 20, 30, 40))
    img.setPixelColor(2, 1, QColor(200, 150, 100, 255))
    w, h, raw = taskbar.bgra_rows(img)
    assert (w, h, len(raw)) == (3, 2, 24)
    assert raw[0:4] == bytes([30, 20, 10, 40])           # B, G, R, A, straight (not premultiplied) alpha
    assert raw[20:24] == bytes([100, 150, 200, 255])     # last pixel of the second (bottom) row
    g = taskbar.make_guid(taskbar.CLSID_TASKBAR_LIST)
    assert g.Data1 == 0x56FDF344 and g.Data2 == 0xFD6D and g.Data3 == 0x11D0
    assert bytes(g) == uuid.UUID(taskbar.CLSID_TASKBAR_LIST).bytes_le


def test_badge_is_noop_off_windows(qapp):
    from PySide6.QtWidgets import QWidget
    from vrp.ui import icons, taskbar
    w = QWidget()
    b = taskbar.TaskbarBadge(w, icons.badge_image)
    if not taskbar.IS_WINDOWS:
        assert b.msg_id == 0
        b.set("recording")
        assert b.wanted == "recording" and b.shown is None
        assert b.on_native_message(12345) is False


def test_window_badge_follows_recording(qapp, tmp_path):
    import vrp.app as A
    from vrp.settings import Settings
    s = Settings()
    s.update({"rec.folder": str(tmp_path / "rec"), "general.first_run_done": True, "vox.enabled": False})
    c = A.AppController(qapp, A._gui_args(["--fake-audio"]))
    c.backend.realtime = 8.0
    try:
        w, e = c.window, c.engine
        w.show()
        pump(qapp, 0.2)
        assert w._badge_kind is None and w.badge.wanted is None
        idle_icon = w.windowIcon().cacheKey()
        e.record()
        assert wait_for(qapp, lambda: e.state.value == "recording")
        pump(qapp, 0.1)
        assert w._badge_kind == "recording" and w.badge.wanted == "recording"
        assert w.windowIcon().cacheKey() != idle_icon
        e.stop()
        assert wait_for(qapp, lambda: e.state.value == "stopped")
        pump(qapp, 0.1)
        assert w._badge_kind is None and w.badge.wanted is None
    finally:
        c.quit()
