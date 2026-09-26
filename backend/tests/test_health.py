"""System health endpoint (/system/health) + beat heartbeat task.

Hermetic by design: Redis and the Celery control plane are faked, so the
tests run without live infra — mirroring the conftest _FakeRedis approach.
"""
import asyncio
import datetime as dt
import shutil

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.tasks.celery_app as celery_app_module
from app.api.deps import get_current_admin, get_db
from app.config import settings
from app.database import Base
from app.main import app
from app.models import Account, AccountStatus, Proxy, ProxyProtocol


class _FakeAsyncRedis:
    """Minimal async Redis stand-in: ping/get/close."""

    def __init__(self, store):
        self._store = dict(store)

    async def ping(self):
        return True

    async def get(self, key):
        return self._store.get(key)

    async def close(self):
        pass


class _FakeSyncRedis:
    """Minimal sync Redis stand-in for app.services.health_checks."""

    def __init__(self, store):
        self._store = dict(store)

    def ping(self):
        return True

    def get(self, key):
        return self._store.get(key)

    def close(self):
        pass


class _FakeInspect:
    def __init__(self, pong):
        self._pong = pong

    def ping(self):
        return self._pong


class _FakeControl:
    def __init__(self, pong):
        self._pong = pong

    def inspect(self, timeout=None):
        return _FakeInspect(self._pong)


class _FakeCelery:
    def __init__(self, pong):
        self.control = _FakeControl(pong)


def _fake_redis_factory(store):
    def _from_url(*args, **kwargs):
        return _FakeAsyncRedis(store)

    return _from_url


def _fake_sync_redis_factory(store):
    def _from_url(*args, **kwargs):
        return _FakeSyncRedis(store)

    return _from_url


def _fresh_store():
    return {"health:beat_heartbeat": dt.datetime.now(dt.timezone.utc).isoformat()}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/h.db")
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def override_db():
        async with maker() as s:
            yield s

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.get_event_loop().run_until_complete(_create())
    (tmp_path / "media").mkdir()
    monkeypatch.setattr(settings, "MEDIA_ROOT", str(tmp_path / "media"))
    # ffmpeg is not installed in CI — pretend it is so the happy path is green.
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")

    # The health checks use the SYNC redis client (shared with the watchdog).
    import redis as redis_module

    monkeypatch.setattr(
        redis_module, "from_url", _fake_sync_redis_factory(_fresh_store())
    )
    monkeypatch.setattr(
        celery_app_module, "celery", _FakeCelery({"worker@test": {"ok": "pong"}})
    )

    app.dependency_overrides[get_current_admin] = lambda: "admin"
    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as c:
        yield c, maker
    app.dependency_overrides.clear()


def _seed(tmp_path, maker):
    async def _add():
        async with maker() as s:
            sess = tmp_path / "media" / "sess.dat"
            sess.write_text("x")
            s.add(
                Account(
                    username="healthy_ig",
                    password_enc="x",
                    status=AccountStatus.active,
                    session_file_path=str(sess),
                )
            )
            s.add(
                Proxy(
                    url="http://127.0.0.1:8080",
                    protocol=ProxyProtocol.http,
                    is_active=True,
                    is_healthy=True,
                )
            )
            await s.commit()

    asyncio.get_event_loop().run_until_complete(_add())


def test_health_all_ok(client, tmp_path):
    c, maker = client
    _seed(tmp_path, maker)
    r = c.get("/api/v1/system/health")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["overall"] == "ok", body
    by_name = {comp["name"]: comp for comp in body["components"]}
    assert set(by_name) == {
        "database",
        "redis",
        "celery_worker",
        "celery_beat",
        "instagram",
        "proxies",
        "ffmpeg",
        "disk",
    }
    for comp in body["components"]:
        assert comp["status"] == "ok", comp
        assert comp["latency_ms"] >= 0
        assert comp["message"]
    assert "PONG" in by_name["redis"]["message"]


def test_health_beat_stale_marks_down(client, tmp_path, monkeypatch):
    c, maker = client
    _seed(tmp_path, maker)
    stale = {
        "health:beat_heartbeat": (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)
        ).isoformat()
    }
    import redis as redis_module
    monkeypatch.setattr(redis_module, "from_url", _fake_sync_redis_factory(stale))
    body = c.get("/api/v1/system/health").json()
    comps = {c_["name"]: c_ for c_ in body["components"]}
    assert comps["celery_beat"]["status"] == "down"
    assert body["overall"] == "down"
    # The other critical checks are unaffected by the stale beat key.
    assert comps["redis"]["status"] == "ok"
    assert comps["celery_worker"]["status"] == "ok"


def test_health_beat_missing_marks_down(client, tmp_path, monkeypatch):
    c, maker = client
    _seed(tmp_path, maker)
    import redis as redis_module
    monkeypatch.setattr(redis_module, "from_url", _fake_sync_redis_factory({}))
    body = c.get("/api/v1/system/health").json()
    assert {c_["name"]: c_["status"] for c_ in body["components"]}["celery_beat"] == "down"


def test_health_worker_silent_marks_down(client, tmp_path, monkeypatch):
    c, maker = client
    _seed(tmp_path, maker)
    monkeypatch.setattr(celery_app_module, "celery", _FakeCelery({}))
    body = c.get("/api/v1/system/health").json()
    comps = {c_["name"]: c_ for c_ in body["components"]}
    assert comps["celery_worker"]["status"] == "down"
    assert body["overall"] == "down"


def test_health_missing_session_warns_not_down(client, tmp_path):
    c, maker = client

    async def _add():
        async with maker() as s:
            s.add(
                Account(
                    username="ghost",
                    password_enc="x",
                    status=AccountStatus.active,
                    session_file_path="/nonexistent/session.dat",
                )
            )
            await s.commit()

    asyncio.get_event_loop().run_until_complete(_add())
    body = c.get("/api/v1/system/health").json()
    comps = {c_["name"]: c_ for c_ in body["components"]}
    assert comps["instagram"]["status"] == "warn"
    assert "ghost" in comps["instagram"]["message"]
    # Non-critical: a missing session must not report the system as down.
    assert body["overall"] == "warn"


def test_beat_heartbeat_writes_key(monkeypatch):
    written = {}

    class _FakeSyncRedis:
        def set(self, key, value, ex=None):
            written[key] = (value, ex)

    monkeypatch.setattr("redis.from_url", lambda *a, **k: _FakeSyncRedis())
    from app.tasks.health_tasks import BEAT_HEARTBEAT_KEY, beat_heartbeat

    beat_heartbeat()  # calling the task directly runs it synchronously
    assert BEAT_HEARTBEAT_KEY in written
    value, ttl = written[BEAT_HEARTBEAT_KEY]
    ts = dt.datetime.fromisoformat(value)
    assert (dt.datetime.now(dt.timezone.utc) - ts).total_seconds() < 60
    assert ttl == 180


def test_all_beat_scheduled_tasks_are_registered():
    """Every task beat sends must be importable by the worker.

    Regression test: tasks sent by name from beat_schedule are only
    executed if the worker registered them (see the explicit imports in
    celery_app — autodiscover is unreliable for that package). A missing
    import means beat happily "sends" a task the worker rejects as
    unregistered, e.g. the beat heartbeat never landing in Redis.
    """
    from app.tasks.celery_app import celery

    schedule = celery.conf.beat_schedule
    assert schedule, "beat_schedule must not be empty"
    missing = sorted(
        {entry["task"] for entry in schedule.values()} - set(celery.tasks.keys())
    )
    assert missing == [], f"beat sends unregistered tasks: {missing}"
