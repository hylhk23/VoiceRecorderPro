"""Commands received over the control channel (shared by the GUI and headless mode)."""
from __future__ import annotations

import os

from vrp.cli import BUSY, DEVICE, FILE_ERR, OK, USAGE, schedule_command
from vrp.core.engine import CAPTURE, State


def handle(engine, settings, cmd: str, args: dict, ui=None) -> dict:
    def ok(msg: str = "", data: dict | None = None) -> dict:
        return {"ok": True, "code": OK, "msg": msg, "data": data or {}}

    def fail(code: int, msg: str) -> dict:
        return {"ok": False, "code": code, "msg": msg}

    if engine.busy and cmd in ("record", "play"):
        return fail(BUSY, "An edit is in progress.")
    if cmd == "record":
        if engine.capturing:
            return fail(BUSY, "Already recording.")
        if not engine.record_with(args):
            return fail(DEVICE, "Recording could not start (see the app or log for details).")
        return ok(f"Recording to {engine.settings.recordings_folder() if not args.get('out') else args['out']}")
    if cmd == "pause":
        if engine.state in (State.ARMED, State.RECORDING):
            engine.record()
            return ok("Paused.")
        return fail(BUSY, "Not recording.")
    if cmd == "resume":
        if engine.state == State.REC_PAUSED:
            engine.record()
            return ok("Resumed.")
        return fail(BUSY, "Recording is not paused.")
    if cmd == "stop":
        if engine.state in CAPTURE:
            path = engine.file.path if engine.file else ""
            engine.stop()
            return ok(f"Stopped. {path}")
        engine.stop()
        return ok("Stopped.")
    if cmd == "play":
        f = args.get("file")
        if f:
            if not os.path.exists(f):
                return fail(FILE_ERR, f"{f} does not exist.")
            if not engine.open_file(f):
                return fail(FILE_ERR, f"{f} could not be opened.")
        if engine.file is None:
            return fail(FILE_ERR, "No file is open.")
        engine.seek(float(args.get("from", 0)) * engine.file.rate)
        if engine.state != State.PLAYING:
            engine.play_pause()
        return ok("Playing.")
    if cmd == "status":
        return ok(data=engine.status_dict())
    if cmd == "show":
        if ui is not None:
            ui.show_window()
        return ok()
    if cmd.startswith("schedule_"):
        return schedule_command(settings, cmd[len("schedule_"):], args)
    return fail(USAGE, f"Unknown command: {cmd}")
