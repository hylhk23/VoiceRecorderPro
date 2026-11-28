"""Offscreen GUI smoke test: window, menus enabled per state."""
import pytest

from conftest import pump, wait_for


@pytest.fixture
def ctl(qapp, tmp_path):
    import vrp.app as A
    from vrp.settings import Settings
    s = Settings()
    s.update({"rec.folder": str(tmp_path / "rec"), "general.first_run_done": True, "vox.enabled": False})
    args = A._gui_args(["--fake-audio"])
    c = A.AppController(qapp, args)
    c.backend.realtime = 8.0
    c.window.show()
    yield c
    c.quit()


def test_window_states(qapp, ctl):
    w, e = ctl.window, ctl.engine
    pump(qapp, 0.2)
    A = w.actions_by_key
    assert w.buttons["record"].isEnabled() and not w.buttons["play"].isEnabled()
    assert not A["extract"].isEnabled()
    e.record()
    pump(qapp, 0.5)
    assert "[REC]" in w.windowTitle()
    assert not w.position.isEnabled() and not A["open"].isEnabled()
    e.stop()
    assert wait_for(qapp, lambda: e.state.value == "stopped")
    pump(qapp, 0.1)
    assert w.buttons["play"].isEnabled() and w.position.isEnabled()
    e.set_selection(0, e.file.frames // 2)
    pump(qapp, 0.1)
    assert A["extract"].isEnabled() and w.disk.text().startswith("Sel ")
    assert w.f_segment.text() == "1 of 1"


def test_hide_vox_and_options_dialog(qapp, ctl, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from vrp.ui.options_dialog import OptionsDialog
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a[1:3]))
    ctl.settings.set("vox.hidden", True)
    pump(qapp, 0.1)
    assert not ctl.window.vox_slider.isVisible()
    d = OptionsDialog(ctl.settings, ctl.engine, ctl.hotkeys, ctl.ext, ctl.backend, "Recording", ctl.window)
    d.show()
    pump(qapp, 0.1)
    d.pattern.setText("CON")
    assert not d.apply()                          # invalid pattern refused
    assert warnings and warnings[0][0] == "File names"
    d.pattern.setText("{yyyy}/{Mon}{dd}_{n}")
    assert d.apply()
    assert ctl.settings.get("rec.pattern") == "{yyyy}/{Mon}{dd}_{n}"
    d.done(0)


def _drawn_contrast(widget):
    """Contrast between the commonest colour inside a widget (its background) and the pixel that differs most
    from it (the text), as drawn."""
    from collections import Counter

    from colour_check import contrast
    img = widget.grab().toImage()
    px = [img.pixelColor(x, y).name() for y in range(img.height()) for x in range(img.width())]
    bg = Counter(px).most_common(1)[0][0]
    return max(contrast(p, bg) for p in set(px)), bg


def _windows11_like_style():
    """Fusion with the two rules of Qt's Windows 11 style that hid the Vox button (qwindows11style.cpp:
    CE_PushButtonBevel fills a flat button only while hovered or pressed; controlTextColor gives a checked
    button the text-on-accent colour, white). The real style exists only on Windows."""
    from PySide6.QtGui import QColor, QPalette
    from PySide6.QtWidgets import QProxyStyle, QStyle, QStyleOptionButton

    class Windows11Like(QProxyStyle):
        def drawControl(self, element, option, painter, widget=None):
            if element == QStyle.CE_PushButtonBevel and option.features & QStyleOptionButton.Flat:
                return
            if element == QStyle.CE_PushButtonLabel and option.state & QStyle.State_On:
                option = QStyleOptionButton(option)
                option.palette.setColor(QPalette.ButtonText, QColor("#ffffff"))
            super().drawControl(element, option, painter, widget)
    return Windows11Like("Fusion")


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_vox_button_is_readable(qapp, ctl, theme):
    """Regression (1.1.2): on Windows 11 the "Vox" button under the meters was almost invisible while Vox was on,
    white text on the light window (it shows in the 1.0.3 and 1.1.1 screenshots). Root cause: the button was flat
    and checkable. Qt's Windows 11 style never fills a flat button but draws a checked button's text in the white
    meant for the accent fill. Missed because the GUI tests run in the Fusion style, which fills a checked flat
    button, and checked colour pairs from the app's own tables, not what the window draws. Now no flat button is
    checkable, on or off shows in the text (struck through when off) and in the accessible name, and the drawn
    label is measured in both themes."""
    from PySide6.QtGui import QColor, QPalette
    from PySide6.QtWidgets import QPushButton
    w, e = ctl.window, ctl.engine
    old_palette = QPalette(w.palette())
    if theme == "dark":                     # offscreen Qt ignores the colour scheme hint: set a dark palette
        pal = QPalette(old_palette)
        for role, colour in ((QPalette.Window, "#202020"), (QPalette.WindowText, "#f2f2f2"),
                             (QPalette.Button, "#2d2d2d"), (QPalette.ButtonText, "#f2f2f2")):
            pal.setColor(role, QColor(colour))
        w.setPalette(pal)
    pump(qapp, 0.1)
    assert not [b.text() for b in w.findChildren(QPushButton) if b.isFlat() and b.isCheckable()]
    for on in (True, False):
        if e.vox_enabled() != on:
            e.toggle_vox()
        pump(qapp, 0.1)
        assert not w.vox_btn.isCheckable()
        assert w.vox_btn.font().strikeOut() == (not on)
        assert w.vox_btn.accessibleName() == ("Vox on" if on else "Vox off")
        assert ("is on" if on else "is off") in w.vox_btn.toolTip()
        ratio, bg = _drawn_contrast(w.vox_btn)
        assert ratio >= 4.5, (theme, on, bg, round(ratio, 2))
        style = _windows11_like_style()
        w.vox_btn.setStyle(style)
        try:
            ratio, bg = _drawn_contrast(w.vox_btn)
            assert ratio >= 4.5, ("Windows 11 rules", theme, on, bg, round(ratio, 2))
        finally:
            w.vox_btn.setStyle(None)
    w.setPalette(old_palette)
