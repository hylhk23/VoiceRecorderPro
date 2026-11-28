"""Screenshot of Voice Recorder Pro in its simple, recorder-only window, for the README and website.

Run it from the program folder (the one with run.bat):

    python\\pythonw.exe tools\\screenshot_recorder.py

Like screenshot_both_panels.py it starts a separate, temporary copy of the program and never touches
your own copy: settings, log and test recording in a new folder in %TEMP% (VRP-screenshot-...),
synthetic microphone and computer sound (--fake-audio), no hot-keys, no hand-over to a running copy,
no speech engine. The window is drawn twice as large (QT_SCALE_FACTOR=2), so the picture stays sharp.

Two pictures are taken, each at a few moments:
- "recorder": the live transcript off, the window as in 1.0 with the View and Transcript menus;
- "recorder-lt": the live transcript on in the Recorder layout, with the layout switch and one line
  of the newest words (invented demo text).
It saves PNG files in a "screenshots" folder next to run.bat and closes by itself.
"""
import os
import sys
import time

os.environ.setdefault("QT_SCALE_FACTOR", "2")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import screenshot_both_panels as base          # noqa: E402  (sets VRP_CONFIG_DIR and the program path first)

OUT, TMP = base.OUT, base.TMP


def main() -> int:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication

    import vrp.app as A
    import vrp.core.engine as E
    import vrp.ipc
    from vrp.inputs.hotkeys import HotkeyManager
    from vrp.settings import Settings
    from vrp.transcribe import pack as lt_pack
    from vrp.transcribe.transcript import Entry

    vrp.ipc.Server = base.NoServer
    HotkeyManager.apply = lambda self, *a, **k: None
    E.WorkerHandle = base.DemoWorker
    real_status = E.Engine.lt_pack_status

    def status(self, refresh=False):
        st = real_status(self, refresh)
        return st if st.ok else lt_pack.PackStatus(True, "", "demo", "m", "t", "v", "1.1", "SenseVoiceSmall int8")
    E.Engine.lt_pack_status = status

    app = QApplication(sys.argv[:1])
    s = Settings()
    s.update({"rec.folder": os.path.join(TMP, "rec"), "rec.fallback_folder": os.path.join(TMP, "fallback"),
              "general.first_run_done": True, "lt.enabled": False, "ui.layout": "recorder",
              "rec.src_mic": True, "rec.src_system": True})
    c = A.AppController(app, A._gui_args(["--fake-audio"]))
    w, e, sv = c.window, c.engine, c.engine.transcriber
    w.setWindowFlag(Qt.WindowStaysOnTopHint, True)

    def pump(sec):
        t0 = time.time()
        while time.time() - t0 < sec:
            app.processEvents()
            time.sleep(0.01)

    def place():
        w.adjustSize()
        width = max(w.width(), w.menuBar().sizeHint().width() + 8)      # every menu visible, no » button
        w.resize(width, w.height())
        scr = QGuiApplication.primaryScreen().availableGeometry()
        w.move(scr.x() + (scr.width() - w.width()) // 2, scr.y() + max(0, (scr.height() - w.height()) // 2))
        w.show()
        w.raise_()
        w.activateWindow()
        pump(0.5)

    os.makedirs(OUT, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    notes = []

    def shots(name, n=6):
        for i in range(1, n + 1):           # several moments: the synthetic sound comes in bursts
            pump(0.7)
            w.raise_()
            fg = w.frameGeometry()
            screen = w.screen() or QGuiApplication.primaryScreen()
            pm = screen.grabWindow(0, fg.x(), fg.y(), fg.width(), fg.height())
            path = os.path.join(OUT, f"{name}-{stamp}-{i}.png")
            pm.save(path)
            notes.append(f"{path} {pm.width()}x{pm.height()}")
            w.grab().save(os.path.join(OUT, f"{name}-{stamp}-{i}-inside.png"))    # without the title bar
        notes.append(f"{name}: screen {screen.name()} dpr {screen.devicePixelRatio()} window {w.width()}x{w.height()}"
                     f" menu bar hint {w.menuBar().sizeHint().width()}")

    place()
    e.record()
    pump(1.5)
    shots("recorder")

    e.stop()                                 # the live transcript starts with the next recording
    t0 = time.time()
    while time.time() - t0 < 10 and e.state.value != "stopped":
        pump(0.1)
    c.settings.set("lt.enabled", True)
    pump(0.5)
    t0 = time.time()
    while time.time() - t0 < 10 and not (sv.worker and sv.worker.ready):
        pump(0.1)
    place()
    e.record()
    pump(1.5)
    sec, who, lang, text = base.LINES[-1]
    sv._add(Entry("text", base.START + sec, base.START + sec + 5, float(sec), float(sec + 5), who, text, lang),
            sv.cur_key)
    sv.partials = {base.PARTIAL[0]: (base.PARTIAL[1], base.START + 71)}
    e.transcriptChanged.emit()
    pump(0.5)
    shots("recorder-lt")

    import vrp
    notes.append(f"version {vrp.__version__}; Vox button checkable: {w.vox_btn.isCheckable()}")
    with open(os.path.join(OUT, f"recorder-{stamp}.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(notes) + "\n")
    e.stop()
    t0 = time.time()
    while time.time() - t0 < 10 and (sv.writers or sv.closing):
        pump(0.1)
    c.quit()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:                      # pythonw has no console: leave the error where it can be read
        import traceback
        os.makedirs(OUT, exist_ok=True)
        with open(os.path.join(OUT, "screenshot-error.txt"), "w", encoding="utf-8") as f:
            traceback.print_exc(file=f)
        sys.exit(1)
