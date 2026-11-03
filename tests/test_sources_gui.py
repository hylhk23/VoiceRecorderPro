"""Main window: Mic / Computer tick boxes and the second meter; Options; About box (v1.0.2)."""
import pytest

from conftest import pump, wait_for


@pytest.fixture
def ctl(qapp, tmp_path):
    import vrp.app as A
    from vrp.settings import Settings
    s = Settings()
    s.update({"rec.folder": str(tmp_path / "rec"), "general.first_run_done": True, "vox.enabled": False,
              "rec.src_mic": True, "rec.src_system": False})
    c = A.AppController(qapp, A._gui_args(["--fake-audio"]))
    c.backend.realtime = 8.0
    c.window.show()
    yield c
    c.quit()


def test_tick_boxes_follow_settings_and_work_while_recording(qapp, ctl):
    w, e = ctl.window, ctl.engine
    pump(qapp, 0.1)
    assert w.src_mic.isChecked() and not w.src_sys.isChecked() and w.src_sys.isEnabled()
    assert w.actions_by_key["src_mic"].isChecked() and not w.actions_by_key["src_system"].isChecked()
    w.src_sys.click()                                     # tick before recording: only a setting
    assert e.settings.get("rec.src_system") is True and not e.recorder.input_open
    assert w.tray_src["rec.src_system"].isChecked()
    e.record()
    assert wait_for(qapp, lambda: e.state.value == "recording")
    assert e.recorder.mic_open and e.recorder.system_open
    assert wait_for(qapp, lambda: max(w.meter_sys.levels) > 0.05)    # second bar shows computer audio
    w.src_mic.click()                                     # untick Mic in the middle of recording
    assert not e.recorder.mic_open and e.state.value == "recording"
    w.src_sys.click()                                     # untick Computer too: recording silence
    pump(qapp, 0.1)
    assert not e.recorder.system_open
    assert "No source ticked" in w.disk.text()
    w.src_mic.click()                                     # and back on
    assert e.recorder.mic_open
    e.stop()
    assert wait_for(qapp, lambda: e.state.value == "stopped")


def test_options_record_from(qapp, ctl):
    from vrp.ui.options_dialog import OptionsDialog
    d = OptionsDialog(ctl.settings, ctl.engine, ctl.hotkeys, ctl.ext, ctl.backend, "Recording", ctl.window)
    d.show()
    pump(qapp, 0.05)
    assert d.src_combo.currentData() == "mic"
    assert d.apply() and ctl.settings.get("rec.src_mic") is True       # unchanged: ticks untouched
    d.src_combo.setCurrentIndex(d.src_combo.findData("system"))
    assert d.apply()
    assert ctl.settings.get("rec.src_mic") is False and ctl.settings.get("rec.src_system") is True
    assert not ctl.window.src_mic.isChecked() and ctl.window.src_sys.isChecked()
    d.done(0)


def test_about_box(qapp, ctl):
    from vrp import __version__
    w = ctl.window
    w.about()
    pump(qapp, 0.05)
    dlg = w._about
    assert __version__ == "1.0.2" and "1.0.2" in dlg.title.text()
    assert "https://software.4u.hk" in dlg.link.text() and dlg.link.openExternalLinks()
    fine = dlg.fine.text()
    assert "Neo Time Consulting" in fine and "ABSOLUTELY NO WARRANTY" in fine and "©" in fine
    assert dlg.fine.font().pointSizeF() < dlg.body.font().pointSizeF()       # fine print
    dlg.accept()
