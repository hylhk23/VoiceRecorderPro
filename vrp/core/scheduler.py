"""Scheduled recordings (Section 14): occurrences, overlap checks, due entries.

Times are local wall-clock times (FR-SCHED-007). An entry whose start falls in
the hour skipped by a daylight-saving change starts at the transition; an
occurrence is run at most once, so a repeated hour does not run it twice.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta

REPEATS = {"once": "Once", "daily": "Daily", "weekdays": "Weekdays (Mon-Fri)",
           "weekly": "Weekly on chosen days", "monthly": "Monthly on this day"}
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@dataclass
class Entry:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = "Schedule"
    enabled: bool = True
    start: str = ""                    # local 'YYYY-MM-DDTHH:MM[:SS]'
    duration_s: int = 3600
    repeat: str = "once"
    days: list = field(default_factory=list)     # weekly: 0 = Monday
    vox: str = "current"              # current | on | off
    device: str = ""                  # '' = current input device
    codec: str = ""                   # '' = current format
    pattern: str = ""                 # '' = current naming pattern
    take_over: bool = False           # finalise a manual recording and start this one

    @staticmethod
    def from_json(d: dict) -> "Entry":
        e = Entry()
        for k, v in d.items():
            if hasattr(e, k):
                setattr(e, k, v)
        return e

    def to_json(self) -> dict:
        return asdict(self)

    @property
    def start_dt(self) -> datetime:
        return datetime.fromisoformat(self.start)

    def describe(self) -> str:
        r = REPEATS.get(self.repeat, self.repeat)
        if self.repeat == "weekly" and self.days:
            r = "Weekly " + ", ".join(DAY_NAMES[d] for d in sorted(self.days))
        return f"{r}, {fmt_dur(self.duration_s)}"


def fmt_dur(s: int) -> str:
    h, rem = divmod(int(s), 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}"


def _days_in_month(y: int, m: int) -> int:
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return (nxt - date(y, m, 1)).days


def occurrences(e: Entry, lo: datetime, hi: datetime) -> list[datetime]:
    """Nominal start times of e in [lo - duration, hi)."""
    try:
        s0 = e.start_dt
    except (ValueError, TypeError):
        return []
    lo = lo - timedelta(seconds=max(0, e.duration_s))
    out: list[datetime] = []
    if e.repeat == "once":
        if lo <= s0 < hi:
            out.append(s0)
        return out
    day = max(s0.date(), lo.date())
    end_day = hi.date()
    while day <= end_day:
        dt = datetime.combine(day, s0.time())
        ok = False
        if e.repeat == "daily":
            ok = True
        elif e.repeat == "weekdays":
            ok = day.weekday() < 5
        elif e.repeat == "weekly":
            ok = day.weekday() in (e.days or [s0.weekday()])
        elif e.repeat == "monthly":
            want = min(s0.day, _days_in_month(day.year, day.month))
            ok = day.day == want
        if ok and dt >= s0 and lo <= dt < hi:
            out.append(dt)
        day += timedelta(days=1)
    return out


def effective_start(dt: datetime) -> datetime:
    """Map a nominal local time to the time it really happens (DST gaps move forward)."""
    t = time.mktime(dt.timetuple()[:8] + (-1,))
    real = datetime.fromtimestamp(t)
    if real.replace(microsecond=0) != dt.replace(microsecond=0):
        return real
    return dt


def next_occurrence(e: Entry, after: datetime, horizon_days: int = 400) -> datetime | None:
    occ = [o for o in occurrences(e, after, after + timedelta(days=horizon_days)) if o >= after]
    return min(occ) if occ else None


def find_overlap(entries: list[Entry], cand: Entry, now: datetime | None = None,
                 days: int = 70) -> Entry | None:
    """The first enabled entry whose windows overlap cand's (FR-SCHED-004)."""
    if not cand.enabled:
        return None
    now = now or datetime.now()
    try:
        lo = min(now, cand.start_dt)
    except ValueError:
        return None
    hi = lo + timedelta(days=days)
    mine = [(o, o + timedelta(seconds=cand.duration_s)) for o in occurrences(cand, lo, hi)]
    for other in entries:
        if other.id == cand.id or not other.enabled:
            continue
        for o in occurrences(other, lo, hi):
            a, b = o, o + timedelta(seconds=other.duration_s)
            for x, y in mine:
                if a < y and x < b:
                    return other
    return None


@dataclass
class Due:
    entry: Entry
    key: str               # occurrence id, run at most once
    start: datetime        # effective start
    end: datetime
    late_s: float


def check(entries: list[Entry], now: datetime, done: set[str], late_minutes: int) -> tuple[list[Due], list[Due]]:
    """(entries to start now, entries missed) for the current time."""
    start_now, missed = [], []
    for e in entries:
        if not e.enabled:
            continue
        for o in occurrences(e, now - timedelta(days=1), now + timedelta(seconds=1)):
            key = f"{e.id}@{o.isoformat()}"
            if key in done:
                continue
            eff = effective_start(o)
            end = eff + timedelta(seconds=e.duration_s)
            if eff <= now < end:
                late = (now - eff).total_seconds()
                d = Due(e, key, eff, end, late)
                if late <= late_minutes * 60 + 2:
                    start_now.append(d)
                else:
                    missed.append(d)
    return start_now, missed


def upcoming(entries: list[Entry], now: datetime, within_s: float) -> list[tuple[Entry, datetime]]:
    out = []
    for e in entries:
        if not e.enabled:
            continue
        n = next_occurrence(e, now)
        if n is not None and 0 <= (effective_start(n) - now).total_seconds() <= within_s:
            out.append((e, n))
    return out
