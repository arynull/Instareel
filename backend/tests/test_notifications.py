"""Tests for the dashboard notification system.

Covers: notify_sync dedup semantics, the system_watchdog state machine
(down -> notify once, recovered -> auto-resolve + recovered notice,
scheduler gap detection, retention pruning), _next_fire computation, and
the /api/v1/notifications endpoints.
"""
import datetime as dt

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import app.database as database
import app.services.health_checks as health_checks
from app.config import settings
from app.database import Base
from app.models import Notification, ScheduleRule, Setting
from app.tasks import health_tasks
from app.tasks.sync_helpers import notify_sync

TEHRAN = "Asia/Tehran"


@pytest.fixture()
def factory(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SyncSessionLocal", maker)
    monkeypatch.setattr(settings, "SCHEDULE_TZ", TEHRAN)
    return maker


def _count(maker, **filters):
    with maker() as s:
        q = select(func.count(Notification.id))
        for k, v in filters.items():
            q = q.where(getattr(Notification, k) == v)
        return s.execute(q).scalar()


# ---- notify_sync ----


def test_notify_dedup_suppresses_while_unread(factory):
    maker = factory
    first = notify_sync("component_down", "critical", "DB down", "x", dedup_key="watchdog:database:down")
    second = notify_sync("component_down", "critical", "DB down", "y", dedup_key="watchdog:database:down")
    assert first == second
    assert _count(maker) == 1


def test_notify_dedup_allows_new_after_read(factory):
    maker = factory
    notify_sync("component_down", "critical", "DB down", "x", dedup_key="watchdog:database:down")
    with maker() as s:
        n = s.execute(select(Notification)).scalars().first()
        n.read_at = dt.datetime.now(dt.timezone.utc)
        s.commit()
    new_id = notify_sync("component_down", "critical", "DB down", "again", dedup_key="watchdog:database:down")
    assert _count(maker) == 2
    assert new_id is not None


def test_notify_without_dedup_always_creates(factory):
    maker = factory
    notify_sync("post_posted", "success", "t1", "m1")
    notify_sync("post_posted", "success", "t1", "m1")
    assert _count(maker) == 2


def test_notify_invalid_severity_defaults_to_info(factory):
    maker = factory
    notify_sync("x", "bogus", "t", "m")
    with maker() as s:
        n = s.execute(select(Notification)).scalars().first()
        assert n.severity.value == "info"


# ---- watchdog ----


def _patch_checks(monkeypatch, mapping):
    labels = {
        "database": "Database",
        "redis": "Redis",
        "celery_worker": "Celery worker",
        "celery_beat": "Celery beat",
    }
    checks = []
    for name in ("database", "redis", "celery_worker", "celery_beat"):
        status, message = mapping[name]

        def _check(s=status, m=message):
            return s, m

        checks.append((name, labels[name], _check))
    monkeypatch.setattr(health_checks, "CRITICAL_CHECKS", tuple(checks))


def test_watchdog_down_notifies_once_then_recovers(factory, monkeypatch):
    maker = factory
    ok = ("ok", "fine")
    _patch_checks(monkeypatch, {
        "database": ("down", "connection refused"),
        "redis": ok, "celery_worker": ok, "celery_beat": ok,
    })
    health_tasks.system_watchdog()
    health_tasks.system_watchdog()  # second tick: no duplicate
    assert _count(maker, ntype="component_down") == 1
    with maker() as s:
        n = s.execute(select(Notification).where(Notification.ntype == "component_down")).scalars().first()
        assert n.severity.value == "critical"
        assert n.dedup_key == "watchdog:database:down"
        assert n.read_at is None

    _patch_checks(monkeypatch, {
        "database": ok, "redis": ok, "celery_worker": ok, "celery_beat": ok,
    })
    health_tasks.system_watchdog()
    with maker() as s:
        down = s.execute(select(Notification).where(Notification.ntype == "component_down")).scalars().first()
        assert down.read_at is not None  # auto-resolved
        rec = s.execute(select(Notification).where(Notification.ntype == "component_recovered")).scalars().all()
        assert len(rec) == 1
        assert rec[0].severity.value == "success"


def test_watchdog_gap_detection(factory, monkeypatch):
    maker = factory
    ok = ("ok", "fine")
    _patch_checks(monkeypatch, {
        "database": ok, "redis": ok, "celery_worker": ok, "celery_beat": ok,
    })
    health_tasks.system_watchdog()  # seeds watchdog_last_run
    assert _count(maker, ntype="scheduler_gap") == 0
    # Simulate a 40-minute outage (worker/beat down, no watchdog runs).
    with maker() as s:
        row = s.get(Setting, "watchdog_last_run")
        row.value = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=40)).isoformat()
        s.commit()
    health_tasks.system_watchdog()
    assert _count(maker, ntype="scheduler_gap") == 1
    health_tasks.system_watchdog()  # last_run refreshed: no repeat
    assert _count(maker, ntype="scheduler_gap") == 1


def test_watchdog_prunes_old_notifications(factory, monkeypatch):
    maker = factory
    ok = ("ok", "fine")
    _patch_checks(monkeypatch, {
        "database": ok, "redis": ok, "celery_worker": ok, "celery_beat": ok,
    })
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=45)
    with maker() as s:
        s.add(Notification(ntype="x", severity="info", title="old", message="old", created_at=old))
        s.commit()
    assert _count(maker) == 1
    health_tasks.system_watchdog()
    assert _count(maker) == 0


# ---- _next_fire ----

from zoneinfo import ZoneInfo  # noqa: E402

from app.api.notifications import _next_fire  # noqa: E402


def _rule(dow, hour, minute):
    return ScheduleRule(name="r", day_of_week=dow, hour=hour, minute=minute, is_active=True)


def test_next_fire_daily_later_today():
    now = dt.datetime(2026, 9, 26, 10, 0, tzinfo=ZoneInfo(TEHRAN))  # Saturday
    nxt = _next_fire(_rule(-1, 21, 0), now)
    assert nxt is not None
    assert (nxt.hour, nxt.minute, nxt.date().isoformat()) == (21, 0, "2026-09-26")


def test_next_fire_daily_passed_goes_tomorrow():
    now = dt.datetime(2026, 9, 26, 22, 0, tzinfo=ZoneInfo(TEHRAN))
    nxt = _next_fire(_rule(-1, 21, 0), now)
    assert nxt is not None
    assert (nxt.hour, nxt.minute, nxt.date().isoformat()) == (21, 0, "2026-09-27")


def test_next_fire_weekly_skips_to_matching_day():
    # Saturday 10:00, rule is Monday 09:00 -> next Monday (2026-09-28).
    now = dt.datetime(2026, 9, 26, 10, 0, tzinfo=ZoneInfo(TEHRAN))
    nxt = _next_fire(_rule(0, 9, 0), now)
    assert nxt is not None
    assert (nxt.weekday(), nxt.hour, nxt.date().isoformat()) == (0, 9, "2026-09-28")


def test_next_fire_strictly_after_now():
    # Exact minute right now -> next occurrence, not this one.
    now = dt.datetime(2026, 9, 26, 21, 0, 30, tzinfo=ZoneInfo(TEHRAN))
    nxt = _next_fire(_rule(-1, 21, 0), now)
    assert nxt is not None
    assert nxt.date().isoformat() == "2026-09-27"


# ---- API (/api/v1/notifications) ----


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.api.deps import get_current_admin, get_db
    from app.main import app

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/api.db")
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def override_db():
        async with maker() as s:
            yield s

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    # Fresh loop per fixture: other test files may close the shared default
    # loop, which would break asyncio.get_event_loop() here.
    _run_coro(_create())
    monkeypatch.setattr(settings, "SCHEDULE_TZ", TEHRAN)
    app.dependency_overrides[get_current_admin] = lambda: "admin"
    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as c:
        yield c, maker
    app.dependency_overrides.clear()


def _run_coro(coro):
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _seed_async(maker, *rows):
    async def _go():
        async with maker() as s:
            s.add_all(rows)
            await s.commit()

    _run_coro(_go())


def test_api_list_shape_and_unread_count(api_client):
    from app.models import NotificationSeverity

    c, maker = api_client
    _seed_async(
        maker,
        Notification(ntype="a", severity=NotificationSeverity.CRITICAL, title="t1", message="m1"),
        Notification(
            ntype="b", severity=NotificationSeverity.INFO, title="t2", message="m2",
            read_at=dt.datetime.now(dt.timezone.utc),
        ),
    )
    r = c.get("/api/v1/notifications")
    assert r.status_code == 200
    body = r.json()
    assert body["unread_count"] == 1
    assert len(body["notifications"]) == 2
    assert {n["read"] for n in body["notifications"]} == {True, False}
    assert set(body.keys()) == {"notifications", "unread_count", "upcoming"}


def test_api_created_at_carries_utc_offset(api_client):
    """SQLite reads CURRENT_TIMESTAMP back naive; the API must still emit an
    explicit UTC offset so browsers don't parse it as local time (which
    shifted every timestamp by the server/browser offset)."""
    from app.models import NotificationSeverity

    c, maker = api_client
    _seed_async(
        maker,
        Notification(ntype="a", severity=NotificationSeverity.INFO, title="t", message="m"),
    )
    body = c.get("/api/v1/notifications").json()
    created_at = body["notifications"][0]["created_at"]
    assert created_at is not None
    parsed = dt.datetime.fromisoformat(created_at)
    assert parsed.tzinfo is not None, f"missing offset: {created_at}"
    # The instant must be ~now, not hours off (the reported bug).
    skew = abs((dt.datetime.now(dt.timezone.utc) - parsed).total_seconds())
    assert skew < 300, f"timestamp skewed by {skew}s: {created_at}"


def test_api_upcoming_lists_active_rules(api_client):
    c, maker = api_client
    _seed_async(maker, ScheduleRule(name="Sat 21:00", day_of_week=5, hour=21, minute=0, is_active=True))
    r = c.get("/api/v1/notifications")
    assert r.status_code == 200
    upcoming = r.json()["upcoming"]
    assert len(upcoming) == 1
    u = upcoming[0]
    assert u["rule_name"] == "Sat 21:00"
    assert u["in_seconds"] > 0
    assert u["label"].endswith("21:00")


def test_api_read_all_and_single(api_client):
    from app.models import NotificationSeverity

    c, maker = api_client
    _seed_async(
        maker,
        Notification(ntype="a", severity=NotificationSeverity.INFO, title="t1", message="m1"),
        Notification(ntype="b", severity=NotificationSeverity.INFO, title="t2", message="m2"),
    )
    nid = c.get("/api/v1/notifications").json()["notifications"][1]["id"]
    r = c.post(f"/api/v1/notifications/{nid}/read")
    assert r.status_code == 200
    assert c.get("/api/v1/notifications").json()["unread_count"] == 1
    r = c.post("/api/v1/notifications/999999/read")
    assert r.status_code == 404
    r = c.post("/api/v1/notifications/read-all")
    assert r.status_code == 200
    assert c.get("/api/v1/notifications").json()["unread_count"] == 0


def test_post_age_hours_handles_naive_db_timestamp():
    """Regression: fetch_all_analytics crashed with
    TypeError: can't subtract offset-naive and offset-aware datetimes
    because SQLite returns posted_at naive. Naive is UTC."""
    from app.tasks.periodic_tasks import _post_age_hours

    now = dt.datetime.now(dt.timezone.utc)
    naive_posted = (now - dt.timedelta(hours=5)).replace(tzinfo=None)
    aware_posted = now - dt.timedelta(hours=5)

    assert _post_age_hours(naive_posted, now) == pytest.approx(5.0, abs=0.01)
    assert _post_age_hours(aware_posted, now) == pytest.approx(5.0, abs=0.01)
    assert _post_age_hours(None, now) == 1.0  # floored
    # Future timestamps still floor at 1, never negative.
    assert _post_age_hours(now + dt.timedelta(hours=2), now) == 1.0


def test_watchdog_skips_self_worker_check():
    """The watchdog runs ON the worker; under the solo pool the worker can
    never answer its own inspect().ping(), so including the worker check
    would emit a spurious 'Celery worker down' critical notification every
    2 minutes. /system/health (backend) still covers the worker."""
    from app.services.health_checks import CRITICAL_CHECKS
    from app.tasks.health_tasks import _watchdog_checks

    names = [c[0] for c in _watchdog_checks()]
    assert "celery_worker" not in names
    assert "celery_worker" in [c[0] for c in CRITICAL_CHECKS]
    assert set(names) == {"database", "redis", "celery_beat"}
