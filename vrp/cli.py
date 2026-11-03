"""Command line (Section 16).

    vrp                         start the GUI (or bring the running one to the front)
    vrp record [options]        start recording in the running instance (starting it if needed)
    vrp pause | resume | stop   control the current recording
    vrp play FILE [--from T]    open and play a file
    vrp status [--json]         state, file, segment, duration, size, level, free disk
    vrp devices [--json]        host APIs and devices
    vrp extract FILE --from T --to T [--out PATH]
    vrp split FILE --at T
    vrp join FILE1 FILE2 ... --out PATH
    vrp convert FILE --format flac [--rate 44100] [--channels 1] [--out PATH]
    vrp schedule list | add --name N --start ISO --duration T [--repeat daily] | remove ID
    vrp record --headless ...   record without window or tray; Ctrl+C finalises the file

Exit codes: 0 ok, 1 usage error, 2 device error, 3 file error, 4 no running instance, 5 busy.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

from vrp import APP_NAME, __version__

OK, USAGE, DEVICE, FILE_ERR, NO_INSTANCE, BUSY = 0, 1, 2, 3, 4, 5
STATUS_SCHEMA = 1
COMMANDS = {"record", "pause", "resume", "stop", "play", "status", "devices", "extract", "split", "join",
            "convert", "schedule", "show"}


class UsageError(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


def build_parser() -> argparse.ArgumentParser:
    p = Parser(prog="vrp", description=f"{APP_NAME} {__version__} command line",
               formatter_class=argparse.RawDescriptionHelpFormatter,
               epilog="Times accept ss, m:ss, h:mm:ss or decimal seconds.\n"
                      "Exit codes: 0 ok, 1 usage, 2 device, 3 file, 4 no instance, 5 busy.")
    p.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    sub = p.add_subparsers(dest="cmd")
    r = sub.add_parser("record", help="start recording")
    r.add_argument("--vox", choices=["on", "off"])
    r.add_argument("--duration", help="stop after this long (h:mm:ss)")
    r.add_argument("--device", help="input device name")
    r.add_argument("--format", choices=["mp3", "wav", "flac"])
    r.add_argument("--rate", type=int)
    r.add_argument("--channels", type=int, choices=[1, 2])
    g = r.add_mutually_exclusive_group()
    g.add_argument("--out", help="output file")
    g.add_argument("--pattern", help="file-name pattern")
    r.add_argument("--headless", action="store_true", help="record without a window")
    r.add_argument("--fake-audio", action="store_true", help=argparse.SUPPRESS)
    for name, h in (("pause", "pause recording"), ("resume", "resume recording"), ("stop", "stop and save"),
                    ("show", "show the main window")):
        sub.add_parser(name, help=h)
    pl = sub.add_parser("play", help="open and play a file")
    pl.add_argument("file")
    pl.add_argument("--from", dest="start", default="0")
    st = sub.add_parser("status", help="show the current state")
    st.add_argument("--json", action="store_true")
    dv = sub.add_parser("devices", help="list sound devices")
    dv.add_argument("--json", action="store_true")
    ex = sub.add_parser("extract", help="copy a range to a new file")
    ex.add_argument("file")
    ex.add_argument("--from", dest="start", required=True)
    ex.add_argument("--to", dest="end", required=True)
    ex.add_argument("--out")
    sp = sub.add_parser("split", help="split a file in two")
    sp.add_argument("file")
    sp.add_argument("--at", required=True)
    jn = sub.add_parser("join", help="join files")
    jn.add_argument("files", nargs="+")
    jn.add_argument("--out", required=True)
    jn.add_argument("--convert", action="store_true", help="convert files of other formats")
    cv = sub.add_parser("convert", help="convert a file")
    cv.add_argument("file")
    cv.add_argument("--format", required=True, choices=["mp3", "wav", "flac"])
    cv.add_argument("--rate", type=int)
    cv.add_argument("--channels", type=int, choices=[1, 2])
    cv.add_argument("--kbps", type=int)
    cv.add_argument("--out")
    sc = sub.add_parser("schedule", help="manage scheduled recordings")
    scs = sc.add_subparsers(dest="action")
    scs.add_parser("list")
    sa = scs.add_parser("add")
    sa.add_argument("--name", required=True)
    sa.add_argument("--start", required=True, help="YYYY-MM-DDTHH:MM (local time)")
    sa.add_argument("--duration", required=True)
    sa.add_argument("--repeat", default="once", choices=["once", "daily", "weekdays", "weekly", "monthly"])
    sa.add_argument("--days", default="", help="weekly: comma-separated 0-6 (0 = Monday)")
    sa.add_argument("--vox", default="current", choices=["current", "on", "off"])
    srm = scs.add_parser("remove")
    srm.add_argument("id")
    return p


def wants_cli(argv: list[str]) -> bool:
    return any(a in COMMANDS for a in argv[:1]) or any(a in ("-h", "--help", "--version") for a in argv[:1])


def _print(obj, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, default=str))
    else:
        for k, v in obj.items():
            print(f"{k}: {v}")


def _progress(label: str):
    last = [-1]

    def cb(f: float) -> None:
        pct = int(f * 100)
        if pct != last[0] and pct % 10 == 0:
            last[0] = pct
            print(f"{label}: {pct}%", file=sys.stderr)
    return cb


def record_args(ns) -> dict:
    from vrp.core.model import parse_time
    args = {k: getattr(ns, k) for k in ("vox", "device", "format", "rate", "channels", "out", "pattern")
            if getattr(ns, k, None) is not None}
    if ns.duration:
        args["duration"] = parse_time(ns.duration)
    return args


def launch_gui(startup: dict | None) -> None:
    """Start the GUI detached, passing a command to run once it is up."""
    from vrp.winutil import gui_command
    cmd = gui_command()
    if startup:
        cmd += " --startup-cmd " + _quote(json.dumps(startup))
    flags = 0
    if sys.platform == "win32":
        flags = 0x00000008 | 0x00000200       # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(cmd, creationflags=flags, close_fds=True)
    else:
        import shlex
        subprocess.Popen(shlex.split(cmd), start_new_session=True, close_fds=True)


def _quote(s: str) -> str:
    if sys.platform == "win32":
        return '"' + s.replace('"', '\\"') + '"'
    import shlex
    return shlex.quote(s)


def run(argv: list[str]) -> int:
    try:
        ns = build_parser().parse_args(argv)
    except UsageError as exc:
        print(f"vrp: {exc}\nRun 'vrp --help' for usage.", file=sys.stderr)
        return USAGE
    except SystemExit as exc:            # --help / --version
        return int(exc.code or 0)
    try:
        return _dispatch(ns)
    except UsageError as exc:
        print(f"vrp: {exc}", file=sys.stderr)
        return USAGE
    except KeyboardInterrupt:
        return OK


def _dispatch(ns) -> int:
    from PySide6.QtCore import QCoreApplication
    if QCoreApplication.instance() is None:       # needed by QLocalSocket
        _dispatch.app = QCoreApplication(sys.argv[:1])
    from vrp import ipc
    from vrp.core.model import parse_time

    cmd = ns.cmd
    if cmd == "record" and ns.headless:
        from vrp.headless import run_headless
        return run_headless(record_args(ns), fake=ns.fake_audio)
    if cmd in ("record", "pause", "resume", "stop", "status", "play", "show"):
        args = {}
        if cmd == "record":
            args = record_args(ns)
        elif cmd == "play":
            args = {"file": os.path.abspath(ns.file), "from": parse_time(ns.start)}
            if not os.path.exists(ns.file):
                print(f"vrp: {ns.file} does not exist", file=sys.stderr)
                return FILE_ERR
        reply = ipc.send(cmd, args)
        if reply is None:
            if cmd in ("record", "play", "show"):
                launch_gui({"cmd": cmd, "args": args} if cmd != "show" else None)
                print(f"Started {APP_NAME}.")
                return OK
            print(f"vrp: {APP_NAME} is not running.", file=sys.stderr)
            return NO_INSTANCE
        if cmd == "status":
            data = reply.get("data", {})
            data["schema"] = STATUS_SCHEMA
            _print(data, ns.json)
        elif reply.get("msg"):
            print(reply["msg"], file=sys.stderr if not reply.get("ok") else sys.stdout)
        return int(reply.get("code", 0 if reply.get("ok") else BUSY))
    if cmd == "devices":
        return _devices(ns.json)
    if cmd in ("extract", "split", "join", "convert"):
        return _file_op(ns)
    if cmd == "schedule":
        return _schedule(ns)
    build_parser().print_help()
    return USAGE


def _devices(as_json: bool) -> int:
    try:
        from vrp.core.audio_backend import SoundDeviceBackend
        be = SoundDeviceBackend()
    except Exception as exc:
        print(f"vrp: sound system unavailable: {exc}", file=sys.stderr)
        return DEVICE
    out = {"default_host_api": be.default_host_api(), "host_apis": []}
    for api in be.host_apis():
        entry = {"name": api, "inputs": [], "outputs": []}
        for kind, key in (("input", "inputs"), ("output", "outputs")):
            for d in be.devices(kind, api):
                entry[key].append({"name": d.name, "channels": d.max_in if kind == "input" else d.max_out,
                                   "default_rate": d.default_rate,
                                   "default": d.is_default_in if kind == "input" else d.is_default_out})
        out["host_apis"].append(entry)
    if as_json:
        print(json.dumps({"schema": 1, **out}, indent=2))
    else:
        for api in out["host_apis"]:
            print(f"{api['name']}{' (default)' if api['name'] == out['default_host_api'] else ''}")
            for kind in ("inputs", "outputs"):
                for d in api[kind]:
                    star = "*" if d["default"] else " "
                    print(f"  {star} {kind[:-1]:6} {d['name']}  ({d['channels']} ch, {d['default_rate']:g} Hz)")
    return OK


def _file_op(ns) -> int:
    from vrp.core import editor
    from vrp.core.audiofile import UnsupportedFile, probe
    from vrp.core.model import parse_time

    try:
        if ns.cmd == "join":
            m = editor.join([os.path.abspath(f) for f in ns.files], os.path.abspath(ns.out), ns.convert,
                            _progress("join"))
            print(m.path)
            return OK
        model = probe(os.path.abspath(ns.file))
        if ns.cmd == "extract":
            a, b = int(parse_time(ns.start) * model.rate), int(parse_time(ns.end) * model.rate)
            if b <= a:
                raise UsageError("--to must be after --from")
            m = editor.extract(model, a, b, os.path.abspath(ns.out) if ns.out else None, None, _progress("extract"))
            print(m.path)
        elif ns.cmd == "split":
            p1, p2 = editor.split(model, int(parse_time(ns.at) * model.rate), False, _progress("split"))
            print(p1)
            print(p2)
        elif ns.cmd == "convert":
            fmt = editor.format_of(model)
            fmt.codec = ns.format
            if ns.rate:
                fmt.rate = ns.rate
            if ns.channels:
                fmt.channels = ns.channels
            if ns.kbps:
                fmt.mp3_kbps = ns.kbps
            fmt = fmt.normalised()
            out = ns.out or os.path.splitext(model.path)[0] + fmt.ext
            if os.path.abspath(out) == os.path.abspath(model.path):
                out = os.path.splitext(model.path)[0] + "_converted" + fmt.ext
            m = editor.convert(model, editor.unique_path(os.path.abspath(out)), fmt, _progress("convert"))
            print(m.path)
        return OK
    except (UnsupportedFile, editor.EditError, OSError, ValueError) as exc:
        print(f"vrp: {exc}", file=sys.stderr)
        return FILE_ERR


def _schedule(ns) -> int:
    from vrp import ipc
    from vrp.core import scheduler
    from vrp.core.model import parse_time

    action = ns.action or "list"
    args = {}
    if action == "add":
        try:
            start = __import__("datetime").datetime.fromisoformat(ns.start)
        except ValueError:
            raise UsageError("--start must look like 2026-10-20T09:00")
        days = [int(d) for d in ns.days.split(",") if d.strip()] if ns.days else []
        e = scheduler.Entry(name=ns.name, start=start.isoformat(timespec="minutes"),
                            duration_s=int(parse_time(ns.duration)), repeat=ns.repeat, days=days, vox=ns.vox)
        args = {"entry": e.to_json()}
    elif action == "remove":
        args = {"id": ns.id}
    reply = ipc.send("schedule_" + action, args)
    if reply is None:
        # no instance: edit the settings file directly
        from vrp.settings import Settings
        s = Settings()
        s.load()
        reply = schedule_command(s, action, args)
    if action == "list":
        for e in reply.get("data", {}).get("entries", []):
            print(f"{e['id']}  {'on ' if e['enabled'] else 'off'}  {e['start']}  {e['describe']}  {e['name']}")
    elif reply.get("msg"):
        print(reply["msg"], file=sys.stderr if not reply.get("ok") else sys.stdout)
    return int(reply.get("code", 0))


def schedule_command(settings, action: str, args: dict) -> dict:
    from vrp.core import scheduler
    entries = [scheduler.Entry.from_json(d) for d in settings.get("sched.entries")]
    if action == "list":
        return {"ok": True, "code": OK, "data": {"entries": [{**e.to_json(), "describe": e.describe()}
                                                             for e in entries]}}
    if action == "add":
        e = scheduler.Entry.from_json(args["entry"])
        clash = scheduler.find_overlap(entries, e)
        if clash:
            return {"ok": False, "code": BUSY, "msg": f"Overlaps schedule '{clash.name}' ({clash.id})."}
        entries.append(e)
        settings.set("sched.entries", [x.to_json() for x in entries])
        return {"ok": True, "code": OK, "msg": f"Added {e.id}"}
    if action == "remove":
        keep = [x for x in entries if x.id != args.get("id")]
        if len(keep) == len(entries):
            return {"ok": False, "code": USAGE, "msg": "No schedule with that id."}
        settings.set("sched.entries", [x.to_json() for x in keep])
        return {"ok": True, "code": OK, "msg": "Removed."}
    return {"ok": False, "code": USAGE, "msg": "Unknown schedule action."}
