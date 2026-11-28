"""Screenshot of Voice Recorder Pro with both panels: the recorder and the live transcript.

For the README and the website. Run it from the program folder (the one with run.bat):

    python\\pythonw.exe tools\\screenshot_both_panels.py

It starts a separate, temporary copy of the program and never touches your own copy:
- its settings, log and test recording go to a new folder in %TEMP% (VRP-screenshot-...);
- the microphone and computer sound are synthetic (the --fake-audio demo backend);
- no hot-keys are registered and it does not talk to a copy that is already running;
- the speech engine is not started: the transcript lines are invented demo text, put
  straight into the transcript, so the picture does not depend on a real meeting.

It saves PNG files in a "screenshots" folder next to run.bat and closes by itself after
about 15 seconds. Keep the window in front until it closes.
"""
import os
import sys
import tempfile
import time
from datetime import datetime

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # the program folder
sys.path.insert(0, HERE)
TMP = tempfile.mkdtemp(prefix="VRP-screenshot-")
os.environ["VRP_CONFIG_DIR"] = os.path.join(TMP, "settings")         # before anything reads settings
OUT = os.path.join(HERE, "screenshots")
SIZE = (1100, 600)               # window size in logical pixels; reduced to fit a small screen

# Invented demo meeting: (seconds after the start, speaker, language, text). No real names.
LINES = [
    (4, "me", "en", "Morning, everyone. Let's start with the office move."),
    (11, "others", "yue", "好呀。新寫字樓下個月一號交樓，冷氣同網絡上個禮拜已經裝好咗。"),
    (24, "me", "en", "Great. Can we move the meeting rooms first, before the open-plan area?"),
    (33, "others", "yue", "可以，不過要先問吓 IT 部，佢哋話伺服器要停機半日。"),
    (47, "me", "zh", "那我們定在星期六搬，避開上班時間。"),
    ("bookmark", 52),
    (55, "others", "en", "OK, Saturday works. I'll send the moving schedule by Friday."),
    (61, "others", "yue", "冇問題，我會連埋平面圖一齊發畀大家。"),
    (66, "me", "en", "Thanks. Please copy me on the quotes from the movers too."),
]
PARTIAL = ("others", "好，搬運公司嘅報價我哋收到兩份")      # words still being spoken
START = datetime.now().replace(hour=10, minute=30, second=0, microsecond=0).timestamp()


class NoServer:
    """Stands in for the single-instance pipe, so a running copy is left alone."""

    def __init__(self, *_a, **_k):
        pass

    def close(self):
        pass


class DemoWorker:
    """Stands in for the speech engine: ready at once, recognises nothing."""

    def __init__(self, cfg):
        self.events = [("ready", 0.1)]
        self.ready = False
        self.started = False

    def start(self):
        self.started = True

    def alive(self):
        return self.started

    def send(self, msg):
        pass

    def poll(self):
        ev, self.events = self.events, []
        if ev:
            self.ready = True
        return ev

    def stop(self):
        self.started = False
        return []


def main() -> int:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication, QPixmap
    from PySide6.QtWidgets import QApplication

    import vrp.app as A
    import vrp.core.engine as E
    import vrp.ipc
    from vrp.inputs.hotkeys import HotkeyManager
    from vrp.settings import Settings
    from vrp.transcribe import pack as lt_pack
    from vrp.transcribe.transcript import Entry

    vrp.ipc.Server = NoServer
    HotkeyManager.apply = lambda self, *a, **k: None
    E.WorkerHandle = DemoWorker
    real_status = E.Engine.lt_pack_status

    def status(self, refresh=False):
        st = real_status(self, refresh)
        return st if st.ok else lt_pack.PackStatus(True, "", "demo", "m", "t", "v", "1.1", "SenseVoiceSmall int8")
    E.Engine.lt_pack_status = status

    app = QApplication(sys.argv[:1])
    app.setApplicationName("Voice Recorder Pro (screenshot)")
    s = Settings()
    s.update({"rec.folder": os.path.join(TMP, "rec"), "rec.fallback_folder": os.path.join(TMP, "fallback"),
              "general.first_run_done": True, "lt.enabled": True, "ui.layout": "both",
              "rec.src_mic": True, "rec.src_system": True, "fmt.channels": 2, "fmt.layout": "split"})
    c = A.AppController(app, A._gui_args(["--fake-audio"]))
    w, e, sv = c.window, c.engine, c.engine.transcriber

    scr = QGuiApplication.primaryScreen().availableGeometry()
    width, height = min(SIZE[0], scr.width() - 40), min(SIZE[1], scr.height() - 80)
    w.setWindowFlag(Qt.WindowStaysOnTopHint, True)
    w.resize(width, height)
    w.move(scr.x() + (scr.width() - width) // 2, scr.y() + (scr.height() - height) // 2)
    w.show()
    w.raise_()
    w.activateWindow()

    def pump(sec):
        t0 = time.time()
        while time.time() - t0 < sec:
            app.processEvents()
            time.sleep(0.01)

    t0 = time.time()
    while time.time() - t0 < 10 and not (sv.worker and sv.worker.ready):
        pump(0.1)
    e.record()
    pump(1.5)
    key = sv.cur_key
    for item in LINES:
        if item[0] == "bookmark":
            at = START + item[1]
            sv._add(Entry("bookmark", at, at, float(item[1]), None), key)
        else:
            sec, who, lang, text = item
            sv._add(Entry("text", START + sec, START + sec + 5, float(sec), float(sec + 5), who, text, lang), key)
    sv.partials = {PARTIAL[0]: (PARTIAL[1], START + 71)}
    e.transcriptChanged.emit()
    w.transcript_panel.refresh()
    pump(1.0)

    os.makedirs(OUT, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    saved = []
    for i in range(1, 7):                  # several moments: the synthetic sound comes in bursts
        pump(0.7)
        w.raise_()
        fg = w.frameGeometry()
        screen = w.screen() or QGuiApplication.primaryScreen()
        pm = screen.grabWindow(0, fg.x(), fg.y(), fg.width(), fg.height())   # with the title bar
        name = os.path.join(OUT, f"both-panels-{stamp}-{i}-window.png")
        pm.save(name)
        saved.append(name)
        hi = QPixmap(w.size() * 2)                                            # the inside, twice as sharp
        hi.setDevicePixelRatio(2)
        hi.fill(w.palette().window().color())
        w.render(hi)
        name = os.path.join(OUT, f"both-panels-{stamp}-{i}-2x.png")
        hi.save(name)
        saved.append(name)
    with open(os.path.join(OUT, f"both-panels-{stamp}.txt"), "w", encoding="utf-8") as f:
        f.write(f"screen {screen.name()} dpr {screen.devicePixelRatio()} window {w.width()}x{w.height()} "
                f"frame {fg.width()}x{fg.height()}\n" + "\n".join(saved) + "\n")
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
