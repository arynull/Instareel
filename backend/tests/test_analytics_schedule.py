"""Beat-schedule regression tests for the analytics tasks.

Incident 2026-10-03: ``"fetch-analytics"`` was defined as
``crontab(hour="*/4")``. Celery's crontab defaults ``minute`` to ``"*"``,
so the sweep fired EVERY MINUTE of hours 0/4/8/12/16/20 (60 runs per 4h
window) instead of once per 4h. Server logs showed
``[system] Analytics refresh: 0 posts updated`` every minute from 00:31
to 00:50 — the first run stamped ``last_analytics_check`` and the
3h-per-post minimum interval made every follow-up run a no-op, so the
panel froze on a stale reading while a manual Refresh (force_refresh)
bypassed the interval and showed the real number.

These tests pin the minute on every sub-hourly schedule so the bug
cannot come back.
"""
import pytest
from celery.schedules import crontab

from app.tasks.celery_app import SLOW_TASKS, celery


def _schedule(name):
    return celery.conf.beat_schedule[name]["schedule"]


def test_fetch_analytics_fires_once_per_four_hours():
    """The 4h sweep must tick at minute 0 only — never every minute."""
    sched = _schedule("fetch-analytics")
    assert isinstance(sched, crontab)
    assert sched.minute == {0}, f"minute must be pinned to 0, got {sched.minute}"
    assert sched.hour == {0, 4, 8, 12, 16, 20}


def test_fetch_fresh_analytics_fires_hourly():
    """The young-post fast lane ticks once per hour, at a fixed minute."""
    sched = _schedule("fetch-fresh-analytics")
    assert isinstance(sched, crontab)
    assert len(sched.minute) == 1, f"must tick once per hour, got {sched.minute}"
    # Fixed minute, away from the :00 full sweep and the other hourly tasks
    # (:17 proxy refresh, :23 shadowban scan) so IG calls never burst.
    assert sched.minute not in ({0}, {17}, {23})


def test_analytics_tasks_ride_the_slow_lane():
    """Both sweeps make IG calls — they must not stall the fast lane."""
    assert "tasks.analytics_tasks.fetch_all_analytics" in SLOW_TASKS
    assert "tasks.analytics_tasks.fetch_fresh_analytics" in SLOW_TASKS
    assert celery.conf.task_routes["tasks.analytics_tasks.fetch_fresh_analytics"] == {
        "queue": "slow"
    }


@pytest.mark.parametrize(
    "name", ["fetch-analytics", "fetch-fresh-analytics"]
)
def test_analytics_schedules_are_not_wildcard_minute(name):
    """No analytics schedule may ever have minute='*' again."""
    sched = _schedule(name)
    assert sched.minute != set(range(60)), f"{name} must not run every minute"


def test_only_intended_tasks_run_every_minute():
    """check-scheduled-posts and beat-heartbeat are the only per-minute
    tasks by design; every other beat entry must pin a sub-hourly minute."""
    for name, entry in celery.conf.beat_schedule.items():
        sched = entry["schedule"]
        if name in ("check-scheduled-posts", "beat-heartbeat"):
            assert sched.minute == set(range(60)), f"{name} should stay per-minute"
        else:
            assert sched.minute != set(range(60)), (
                f"{name} has a wildcard minute — pin it (see 2026-10-03 incident)"
            )
