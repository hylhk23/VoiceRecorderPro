"""Live transcript in the window (1.1.0): layouts, mini bar, rolling line, caption bar, menus, Options tab,
recording in each save mode with the fake speech engine, and the colours drawn."""
import os

import pytest

from colour_check import contrast, cvd_distance
from conftest import pump, wait_for
from fake_speech import InProcWorker
from vrp.transcribe.pack import PackStatus

OK_PACK = PackStatus(True, "", "/pack", "m", "t", "v", "1.1.0", "SenseVoiceSmall int8", [])


@pytest.fixture
def ctl(qapp, tmp_path, monkeypatch):
    import vrp.app as A
    from vrp.core.engine import Engine
    from vrp.settings import Settings
    monkeypatch.setattr(Engine, "lt_pack_status", lambda self, refresh=False: OK_PACK)
    s = Settings()
    s.update({"rec.folder": str(tmp_path / "rec"), "general.first_run_done": True, "vox.enabled": False,
              "lt.enabled": True})
    c = A.AppController(qapp, A._gui_args(["--fake-audio"]))
    c.engine.transcriber.worker_factory = InProcWorker
    c.backend.realtime = 6.0
    c.window.show()
    pump(qapp, 0.1)
    yield c
    c.quit()


def record(qapp, c, seconds=1.2):
    c.engine.record()
    assert wait_for(qapp, lambda: c.engine.capturing)
    pump(qapp, seconds)


def stop(qapp, c):
    e = c.engine
    e.stop()
    assert wait_for(qapp, lambda: e.state.value in ("stopped", "idle"))
    assert wait_for(qapp, lambda: not e.transcriber.writers and not e.transcriber.closing)
    pump(qapp, 0.1)


# ------------------------------------------------------------------ layouts
def test_off_means_the_recorder_alone(qapp, ctl):
    w, s = ctl.window, ctl.settings
    s.set("lt.enabled", False)
    pump(qapp, 0.1)
    assert w.effective_layout() == "recorder"
    assert w.rec_panel.isVisible() and not w.transcript_panel.isVisible() and not w.rolling.isVisible()
    assert not w.layout_switch.isVisible() and not w.layout_switch_inline.isVisible()
    assert not w.layout_actions["both"].isEnabled() and w.layout_actions["recorder"].isEnabled()
    w.set_layout("transcript")
    assert s.get("ui.layout") == "both" and w.effective_layout() == "recorder"   # unchanged, hint shown
    assert "Turn on the live transcript" in w._flash_text


def test_three_layouts(qapp, ctl):
    w, s = ctl.window, ctl.settings
    assert w.effective_layout() == "both"
    assert w.rec_panel.isVisible() and w.transcript_panel.isVisible()
    assert not w.transcript_panel.mini.isVisible() and not w.rolling.isVisible()
    assert w.layout_switch.isVisible() and w.layout_btns[1]["both"].isChecked()
    w.layout_actions["transcript"].trigger()                       # View > Transcript Only (Ctrl+3)
    pump(qapp, 0.1)
    assert s.get("ui.layout") == "transcript"
    assert not w.rec_panel.isVisible() and w.transcript_panel.isVisible() and w.transcript_panel.mini.isVisible()
    w.layout_btns[1]["recorder"].click()                            # the switch in the menu bar
    pump(qapp, 0.1)
    assert w.rec_panel.isVisible() and not w.transcript_panel.isVisible() and w.rolling.isVisible()
    assert w.layout_switch_inline.isVisible() and not w.layout_switch.isVisible()
    assert w.layout_btns[0]["recorder"].isChecked() and w.layout_actions["recorder"].isChecked()
    w.layout_btns[0]["both"].click()                                # the switch in the recorder panel
    pump(qapp, 0.1)
    assert w.effective_layout() == "both"


def test_window_size_is_kept_per_layout(qapp, ctl):
    w = ctl.window
    w.resize(1000, 600)
    pump(qapp, 0.1)
    w.set_layout("transcript")
    pump(qapp, 0.1)
    w.resize(700, 500)
    pump(qapp, 0.1)
    w.set_layout("both")
    pump(qapp, 0.1)
    assert (w.width(), w.height()) == (1000, 600)
    w.set_layout("transcript")
    pump(qapp, 0.1)
    assert (w.width(), w.height()) == (700, 500)
    sizes = ctl.settings.get("ui.layout_widths")
    assert sizes["both"] == [1000, 600]


def test_stored_sizes_are_checked(qapp, ctl):
    w = ctl.window
    ctl.settings.set("ui.layout_widths", {"both": ["x", 5], "transcript": [50000, 300], "recorder": [400, 300]})
    assert w._stored_size("both") is None and w._stored_size("transcript") is None
    assert w._stored_size("recorder") == (400, 300)


# ------------------------------------------------------------------ recording with the live transcript
def test_record_audio_and_text(qapp, ctl, tmp_path):
    w, e = ctl.window, ctl.engine
    record(qapp, ctl, 1.5)
    assert e.lt_active and w.transcript_panel.status.text() == "Transcribing"
    assert w.actions_by_key["lt_bookmark"].isEnabled()
    assert wait_for(qapp, lambda: any(x.kind == "text" for x in e.transcriber.entries), 5)
    w.actions_by_key["lt_bookmark"].trigger()
    pump(qapp, 0.1)
    assert any(x.kind == "bookmark" for x in e.transcriber.entries)
    assert "phrase of" in w.transcript_panel.body.toPlainText() or "秒" in w.transcript_panel.body.toPlainText()
    assert "Bookmark" in w.transcript_panel.body.toPlainText()
    stop(qapp, ctl)
    rec = tmp_path / "rec"
    names = sorted(os.listdir(rec))
    audio = [n for n in names if n.endswith(".mp3")]
    assert audio and audio[0][:-4] + ".txt" in names and audio[0][:-4] + ".srt" in names
    assert not [n for n in names if n.endswith(".journal")]
    A = w.actions_by_key
    assert A["lt_open"].isEnabled() and A["lt_sot"].isEnabled()
    assert A["lt_checked"].isEnabled()                             # default: text, audio kept for checking
    assert w.transcript_panel.status.text() == "Ready"


def test_mark_checked_deletes_audio_keeps_text(qapp, ctl, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    record(qapp, ctl, 1.0)
    stop(qapp, ctl)
    e, w = ctl.engine, ctl.window
    path = e.file.path
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    w.mark_checked()
    assert not os.path.exists(path) and os.path.exists(os.path.splitext(path)[0] + ".txt")
    assert not e.retention.find(path) and e.file is None


def test_text_only_saves_no_sound(qapp, ctl, tmp_path):
    ctl.settings.set("lt.save", "text_only")
    w, e = ctl.window, ctl.engine
    assert "no sound is being saved" in w.transcript_panel.strip.text()
    record(qapp, ctl, 1.5)
    assert wait_for(qapp, lambda: any(x.kind == "text" for x in e.transcriber.entries), 5)
    stop(qapp, ctl)
    names = os.listdir(tmp_path / "rec")
    assert names and all(n.endswith(".txt") for n in names)         # no audio, no subtitles
    # regression: the engine kept the .txt as the open "recording" (it tested for a missing file,
    # but in Text only mode the transcript has that very name)
    assert e.file is None and e.state.value == "idle" and not ctl.settings.recent_files()
    assert w.actions_by_key["lt_open"].isEnabled()                 # the transcript just made
    txt = (tmp_path / "rec" / names[0]).read_text(encoding="utf-8")
    assert "Text only: no sound was saved" in txt and e.transcriber.entries[0].pos is None


def test_off_the_record_from_the_menu(qapp, ctl):
    w, e = ctl.window, ctl.engine
    record(qapp, ctl, 0.8)
    w.actions_by_key["lt_off"].trigger()                            # = pause
    assert wait_for(qapp, lambda: e.state.value == "rec_paused")
    assert w.transcript_panel.status.text() == "Off the record"
    assert "Back" in w.actions_by_key["lt_off"].text() and "Back" in w.transcript_panel.off_btn.text()
    w.actions_by_key["lt_off"].trigger()
    assert wait_for(qapp, lambda: e.state.value == "recording")
    pump(qapp, 0.2)
    assert e.transcriber.entries[-1].kind == "off" or any(x.kind == "off" for x in e.transcriber.entries)
    assert "Off the record" in w.transcript_panel.body.toPlainText()
    stop(qapp, ctl)


def test_rolling_line_and_caption_bar(qapp, ctl):
    w, s, e = ctl.window, ctl.settings, ctl.engine
    s.set("ui.layout", "recorder")
    pump(qapp, 0.1)
    assert "words appear here" in w.rolling.text()
    s.set("ui.caption_bar", True)
    pump(qapp, 0.1)
    cap = w._caption
    assert cap is not None and cap.isVisible() and w.caption_action.isChecked()
    record(qapp, ctl, 1.5)
    assert wait_for(qapp, lambda: any(x.kind == "text" for x in e.transcriber.entries), 5)
    pump(qapp, 0.1)
    newest = [x for x in e.transcriber.entries if x.kind == "text"][-1].text
    assert newest[-6:] in w.rolling.text() and newest[-6:] in cap.lines.text()
    size = s.get("ui.caption_size")
    cap.larger.click()
    assert s.get("ui.caption_size") == size + 2 and f"font-size:{size + 2}px" in cap.lines.text()
    stop(qapp, ctl)
    ctl.dispatch("caption_bar", True)                               # the hot-key toggles it
    pump(qapp, 0.1)
    assert w._caption is None and not s.get("ui.caption_bar")
    ctl.dispatch("caption_bar", True)
    pump(qapp, 0.1)
    w._caption.close()                                              # closing the window turns it off
    pump(qapp, 0.1)
    assert w._caption is None and not s.get("ui.caption_bar") and s.get("ui.caption_geometry")
    # regression: the window was deleted from inside its own close event (heap corruption, found as an
    # intermittent crash at exit when these tests ran before the engine tests); it is now only hidden
    import shiboken6
    assert shiboken6.isValid(cap) and w._caption_win is cap and not cap.isVisible()
    s.set("ui.caption_bar", True)
    pump(qapp, 0.1)
    assert w._caption is cap and cap.isVisible()


def test_mini_bar_shows_the_recording(qapp, ctl):
    w = ctl.window
    ctl.settings.set("ui.layout", "transcript")
    pump(qapp, 0.1)
    mini = w.transcript_panel.mini
    assert mini.state.text() == "Stopped"
    mini.rec_btn.click()
    assert wait_for(qapp, lambda: ctl.engine.capturing)
    pump(qapp, 0.3)
    assert mini.state.text().startswith("● REC") and mini.info.text().endswith("KB")
    assert mini.stop_btn.isEnabled()
    mini.stop_btn.click()
    stop(qapp, ctl)
    assert not mini.stop_btn.isEnabled()


def test_pack_missing_records_without_transcript(qapp, ctl, monkeypatch):
    from vrp.core.engine import Engine
    bad = PackStatus(False, "The Live Transcript pack is not installed.", "/x")
    monkeypatch.setattr(Engine, "lt_pack_status", lambda self, refresh=False: bad)
    ctl.settings.set("lt.mask", True)                                 # any lt.* change refreshes the panel
    pump(qapp, 0.1)
    w = ctl.window
    assert w.transcript_panel.status.text() == "Not available"
    assert "not installed" in w.transcript_panel.info.text() and not w.transcript_panel.strip.isVisible()
    record(qapp, ctl, 0.5)
    assert not ctl.engine.lt_active and ctl.engine.file.path.endswith(".mp3")
    stop(qapp, ctl)


# ------------------------------------------------------------------ options
def test_options_transcript_tab(qapp, ctl):
    from PySide6.QtWidgets import QComboBox, QRadioButton

    from vrp.ui.options_dialog import OptionsDialog
    d = OptionsDialog(ctl.settings, ctl.engine, ctl.hotkeys, ctl.ext, ctl.backend, "Transcript", ctl.window)
    d.show()
    pump(qapp, 0.1)
    assert d.tabs.tabText(d.tabs.currentIndex()) == "Transcript"
    assert "SenseVoiceSmall" in d.lt_pack_label.text()
    page = d.tabs.currentWidget()
    lang = [c for c in page.findChildren(QComboBox) if c.findData("yue") >= 0][0]
    lang.setCurrentIndex(lang.findData("yue"))
    [r for r in page.findChildren(QRadioButton) if r.text() == "Text only"][0].setChecked(True)
    assert not d.lt_keep.isEnabled()                                # days apply only to "kept for checking"
    d._lt_names = [{"heard": "neo", "correct": "Neo"}]
    assert d.apply()
    s = ctl.settings
    assert s.get("lt.language") == "yue" and s.get("lt.save") == "text_only"
    assert s.get("lt.names") == [{"heard": "neo", "correct": "Neo"}]
    d.done(0)


def test_names_dialog(qapp):
    from vrp.ui.transcript_panel import NamesDialog
    d = NamesDialog([{"heard": "a", "correct": "A"}, "junk", {"heard": "", "correct": "x"}])
    assert d.table.rowCount() == 2
    d._add("  b  ", "B" * 300)
    assert d.names() == [{"heard": "a", "correct": "A"}, {"heard": "b", "correct": "B" * 200}]


def test_settings_migration_adds_new_hotkeys(tmp_path, monkeypatch):
    import json

    from vrp.settings import Settings
    s = Settings()
    path = s.path
    old = {"general.settings_rev": 1, "hotkeys.bindings": {"record": {"keys": "Ctrl+Alt+K", "scope": "global"}}}
    path.write_text(json.dumps({"schema_version": 1, "settings": old}), encoding="utf-8")
    def loaded():
        x = Settings()
        x.load()
        return x
    b = loaded().get("hotkeys.bindings")
    assert b["bookmark"]["keys"] == ""                 # Ctrl+Alt+K was already taken by another action
    assert b["caption_bar"]["keys"] == "" and b["record"]["keys"] == "Ctrl+Alt+K"
    old["hotkeys.bindings"] = {"record": {"keys": "Ctrl+Alt+R", "scope": "global"}}
    path.write_text(json.dumps({"schema_version": 1, "settings": old}), encoding="utf-8")
    assert loaded().get("hotkeys.bindings")["bookmark"]["keys"] == "Ctrl+Alt+K"


# ------------------------------------------------------------------ colours
THEMES = {False: ("#ffffff", "#f0f0f0"), True: ("#1e1e1e", "#202020")}       # text area, window


@pytest.mark.parametrize("dark", [False, True])
def test_text_contrast(dark):
    from vrp.ui.transcript_panel import colours
    c = colours(dark)
    base, window = THEMES[dark]
    assert c["base"] == base
    pairs = [(c["text"], base), (c["muted"], base), (c["others"], base), (c["accent"], base),
             (c["me_fg"], c["me_bg"]), (c["muted"], c["bm_bg"]), (c["text"], c["bm_bg"]),
             (c["accent"], c["bm_bg"]), (c["muted"], c["off_bg"]), (c["strip_fg"], c["strip_bg"]),
             (c["muted"], window), (c["accent"], window), (c["text"], window)]
    for fg, bg in pairs:
        assert contrast(fg, bg) >= 4.5, (fg, bg, round(contrast(fg, bg), 2))
    cap = colours(True)                                     # the caption bar is always dark (#202020)
    for fg in ("#ffffff", "#c8c8c8", cap["muted"], cap["others"]):
        assert contrast(fg, "#202020") >= 4.5
    assert contrast(cap["me_fg"], cap["me_bg"]) >= 4.5 and contrast("#ffffff", "#3d3d3d") >= 4.5


@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("kind", ["deutan", "protan"])
def test_colour_pairs_that_carry_meaning(dark, kind):
    from vrp.ui.transcript_panel import colours
    from vrp.ui.widgets import meter_colours
    c = colours(dark)
    assert cvd_distance(c["me_bg"], c["others"], kind) >= 40           # speakers (also filled vs plain)
    m = meter_colours(dark)
    # regression: below / above the Vox threshold were blue / magenta, 39 (deutan) and 26 (protan) apart
    assert cvd_distance(m["below"], m["above"], kind) >= 40


def test_speaker_tags_differ_in_shape():
    from vrp.transcribe.transcript import Entry
    from vrp.ui.transcript_panel import colours, entry_html
    c = colours(False)
    me = entry_html(Entry("text", 0, 1, speaker="me", text="<hi>"), c)
    others = entry_html(Entry("text", 0, 1, speaker="others", text="x"), c)
    assert "background-color:#0067c0" in me and "&lt;hi&gt;" in me              # filled tag, text escaped
    assert "background-color" not in others.split("Others")[0].split("<span")[-1]  # plain bold text
