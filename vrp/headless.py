"""Headless recording: no window, no tray (FR-CLI-004).

Every recording feature works (Vox, rollover, schedules from the settings
file). Ctrl+C or SIGTERM stops and finalises the file before exiting.
"""
from __future__ import annotations

import logging
import signal
import sys

from vrp import logsetup
from vrp.cli import DEVICE, OK


def run_headless(args: dict, fake: bool = False) -> int:
    from PySide6.QtCore import QCoreApplication, QTimer

    from vrp import control, ipc
    from vrp.core.audio_backend import make_backend
    from vrp.core.engine import CAPTURE, Engine
    from vrp.settings import Settings

    app = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])
    settings = Settings()
    settings.load()
    logsetup.setup(settings.get("adv.log_level"), settings.get("adv.log_days"), console=True)
    log = logging.getLogger("vrp.headless")
    try:
        backend = make_backend(fake)
    except OSError as exc:
        print(f"vrp: sound system unavailable: {exc}", file=sys.stderr)
        return DEVICE
    engine = Engine(settings, backend)
    engine.message.connect(lambda level, title, text: print(f"{level.upper()}: {title}: {text}", file=sys.stderr))
    engine.notify.connect(lambda title, text: print(f"{title}: {text}", file=sys.stderr))
    server = ipc.Server(lambda cmd, a: control.handle(engine, settings, cmd, a))
    engine.start()
    if not engine.record_with(args):
        engine.shutdown()
        return DEVICE
    print(f"Recording ({engine.fmt().describe()}). Press Ctrl+C to stop.", file=sys.stderr)
    state = {"stopping": False, "code": OK}

    def request_stop(*_a):
        if not state["stopping"]:
            state["stopping"] = True
            print("Stopping and saving…", file=sys.stderr)
            engine.stop()

    signal.signal(signal.SIGINT, request_stop)
    try:
        signal.signal(signal.SIGTERM, request_stop)
    except (AttributeError, ValueError):
        pass
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, request_stop)

    last_file = [None]

    def watch():
        f = engine.file
        if f is not None and f.path != last_file[0]:
            last_file[0] = f.path
            print(f"File: {f.path}", file=sys.stderr)
        if engine.state not in CAPTURE:
            log.info("headless recording finished")
            if f is not None:
                print(f.path)
            server.close()
            engine.shutdown()
            app.quit()

    timer = QTimer()
    timer.setInterval(200)          # also lets Python handle Ctrl+C promptly
    timer.timeout.connect(watch)
    timer.start()
    app.exec()
    return state["code"]
