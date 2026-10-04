"""Task lanes: slow jobs must never stall the fast scheduler ticks.

Covers:
- celery routing: slow tasks -> slow queue, default queue -> fast
- every beat-scheduled task resolves to a known queue and is registered
- per-lane worker signal keys (WORKER_LANE namespacing) and legacy fallback
- per-lane health checks (fast/slow), incl. busy-vs-down distinction
- watchdog: no self-check of the fast lane, slow-lane monitoring gated on
  WORKER_LANE=fast, slow-lane down -> critical notification (deduped)

Hermetic: Redis faked, Celery control plane faked, DB is temp SQLite.
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
from app.database import Base
from app.models import Notification
from app.services import health_checks


class _FakeRedis:
    def __init__(self):
        self.store = {}

    def setex(self, k, ttl, v):
        self.store[k] = (v, dt.datetime.now(dt.timezone.utc).timestamp() + ttl)

    def set(self, k, v, ex=None, **kwargs):
        exp = None
        if ex is not None:
            exp = dt.datetime.now(dt.timezone.utc).timestamp() + ex
        self.store[k] = (v, exp)

    def get(self, k):
        hit = self.store.get(k)
        if hit is None:
            return None
        v, exp = hit
        if exp is not None and dt.datetime.now(dt.timezone.utc).timestamp() > exp:
            del self.store[k]
            return None
        return v

    def delete(self, *ks):
        for k in ks:
            self.store.pop(k, None)

    def close(self):
        pass


@pytest.fixture()
def fake_redis(monkeypatch):
    fr = _FakeRedis()
    monkeypatch.setattr(worker_signals, "_redis_client", lambda: fr)
    return fr


@pytest.fixture()
def factory(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    Base.metadata.create_all(engine)
    fac = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SyncSessionLocal", fac)
    return fac


def _now():
    return dt.datetime.now(dt.timezone.utc)


# ---------------------------------------------------------------- routing ---


def test_slow_tasks_routed_to_slow_queue():
    routes = celery_app_module.celery.conf.task_routes
    assert set(celery_app_module.SLOW_TASKS) == {
        "tasks.video_tasks.process_video",
        "tasks.post_tasks.execute_post",
        "tasks.source_tasks.ingest_source",
        "tasks.analytics_tasks.fetch_all_analytics",
        "tasks.analytics_tasks.fetch_fresh_analytics",
        "tasks.analytics_tasks.fetch_post_analytics",
    }
    for name in celery_app_module.SLOW_TASKS:
        assert routes[name] == {"queue": "slow"}, name


def test_default_queue_is_fast():
    assert celery_app_module.celery.conf.task_default_queue == "fast"


def test_every_beat_task_resolves_to_a_known_queue():
    """No beat entry may silently land on the celery default queue."""
    routes = celery_app_module.celery.conf.task_routes
    default = celery_app_module.celery.conf.task_default_queue
    for entry, cfg in celery_app_module.celery.conf.beat_schedule.items():
        task = cfg["task"]
        queue = routes.get(task, {}).get("queue", default)
        assert queue in ("fast", "slow"), f"{entry}: {task} -> {queue}"
    assert default == "fast"


def test_beat_scheduled_tasks_are_registered():
    registered = set(celery_app_module.celery.tasks)
    for entry, cfg in celery_app_module.celery.conf.beat_schedule.items():
        assert cfg["task"] in registered, f"{entry}: {cfg['task']} not registered"
    assert "tasks.account_tasks.scan_shadowban" in registered


def test_fast_lane_beat_entries_dont_include_slow_tasks():
    """Scheduler/watchdog/proxy/cleanup/shadowban beat entries must stay
    on fast; the only beat entry allowed on slow is the analytics sweep."""
    routes = celery_app_module.celery.conf.task_routes
    default = celery_app_module.celery.conf.task_default_queue
    fast_entries = {
        "check-scheduled-posts": "fast",
        "system-watchdog": "fast",
        "beat-heartbeat": "fast",
        "proxy-health-check": "fast",
        "proxy-pool-refresh": "fast",
        "media-cleanup": "fast",
        "reset-daily-counts": "fast",
        "shadowban-scan": "fast",
        "reap-stale-posting": "fast",
    }
    schedule = celery_app_module.celery.conf.beat_schedule
    for entry, lane in fast_entries.items():
        assert entry in schedule, f"beat entry {entry} missing"
        task = schedule[entry]["task"]
        queue = routes.get(task, {}).get("queue", default)
        assert queue == lane, f"{entry}: {task} -> {queue}"


def test_beat_driven_slow_tasks_stay_on_slow_queue():
    """fetch-analytics / fetch-fresh-analytics are beat-driven AND
    slow-lane: they must resolve to the slow queue so the analytics
    sweeps never block scheduler ticks."""
    routes = celery_app_module.celery.conf.task_routes
    default = celery_app_module.celery.conf.task_default_queue
    schedule = celery_app_module.celery.conf.beat_schedule
    beat_tasks = {cfg["task"] for cfg in schedule.values()}
    slow_on_beat = beat_tasks & set(celery_app_module.SLOW_TASKS)
    assert slow_on_beat == {
        "tasks.analytics_tasks.fetch_all_analytics",
        "tasks.analytics_tasks.fetch_fresh_analytics",
    }
    for task in slow_on_beat:
        assert routes[task]["queue"] == "slow"
    assert default == "fast"


# ------------------------------------------------- per-lane signal keys ---


def _fake_task(name):
    class _T:
        pass

    t = _T()
    t.name = name
    return t


def test_lane_env_namespaces_signal_keys(fake_redis, monkeypatch):
    monkeypatch.setenv("WORKER_LANE", "fast")
    worker_signals._record_task_start(task=_fake_task("tasks.post_tasks.check_and_post"), task_id="tid")
    cur = json.loads(fake_redis.get("health:worker_current_task:fast"))
    assert cur["task"] == "tasks.post_tasks.check_and_post"
    assert fake_redis.get("health:worker_current_task") is None
    # the alive key holds an ISO instant refreshed by the daemon thread
    fake_redis.set("health:worker_alive:fast", _now().isoformat())
    state = worker_signals.read_worker_state()  # own lane
    assert state["alive"] is True
    assert state["current_task"]["task"] == "tasks.post_tasks.check_and_post"
    assert worker_signals.own_lane_alive() is True


def test_no_lane_env_keeps_legacy_keys(fake_redis, monkeypatch):
    monkeypatch.delenv("WORKER_LANE", raising=False)
    worker_signals._record_task_start(task=_fake_task("tasks.post_tasks.check_and_post"), task_id="tid")
    cur = json.loads(fake_redis.get("health:worker_current_task"))
    assert cur["task"] == "tasks.post_tasks.check_and_post"
    assert fake_redis.get("health:worker_current_task:fast") is None
    fake_redis.set("health:worker_alive", _now().isoformat())
    assert worker_signals.read_worker_state()["alive"] is True
    assert worker_signals.own_lane_alive() is True


def test_read_worker_state_lane_param(fake_redis, monkeypatch):
    monkeypatch.setenv("WORKER_LANE", "slow")
    worker_signals._record_task_start(task=_fake_task("tasks.video_tasks.process_video"), task_id="tid")
    state = worker_signals.read_worker_state(lane="slow")
    assert state["current_task"]["task"] == "tasks.video_tasks.process_video"
    other = worker_signals.read_worker_state(lane="fast")
    assert other["current_task"] is None


def test_own_lane_alive(fake_redis, monkeypatch):
    monkeypatch.setenv("WORKER_LANE", "fast")
    assert worker_signals.own_lane_alive() is False  # no key yet
    fake_redis.set("health:worker_alive:fast", _now().isoformat())
    assert worker_signals.own_lane_alive() is True
    # stale keys don't count
    fake_redis.set(
        "health:worker_alive:fast",
        (_now() - dt.timedelta(minutes=5)).isoformat(),
    )
    assert worker_signals.own_lane_alive() is False
    # another lane's key does not count for this lane
    fake_redis.delete("health:worker_alive:fast")
    fake_redis.set("health:worker_alive:slow", _now().isoformat())
    assert worker_signals.own_lane_alive() is False
    # garbage values don't count either
    fake_redis.set("health:worker_alive:fast", "not-a-timestamp")
    assert worker_signals.own_lane_alive() is False


def test_own_lane_alive_no_redis(monkeypatch):
    monkeypatch.setenv("WORKER_LANE", "fast")
    monkeypatch.setattr(worker_signals, "_redis_client", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    assert worker_signals.own_lane_alive() is False


def test_busy_task_during_gap_reads_own_lane(fake_redis, monkeypatch):
    """A fast-lane watchdog must not blame a slow-lane task for the gap."""
    monkeypatch.setenv("WORKER_LANE", "fast")
    now = _now()
    # slow lane finished a task mid-gap — invisible from the fast lane
    fake_redis.set(
        "health:worker_last_task:slow",
        json.dumps(
            {
                "task": "tasks.video_tasks.process_video",
                "finished_at": (now - dt.timedelta(minutes=2)).isoformat(),
            }
        ),
    )
    assert health_tasks._busy_task_during_gap(now - dt.timedelta(minutes=5)) is None
    # ...but the fast lane's own finished task does explain the gap
    fake_redis.set(
        "health:worker_last_task:fast",
        json.dumps(
            {
                "task": "tasks.proxy_tasks.check_all_proxies",
                "finished_at": (now - dt.timedelta(minutes=2)).isoformat(),
            }
        ),
    )
    fake_redis.set(
        "health:worker_started_at:fast",
        (now - dt.timedelta(hours=1)).isoformat(),  # not a mid-gap restart
    )
    assert (
        health_tasks._busy_task_during_gap(now - dt.timedelta(minutes=5))
        == "tasks.proxy_tasks.check_all_proxies"
    )


# ------------------------------------------------------ per-lane checks ---


def _patch_ping(monkeypatch, payload):
    class _Inspect:
        def ping(self):
            return payload

    monkeypatch.setattr(
        celery_app_module.celery.control, "inspect", lambda *a, **k: _Inspect()
    )


def test_fast_lane_ok_via_hostname_ping(fake_redis, monkeypatch):
    # ping replies are keyed "fast@<container>" — lane prefix must match
    _patch_ping(monkeypatch, {"fast@a3f2b1": {"ok": "pong"}})
    assert health_checks.check_worker_fast_sync() == ("ok", "Fast lane alive (ping)")
    status, _ = health_checks.check_worker_slow_sync()
    assert status == "down"  # nothing answers for slow


def test_slow_lane_ok_via_hostname_ping(fake_redis, monkeypatch):
    _patch_ping(monkeypatch, {"slow@9c8d7e": {"ok": "pong"}})
    assert health_checks.check_worker_slow_sync() == ("ok", "Slow lane alive (ping)")
    status, _ = health_checks.check_worker_fast_sync()
    assert status == "down"


def test_lane_ping_key_must_not_cross_match(fake_redis, monkeypatch):
    """'faster@x' must not count as the fast lane; exact lane prefix only."""
    _patch_ping(monkeypatch, {"faster@x": {"ok": "pong"}, "slowish@y": {"ok": "pong"}})
    assert health_checks.check_worker_fast_sync()[0] == "down"
    assert health_checks.check_worker_slow_sync()[0] == "down"


def test_legacy_single_worker_ping_does_not_attribute(fake_redis, monkeypatch):
    """A pre-lane worker ('celery@host') can't be attributed to a lane —
    lanes fall back to the legacy un-suffixed signals instead."""
    _patch_ping(monkeypatch, {"celery@host": {"ok": "pong"}})
    assert health_checks.check_worker_fast_sync()[0] == "down"
    assert health_checks.check_worker_slow_sync()[0] == "down"


def test_slow_lane_busy_is_warn_not_down(fake_redis, monkeypatch):
    _patch_ping(monkeypatch, {})  # solo pool: blocked, can't answer
    fake_redis.set("health:worker_alive:slow", _now().isoformat())
    fake_redis.set(
        "health:worker_current_task:slow",
        json.dumps({"task": "tasks.video_tasks.process_video", "id": "t1"}),
    )
    status, detail = health_checks.check_worker_slow_sync()
    assert status == "warn"
    assert "Slow lane" in detail
    assert "processing a video" in detail


def test_slow_lane_falls_back_to_legacy_keys(fake_redis, monkeypatch):
    """Single-worker local dev (no WORKER_LANE): legacy keys feed both lanes."""
    _patch_ping(monkeypatch, {})
    fake_redis.set("health:worker_alive", _now().isoformat())
    fake_redis.set(
        "health:worker_current_task",
        json.dumps({"task": "tasks.health_tasks.system_watchdog", "id": "t1"}),
    )
    for check in (health_checks.check_worker_fast_sync, health_checks.check_worker_slow_sync):
        status, _ = check()
        assert status == "warn", check.__name__


def test_both_lanes_down_without_signals(fake_redis, monkeypatch):
    _patch_ping(monkeypatch, {})
    assert health_checks.check_worker_fast_sync()[0] == "down"
    assert health_checks.check_worker_slow_sync()[0] == "down"


def test_ping_error_is_down(fake_redis, monkeypatch):
    class _Inspect:
        def ping(self):
            raise RuntimeError("broker unreachable")

    monkeypatch.setattr(
        celery_app_module.celery.control, "inspect", lambda *a, **k: _Inspect()
    )
    # @_verdict converts the exception into a down verdict, never raises
    status, detail = health_checks.check_worker_fast_sync()
    assert status == "down"
    assert "broker unreachable" in detail


# -------------------------------------------------------------- watchdog ---


def test_watchdog_does_not_self_check_worker_lanes():
    names = [c[0] for c in health_tasks._watchdog_checks()]
    assert not any(n.startswith("celery_worker") for n in names)
    names = [c[0] for c in health_checks.CRITICAL_CHECKS]
    assert "celery_worker_fast" in names
    assert "celery_worker_slow" in names


def test_slow_lane_watch_gating(monkeypatch):
    monkeypatch.delenv("WORKER_LANE", raising=False)
    assert health_tasks._slow_lane_watch() is None
    monkeypatch.setenv("WORKER_LANE", "slow")
    assert health_tasks._slow_lane_watch() is None
    monkeypatch.setenv("WORKER_LANE", "fast")
    name, label, check = health_tasks._slow_lane_watch()
    assert name == "celery_worker_slow"
    assert check is health_checks.check_worker_slow_sync


def _slow_only_watchdog(monkeypatch):
    """Run the real watchdog but with only the slow-lane check, so other
    services (real redis etc.) don't pollute the test."""
    monkeypatch.setattr(health_tasks, "_watchdog_checks", lambda: [])
    monkeypatch.setattr(
        health_tasks, "_busy_task_during_gap", lambda last_run: None
    )


def test_watchdog_alerts_on_slow_lane_down(factory, fake_redis, monkeypatch):
    monkeypatch.setenv("WORKER_LANE", "fast")
    _patch_ping(monkeypatch, {})
    _slow_only_watchdog(monkeypatch)
    health_tasks.system_watchdog()  # never raises; verify via notifications
    with factory() as s:
        notes = s.execute(select(Notification)).scalars().all()
    down = [n for n in notes if n.dedup_key == "watchdog:celery_worker_slow:down"]
    assert len(down) == 1
    assert down[0].severity.value == "critical"
    assert "slow lane" in down[0].title


def test_watchdog_slow_lane_alert_is_deduped(factory, fake_redis, monkeypatch):
    """A dead slow lane must not spam a critical notification every 2 min."""
    monkeypatch.setenv("WORKER_LANE", "fast")
    _patch_ping(monkeypatch, {})
    _slow_only_watchdog(monkeypatch)
    health_tasks.system_watchdog()
    health_tasks.system_watchdog()
    with factory() as s:
        notes = s.execute(select(Notification)).scalars().all()
    down = [n for n in notes if n.dedup_key == "watchdog:celery_worker_slow:down"]
    assert len(down) == 1


def test_watchdog_slow_lane_recovery_notifies(factory, fake_redis, monkeypatch):
    monkeypatch.setenv("WORKER_LANE", "fast")
    _patch_ping(monkeypatch, {})
    _slow_only_watchdog(monkeypatch)
    health_tasks.system_watchdog()  # down
    _patch_ping(monkeypatch, {"slow@host": {"ok": "pong"}})
    health_tasks.system_watchdog()  # recovered
    with factory() as s:
        notes = s.execute(select(Notification)).scalars().all()
    recovered = [n for n in notes if n.ntype == "component_recovered"]
    assert len(recovered) == 1
    assert "slow lane" in recovered[0].title
    # the down alert was auto-resolved (marked read) on recovery
    with factory() as s:
        down = s.execute(
            select(Notification).where(
                Notification.dedup_key == "watchdog:celery_worker_slow:down"
            )
        ).scalars().first()
    assert down.read_at is not None


def test_watchdog_ignores_slow_lane_without_fast_env(factory, fake_redis, monkeypatch):
    """Single-worker deployment: no slow lane exists, no alerts about it."""
    monkeypatch.delenv("WORKER_LANE", raising=False)
    _patch_ping(monkeypatch, {})
    _slow_only_watchdog(monkeypatch)
    health_tasks.system_watchdog()  # never raises; verify via notifications
    with factory() as s:
        notes = s.execute(select(Notification)).scalars().all()
    assert [n for n in notes if "celery_worker_slow" in (n.dedup_key or "")] == []


def test_watchdog_slow_lane_busy_is_warn(factory, fake_redis, monkeypatch):
    """A busy slow worker (mid-upload) must not raise a critical down alert."""
    monkeypatch.setenv("WORKER_LANE", "fast")
    _patch_ping(monkeypatch, {})
    fake_redis.set("health:worker_alive:slow", _now().isoformat())
    fake_redis.set(
        "health:worker_current_task:slow",
        json.dumps({"task": "tasks.post_tasks.execute_post", "id": "t1"}),
    )
    _slow_only_watchdog(monkeypatch)
    health_tasks.system_watchdog()  # never raises; verify via notifications
    with factory() as s:
        notes = s.execute(select(Notification)).scalars().all()
    assert [n for n in notes if n.severity.value == "critical"] == []
