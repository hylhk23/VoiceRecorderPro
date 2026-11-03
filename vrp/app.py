"""Application entry point: GUI start-up, single instance, wiring (Section 19)."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import traceback

from vrp import APP_ID, APP_NAME, ORG_NAME, __version__, logsetup, paths, winutil

log = logging.getLogger("vrp")


def _gui_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="VoiceRecorderPro", add_help=True)
    p.add_argument("--minimised", "--minimized", action="store_true", help="start in the tray")
    p.add_argument("--fake-audio", action="store_true", help="use a synthetic microphone (demo/testing)")
    p.add_argument("--startup-cmd", default="", help=argparse.SUPPRESS)
    p.add_argument("--cwd", default="", help=argparse.SUPPRESS)
    p.add_argument("files", nargs="*", help="audio file to open")
    ns, _unknown = p.parse_known_args(argv)
    return ns


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    from vrp import cli
    if cli.wants_cli(argv):
        return cli.run(argv)
    return run_gui(_gui_args(argv))


class AppController:
    """Owns the engine, window, hot-keys, external devices and control channel."""

    def __init__(self, app, args):
        from PySide6.QtCore import QTimer

        from vrp import control, ipc
        from vrp.core.audio_backend import make_backend
        from vrp.core.engine import Engine
        from vrp.inputs.external import ExternalManager
        from vrp.inputs.hotkeys import HotkeyManager
        from vrp.settings import Settings
        from vrp.ui.main_window import MainWindow, apply_theme

        self.app = app
        self.settings = Settings()
        self.settings.load()
        logsetup.setup(self.settings.get("adv.log_level"), self.settings.get("adv.log_days"))
        apply_theme(app, self.settings.get("general.theme"))
        self.backend = make_backend(args.fake_audio)
        self.engine = Engine(self.settings, self.backend)
        self.hotkeys = HotkeyManager()
        self.ext = ExternalManager()
        self.window = MainWindow(self)
        self._control = control
        self.server = ipc.Server(lambda cmd, a: control.handle(self.engine, self.settings, cmd, a, self.window))
        self.hotkeys.triggered.connect(self.dispatch)
        self.hotkeys.statusChanged.connect(self._hotkey_status)
        self.ext.actionEvent.connect(self.dispatch)
        self.ext.deviceStatus.connect(self._ext_status)
        self.engine.uiAction.connect(self._ui_action)
        self.engine.stateChanged.connect(self._state_out)
        self.settings.add_listener(self._settings_changed)
        self._apply_inputs()
        app.commitDataRequest.connect(lambda _m: self.engine.shutdown(2.0))   # log-off / shutdown
        app.aboutToQuit.connect(self._about_to_quit)
        self.engine.start()
        self._quitting = False
        QTimer.singleShot(0, lambda: self._after_show(args))

    # ------------------------------------------------------------- wiring
    def dispatch(self, action: str, pressed: bool) -> None:
        if action in ("show_hide", "suspend_hotkeys"):
            if pressed:
                self._ui_action(action)
            return
        self.engine.do(action, pressed)

    def _ui_action(self, action: str) -> None:
        if action == "show_hide":
            self.window.toggle_window()
        elif action == "suspend_hotkeys":
            self.toggle_suspend()
        elif action == "mic_privacy":
            pass

    def toggle_suspend(self) -> None:
        on = not self.hotkeys.suspended
        self.hotkeys.set_suspended(on)
        if self.settings.get("ext.suspend_with_hotkeys"):
            self.ext.set_suspended(on)
        self.window.tray_suspend.setChecked(on)
        self.window.flash("Hot-keys suspended" if on else "Hot-keys active")

    def _hotkey_status(self) -> None:
        bad = {a: s for a, s in self.hotkeys.status.items() if s}
        if bad and self.hotkeys.supported:
            names = ", ".join(sorted(bad))
            self.window.flash(f"⚠ Hot-key not available: {names}", 8)
            log.warning("hot-keys not registered: %s", bad)

    def _ext_status(self, key: str, connected: bool, msg: str) -> None:
        if not connected and msg == "Disconnected":
            self.window.tray.showMessage(APP_NAME, f"External device disconnected: {key}")

    def _state_out(self, state: str) -> None:
        mapping = {"recording": "recording", "armed": "armed", "playing": "playing", "seeking": "playing"}
        self.ext.set_app_state(mapping.get(state, ""))

    def _apply_inputs(self) -> None:
        s = self.settings
        self.hotkeys.apply(s.get("hotkeys.bindings"), s.get("hotkeys.media_keys"))
        self.ext.configure(s.get("ext.mappings"), s.get("ext.outputs"), s.get("ext.on_loss"), s.get("ext.enabled"))

    def _settings_changed(self, keys: set) -> None:
        if any(k.startswith(("hotkeys.", "ext.")) for k in keys):
            self._apply_inputs()
        if "general.theme" in keys:
            from vrp.ui.main_window import apply_theme
            apply_theme(self.app, self.settings.get("general.theme"))
        if "adv.log_level" in keys:
            logging.getLogger().setLevel(self.settings.get("adv.log_level"))

    def open_options(self, tab: str | None) -> None:
        from vrp.ui.options_dialog import OptionsDialog
        self.window.show_window()
        d = OptionsDialog(self.settings, self.engine, self.hotkeys, self.ext, self.backend, tab, self.window)
        d.exec()
        self.window.refresh_all()

    # ------------------------------------------------------------ start-up
    def _after_show(self, args) -> None:
        from PySide6.QtWidgets import QMessageBox

        s = self.settings
        for msg in s.load_messages:
            QMessageBox.warning(self.window, "Settings", msg)
        if not s.get("general.first_run_done"):
            QMessageBox.information(
                self.window, f"Welcome to {APP_NAME}",
                "Recordings are saved in Documents\\VRP Recordings. Press Record (or R) to start.\n\n"
                "Please note: recording phone calls, meetings or conversations may require the consent of "
                "everyone taking part where you live. You are responsible for following those rules.")
            s.set("general.first_run_done", True)
        self._offer_recovery()
        for f in args.files:
            if os.path.exists(f):
                self.engine.open_file(os.path.abspath(f))
                break
        if args.startup_cmd:
            try:
                req = json.loads(args.startup_cmd)
                self._control.handle(self.engine, self.settings, req.get("cmd", ""), req.get("args") or {},
                                     self.window)
            except Exception:
                log.exception("startup command failed")

    def _offer_recovery(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        from vrp.core import journal
        from vrp.ui.dialogs import RecoveryDialog
        pending = self.engine.pending_recoveries()
        if not pending:
            return
        d = RecoveryDialog(pending, self.window)
        r = d.exec()
        if r == 1:
            results = [journal.recover(p) for p in d.selected()]
            text = "\n".join(m for _ok, m in results)
            QMessageBox.information(self.window, "Recovery", text or "Nothing was recovered.")
        elif r == 2:
            for p in d.selected():
                journal.discard(p.journal)

    # ------------------------------------------------------------ shutdown
    def quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        self.engine.shutdown()
        self.hotkeys.stop()
        self.ext.stop()
        self.server.close()
        self.window.tray.hide()
        self.settings.save()
        self.app.quit()

    def _about_to_quit(self) -> None:
        if not self._quitting:
            self.quit()


def _install_excepthooks(app_ref) -> None:
    def hook(exc_type, exc, tb):
        log.critical("unhandled exception", exc_info=(exc_type, exc, tb))
        try:
            from PySide6.QtWidgets import QMessageBox
            box = QMessageBox(QMessageBox.Critical, APP_NAME,
                              f"Something went wrong: {exc}\n\nDetails were written to the log.",
                              QMessageBox.Ok)
            btn = box.addButton("Open log folder", QMessageBox.ActionRole)
            btn.clicked.connect(lambda: winutil.open_folder(paths.log_dir()))
            box.exec()
        except Exception:
            traceback.print_exception(exc_type, exc, tb)

    def thread_hook(args):
        log.critical("unhandled exception in thread %s", args.thread.name if args.thread else "?",
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    sys.excepthook = hook
    threading.excepthook = thread_hook


def run_gui(args) -> int:
    if args.cwd and os.path.isdir(args.cwd):
        os.chdir(args.cwd)
    winutil.set_app_user_model_id(f"{ORG_NAME}.{APP_ID}")
    from PySide6.QtCore import QCoreApplication, Qt
    from PySide6.QtWidgets import QApplication, QMessageBox

    from vrp import ipc

    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORG_NAME)
    app.setApplicationVersion(__version__)
    app.setQuitOnLastWindowClosed(False)
    from vrp.ui import icons
    app.setWindowIcon(icons.app_icon())
    # single instance: hand over to the running one (FR-CLI-001)
    startup = json.loads(args.startup_cmd) if args.startup_cmd else None
    if startup is None and args.files:
        startup = {"cmd": "play", "args": {"file": os.path.abspath(args.files[0]), "from": 0}}
    if startup is None:
        # another version still running (e.g. in the tray after an update): say so instead of
        # silently showing the old window
        st = ipc.send("status", {})
        running = ((st or {}).get("data") or {}).get("version") if st and st.get("ok") else None
        if st is not None and st.get("ok") and running != __version__:
            ipc.send("show", {})
            QMessageBox.information(
                None, APP_NAME,
                f"{APP_NAME} {running or '(an older version)'} is still running, so version {__version__} "
                "cannot start.\n\nExit the running copy: File > Exit, or right-click its icon in the "
                "notification area and choose Exit. Then start Voice Recorder Pro again.")
            return 0
    reply = ipc.send(startup["cmd"] if startup else "show", startup["args"] if startup else {})
    if reply is not None:
        return 0
    _install_excepthooks(app)
    try:
        ctl = AppController(app, args)
    except OSError as exc:
        logsetup.setup()
        log.exception("start-up failed")
        QMessageBox.critical(None, APP_NAME, f"The sound system could not be started:\n{exc}")
        return 2
    start_min = args.minimised or ctl.settings.get("general.start_minimised")
    if start_min and ctl.window.tray.isSystemTrayAvailable():
        ctl.engine.window_visible = False
    else:
        ctl.window.show()
    rc = app.exec()
    log.info("exit %s", rc)
    return rc
