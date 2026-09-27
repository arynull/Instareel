"""Worker liveness signals: "busy" must not look like "down".

Covers: task prerun/postrun key bookkeeping, read_worker_state parsing,
busy_task_during_gap forensics, check_worker_sync warn-vs-down, and the
watchdog's busy-gap notification path.

Hermetic: Redis and the Celery control plane are faked; the watchdog's DB
is temp-file SQLite (same factory pattern as the notification tests).
"""
import datetime as dt
import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.database as database
import app.tasks.celery_app as celery_app_module
import app.tasks.health_tasks as health_tasks
import app.tasks.worker_signals as worker_signals
from app.config import settings
from app.database import Base
from app.models import Notification, Setting
from app.services import health_checks


class _FakeRedis:
    def __init__(self, store=None):
        self._store = dict(store or {})

    def get(self, key):
        return self._store.get(key)

    def set(self, key, value, ex=None):
        self._store[key] = value
        return True

    def delete(self, key):
        return self._store.pop(key, None) is not None

    def close(self):
        pass


@pytest.fixture()
def fake_redis(monkeypatch):
    fake = _FakeRedis()
    import redis as redis_module

    monkeypatch.setattr(redis_module, "from_url", lambda *a, **k: fake)
    return fake


@pytest.fixture()
def factory(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/w.db")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SyncSessionLocal", maker)
    monkeypatch.setattr(settings, "SCHEDULE_GRACE_MINUTES", 5)
    return maker


def _now():
    return dt.datetime.now(dt.timezone.utc)


# ---- describe_task / task_age_human ----


def test_describe_task_labels():
    assert worker_signals.describe_task("tasks.post_tasks.execute_post") == "posting a video"
    assert worker_signals.describe_task("tasks.video_tasks.process_video") == "processing a video"
    assert worker_signals.describe_task("tasks.analytics_tasks.fetch_all_analytics") == "fetching analytics"


def test_describe_task_fallback():
    assert worker_signals.describe_task("some.unknown_thing") == "unknown thing"
    assert worker_signals.describe_task("") == "working"


def test_task_age_human():
    now = _now()
    assert worker_signals.task_age_human((now - dt.timedelta(seconds=45)).isoformat()) == "45s ago"
    assert worker_signals.task_age_human((now - dt.timedelta(minutes=3)).isoformat()) == "3m ago"
    assert worker_signals.task_age_human("not-a-date") is None


# ---- prerun/postrun bookkeeping ----


class _Task:
    def __init__(self, name):
        self.name = name


def test_prerun_postrun_record_keys(fake_redis):
    worker_signals._record_task_start(task=_Task("tasks.post_tasks.execute_post"), task_id="abc")
    cur = json.loads(fake_redis.get(worker_signals.WORKER_CURRENT_TASK_KEY))
    assert cur["task"] == "tasks.post_tasks.execute_post"
    assert cur["id"] == "abc"
    assert cur["started_at"]

    worker_signals._record_task_end(task=_Task("tasks.post_tasks.execute_post"), task_id="abc")
    assert fake_redis.get(worker_signals.WORKER_CURRENT_TASK_KEY) is None
    last = json.loads(fake_redis.get(worker_signals.WORKER_LAST_TASK_KEY))
    assert last["task"] == "tasks.post_tasks.execute_post"
    assert last["started_at"] == cur["started_at"]
    assert last["finished_at"]


# ---- read_worker_state ----


def _set_alive(fake_redis, age_seconds):
    fake_redis.set(
        worker_signals.WORKER_ALIVE_KEY,
        (_now() - dt.timedelta(seconds=age_seconds)).isoformat(),
        ex=60,
    )


def test_read_worker_state_busy(fake_redis):
    _set_alive(fake_redis, 10)
    fake_redis.set(
        worker_signals.WORKER_CURRENT_TASK_KEY,
        json.dumps(
            {
                "task": "tasks.post_tasks.execute_post",
                "started_at": (_now() - dt.timedelta(minutes=3)).isoformat(),
            }
        ),
    )
    state = worker_signals.read_worker_state()
    assert state["alive"] is True
    assert state["current_task"]["task"] == "tasks.post_tasks.execute_post"


def test_read_worker_state_stale_alive_means_not_alive(fake_redis):
    _set_alive(fake_redis, 300)
    state = worker_signals.read_worker_state()
    assert state["alive"] is False


def test_read_worker_state_redis_down_returns_none(monkeypatch):
    import redis as redis_module

    def _boom(*a, **k):
        raise ConnectionError("nope")

    monkeypatch.setattr(redis_module, "from_url", _boom)
    assert worker_signals.read_worker_state() is None


# ---- busy_task_during_gap ----


def test_busy_gap_current_task_spanning_gap(fake_redis):
    now = _now()
    _set_alive(fake_redis, 5)
    fake_redis.set(
        worker_signals.WORKER_CURRENT_TASK_KEY,
        json.dumps(
            {
                "task": "tasks.post_tasks.execute_post",
                "started_at": (now - dt.timedelta(minutes=12)).isoformat(),
            }
        ),
    )
    assert (
        worker_signals.busy_task_during_gap(now - dt.timedelta(minutes=10))
        == "tasks.post_tasks.execute_post"
    )


def test_busy_gap_task_finished_inside_gap(fake_redis):
    now = _now()
    _set_alive(fake_redis, 5)
    fake_redis.set(
        worker_signals.WORKER_LAST_TASK_KEY,
        json.dumps(
            {
                "task": "tasks.video_tasks.process_video",
                "started_at": (now - dt.timedelta(minutes=9)).isoformat(),
                "finished_at": (now - dt.timedelta(minutes=4)).isoformat(),
            }
        ),
    )
    assert (
        worker_signals.busy_task_during_gap(now - dt.timedelta(minutes=10))
        == "tasks.video_tasks.process_video"
    )


def test_busy_gap_genuine_outage_returns_none(fake_redis):
    now = _now()
    _set_alive(fake_redis, 5)
    fake_redis.set(
        worker_signals.WORKER_LAST_TASK_KEY,
        json.dumps(
            {
                "task": "tasks.post_tasks.execute_post",
                "started_at": (now - dt.timedelta(minutes=40)).isoformat(),
                "finished_at": (now - dt.timedelta(minutes=35)).isoformat(),
            }
        ),
    )
    assert worker_signals.busy_task_during_gap(now - dt.timedelta(minutes=10)) is None


def test_busy_gap_no_redis_returns_none(monkeypatch):
    import redis as redis_module

    monkeypatch.setattr(
        redis_module, "from_url", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("x"))
    )
    assert worker_signals.busy_task_during_gap(_now() - dt.timedelta(minutes=10)) is None


# ---- check_worker_sync: warn, not down, when busy ----


def _patch_celery_ping(monkeypatch, pong):
    class _Inspect:
        def ping(self):
            return pong

    class _Control:
        def inspect(self, timeout=None):
            return _Inspect()

    class _Celery:
        control = _Control()

    monkeypatch.setattr(celery_app_module, "celery", _Celery())


def test_check_worker_ok_when_ping_answers(monkeypatch, fake_redis):
    _patch_celery_ping(monkeypatch, {"worker@host": {"ok": "pong"}})
    status, msg = health_checks.check_worker_sync()
    assert status == "ok"
    assert "worker(s) alive" in msg


def test_check_worker_busy_is_warn_not_down(monkeypatch, fake_redis):
    _patch_celery_ping(monkeypatch, {})  # solo pool: no reply while a task runs
    _set_alive(fake_redis, 10)
    fake_redis.set(
        worker_signals.WORKER_CURRENT_TASK_KEY,
        json.dumps(
            {
                "task": "tasks.post_tasks.execute_post",
                "started_at": (_now() - dt.timedelta(minutes=3)).isoformat(),
            }
        ),
    )
    status, msg = health_checks.check_worker_sync()
    assert status == "warn"
    assert "busy" in msg
    assert "posting a video" in msg
    assert "down" not in msg.lower().replace("shutdown", "")


def test_check_worker_down_when_process_dead(monkeypatch, fake_redis):
    _patch_celery_ping(monkeypatch, {})
    # no alive key at all -> process dead
    status, _ = health_checks.check_worker_sync()
    assert status == "down"


def test_check_worker_down_when_stale_alive_and_no_ping(monkeypatch, fake_redis):
    _patch_celery_ping(monkeypatch, {})
    _set_alive(fake_redis, 300)  # heartbeat stopped -> dead
    status, _ = health_checks.check_worker_sync()
    assert status == "down"


# ---- watchdog: busy gap notifies "busy", not "down" ----


def _patch_checks_ok(monkeypatch):
    checks = []
    for name, label in (
        ("database", "Database"),
        ("redis", "Redis"),
        ("celery_beat", "Celery beat"),
    ):
        checks.append((name, label, lambda: ("ok", "fine")))
    monkeypatch.setattr(health_checks, "CRITICAL_CHECKS", tuple(checks))


def _backdate_watchdog(factory, minutes):
    with factory() as s:
        row = s.get(Setting, "watchdog_last_run")
        row.value = (_now() - dt.timedelta(minutes=minutes)).isoformat()
        s.commit()


def _notifications(factory, ntype):
    with factory() as s:
        return (
            s.execute(select(Notification).where(Notification.ntype == ntype))
            .scalars()
            .all()
        )


def test_watchdog_busy_gap_warns_when_beyond_grace(factory, monkeypatch, fake_redis):
    _patch_checks_ok(monkeypatch)
    health_tasks.system_watchdog()  # seeds watchdog_last_run
    _backdate_watchdog(factory, 8)  # 8-minute gap (> 6 = 3x interval)
    # ...explained by a reel upload that finished 4 minutes ago
    fake_redis.set(
        worker_signals.WORKER_LAST_TASK_KEY,
        json.dumps(
            {
                "task": "tasks.post_tasks.execute_post",
                "started_at": (_now() - dt.timedelta(minutes=10)).isoformat(),
                "finished_at": (_now() - dt.timedelta(minutes=4)).isoformat(),
            }
        ),
    )
    health_tasks.system_watchdog()

    assert _notifications(factory, "scheduler_gap") == []
    busies = _notifications(factory, "scheduler_busy")
    assert len(busies) == 1
    assert busies[0].severity.value == "warning"  # 8m > 5m grace: a slot may have been missed
    assert "busy" in busies[0].message
    assert "posting a video" in busies[0].message


def test_watchdog_busy_gap_info_when_within_grace(factory, monkeypatch, fake_redis):
    _patch_checks_ok(monkeypatch)
    monkeypatch.setattr(settings, "SCHEDULE_GRACE_MINUTES", 10)
    health_tasks.system_watchdog()
    _backdate_watchdog(factory, 8)
    fake_redis.set(
        worker_signals.WORKER_LAST_TASK_KEY,
        json.dumps(
            {
                "task": "tasks.post_tasks.execute_post",
                "started_at": (_now() - dt.timedelta(minutes=10)).isoformat(),
                "finished_at": (_now() - dt.timedelta(minutes=4)).isoformat(),
            }
        ),
    )
    health_tasks.system_watchdog()

    assert _notifications(factory, "scheduler_gap") == []
    busies = _notifications(factory, "scheduler_busy")
    assert len(busies) == 1
    assert busies[0].severity.value == "info"
    assert "grace window" in busies[0].message


def test_watchdog_genuine_gap_still_warns_down(factory, monkeypatch, fake_redis):
    _patch_checks_ok(monkeypatch)
    health_tasks.system_watchdog()
    _backdate_watchdog(factory, 40)
    # no task activity anywhere near the gap -> real outage
    health_tasks.system_watchdog()

    assert len(_notifications(factory, "scheduler_busy")) == 0
    gaps = _notifications(factory, "scheduler_gap")
    assert len(gaps) == 1
    assert gaps[0].severity.value == "warning"
    assert "was down" in gaps[0].title
