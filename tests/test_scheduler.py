from datetime import datetime, timedelta

from vrp.core import scheduler
from vrp.core.scheduler import Entry


def test_weekly_occurrences():
    e = Entry(start="2026-10-19T09:00", duration_s=1800, repeat="weekly", days=[0, 2])   # Mon, Wed
    occ = scheduler.occurrences(e, datetime(2026, 10, 19), datetime(2026, 10, 31))
    assert [o.strftime("%a %d") for o in occ] == ["Mon 19", "Wed 21", "Mon 26", "Wed 28"]


def test_monthly_on_31st_uses_last_day():
    e = Entry(start="2026-01-31T08:00", repeat="monthly")
    occ = scheduler.occurrences(e, datetime(2026, 2, 1), datetime(2026, 3, 1))
    assert occ == [datetime(2026, 2, 28, 8, 0)]


def test_overlap_detected():                                                         # FR-SCHED-004
    a = Entry(name="A", start="2026-10-21T09:00", duration_s=3600, repeat="weekdays")
    b = Entry(name="B", start="2026-10-24T09:30", duration_s=600, repeat="weekly", days=[4])  # Fridays
    assert scheduler.find_overlap([a], b, now=datetime(2026, 10, 20)).name == "A"
    c = Entry(name="C", start="2026-10-24T09:30", duration_s=600, repeat="weekly", days=[5])  # Saturdays
    assert scheduler.find_overlap([a], c, now=datetime(2026, 10, 20)) is None


def test_tc15_late_start_and_missed():                                               # TC-15, FR-SCHED-006
    e = Entry(name="Morning", start="2026-10-20T10:00", duration_s=1800)
    start, missed = scheduler.check([e], datetime(2026, 10, 20, 10, 3), set(), 5)
    assert len(start) == 1 and abs(start[0].late_s - 180) < 1
    assert start[0].end == datetime(2026, 10, 20, 10, 30)
    start, missed = scheduler.check([e], datetime(2026, 10, 20, 10, 12), set(), 5)
    assert not start and len(missed) == 1


def test_runs_once_per_occurrence():
    e = Entry(start="2026-10-20T10:00", duration_s=600, repeat="daily")
    now = datetime(2026, 10, 20, 10, 0, 1)
    start, _ = scheduler.check([e], now, set(), 5)
    done = {start[0].key}
    start2, _ = scheduler.check([e], now + timedelta(seconds=30), done, 5)
    assert not start2


def test_next_occurrence_disabled_and_once():
    e = Entry(start="2026-10-20T10:00", repeat="once")
    assert scheduler.next_occurrence(e, datetime(2026, 10, 21)) is None
    assert scheduler.next_occurrence(e, datetime(2026, 10, 19)) == datetime(2026, 10, 20, 10, 0)
