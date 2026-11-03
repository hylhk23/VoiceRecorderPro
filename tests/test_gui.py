"""Offscreen GUI smoke test: window, menus enabled per state (Section 4.4)."""
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
