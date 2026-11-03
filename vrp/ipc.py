"""Single instance and local control channel (FR-CLI-001).

A Qt local server (a named pipe on Windows) per user; never a network socket.
Requests and replies are one line of JSON each.
"""
from __future__ import annotations

import getpass
import json
import logging

from PySide6.QtCore import QObject
from PySide6.QtNetwork import QLocalServer, QLocalSocket

log = logging.getLogger(__name__)


def server_name() -> str:
    try:
        user = getpass.getuser()
    except Exception:
        user = "user"
    return f"VoiceRecorderPro-{user}"


class Server(QObject):
    def __init__(self, handler, parent=None):
        super().__init__(parent)
        self.handler = handler
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.UserAccessOption)
        self.server.newConnection.connect(self._new)
        name = server_name()
        if not self.server.listen(name):
            QLocalServer.removeServer(name)          # stale socket after a crash (non-Windows)
            if not self.server.listen(name):
                log.warning("control channel unavailable: %s", self.server.errorString())

    def _new(self) -> None:
        while self.server.hasPendingConnections():
            sock = self.server.nextPendingConnection()
            sock.setProperty("buf", b"")
            sock.readyRead.connect(lambda s=sock: self._read(s))
            sock.disconnected.connect(sock.deleteLater)

    def _read(self, sock: QLocalSocket) -> None:
        buf = bytes(sock.property("buf") or b"") + bytes(sock.readAll())
        if b"\n" not in buf:
            sock.setProperty("buf", buf)
            return
        line = buf.split(b"\n", 1)[0]
        try:
            req = json.loads(line.decode("utf-8"))
            reply = self.handler(req.get("cmd", ""), req.get("args") or {})
        except Exception as exc:
            log.exception("bad control request")
            reply = {"ok": False, "code": 1, "msg": str(exc)}
        sock.write(json.dumps(reply).encode("utf-8") + b"\n")
        sock.flush()
        sock.waitForBytesWritten(1000)
        sock.disconnectFromServer()

    def close(self) -> None:
        self.server.close()


def send(cmd: str, args: dict | None = None, timeout_ms: int = 2000) -> dict | None:
    """Send a command to the running instance; None when no instance is running."""
    sock = QLocalSocket()
    sock.connectToServer(server_name())
    if not sock.waitForConnected(500):
        return None
    sock.write(json.dumps({"cmd": cmd, "args": args or {}}).encode("utf-8") + b"\n")
    sock.flush()
    sock.waitForBytesWritten(timeout_ms)
    buf = b""
    while b"\n" not in buf:
        if not sock.waitForReadyRead(timeout_ms):
            break
        buf += bytes(sock.readAll())
    sock.disconnectFromServer()
    if not buf:
        return {"ok": False, "code": 5, "msg": "The running instance did not answer."}
    try:
        return json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
    except ValueError:
        return {"ok": False, "code": 5, "msg": "Bad answer from the running instance."}
