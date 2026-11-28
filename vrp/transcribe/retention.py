"""Files to delete after a number of days: audio kept for checking, and transcripts.

The register is a small JSON file in the settings folder. Only files the program
itself put in the register are ever deleted; nothing else in the recordings folder
is touched. The register is written to a temporary file and then moved into place.
"""
from __future__ import annotations

import json
import logging
import os
import time

from vrp import paths

log = logging.getLogger(__name__)

AUDIO, TEXT = "audio", "text"
DAY = 86400.0


class Retention:
    def __init__(self, path: str):
        self.path = str(path)
        self.items: list[dict] = []
        self.load()

    def load(self) -> None:
        self.items = []
        try:
            with open(paths.longpath(self.path), encoding="utf-8") as f:
                obj = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("retention register %s could not be read (%s); starting a new one", self.path, exc)
            return
        for it in obj.get("items", []) if isinstance(obj, dict) else []:
            if not isinstance(it, dict):
                continue
            p, kind, due = it.get("path"), it.get("kind"), it.get("due")
            if isinstance(p, str) and p and kind in (AUDIO, TEXT) and isinstance(due, (int, float)):
                self.items.append({"path": p, "kind": kind, "due": float(due),
                                   "added": float(it.get("added", 0) or 0)})

    def save(self) -> None:
        tmp = self.path + ".tmp"
        os.makedirs(os.path.dirname(os.path.abspath(self.path)) or ".", exist_ok=True)
        with open(paths.longpath(tmp), "w", encoding="utf-8") as f:
            json.dump({"version": 1, "items": self.items}, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(paths.longpath(tmp), paths.longpath(self.path))

    def add(self, path: str, kind: str, days: float, now: float | None = None) -> None:
        now = time.time() if now is None else now
        key = os.path.normcase(os.path.abspath(path))
        self.items = [it for it in self.items if os.path.normcase(os.path.abspath(it["path"])) != key or
                      it["kind"] != kind]
        self.items.append({"path": os.path.abspath(path), "kind": kind, "due": now + days * DAY, "added": now})
        self.save()

    def find(self, path: str, kind: str | None = None) -> list[dict]:
        key = os.path.normcase(os.path.abspath(path))
        return [it for it in self.items if os.path.normcase(os.path.abspath(it["path"])) == key and
                (kind is None or it["kind"] == kind)]

    def remove(self, path: str) -> None:
        key = os.path.normcase(os.path.abspath(path))
        before = len(self.items)
        self.items = [it for it in self.items if os.path.normcase(os.path.abspath(it["path"])) != key]
        if len(self.items) != before:
            self.save()

    def due(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        return [it for it in self.items if it["due"] <= now]

    def sweep(self, now: float | None = None, busy=lambda _p: False, delete=None) -> list[tuple[str, str]]:
        """Delete the files that are due; returns [(path, '' or the reason it was kept)]."""
        delete = delete or (lambda p: os.remove(paths.longpath(p)))
        done, keep = [], []
        for it in self.items:
            if it["due"] > (time.time() if now is None else now):
                keep.append(it)
                continue
            p = it["path"]
            if busy(p):
                keep.append(it)
                done.append((p, "in use"))
                continue
            try:
                if os.path.exists(paths.longpath(p)):
                    delete(p)
                    log.info("deleted %s (kept for %s, now due)", p, it["kind"])
                done.append((p, ""))
            except OSError as exc:
                keep.append(it)
                done.append((p, str(exc)))
        if len(keep) != len(self.items):
            self.items = keep
            self.save()
        return done
