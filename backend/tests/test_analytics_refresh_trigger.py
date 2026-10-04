"""POST /analytics/refresh — manual analytics trigger.

Hermetic: async Redis and Celery send_task are faked (mirrors the
test_health.py approach), so no live infra is needed.
"""
import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.tasks.celery_app as celery_app_module
from app.api.deps import get_current_admin, get_db
from app.database import Base
from app.main import app


class _FakeAsyncRedis:
    """Minimal async Redis stand-in with NX/EX set semantics."""

    def __init__(self, store):
        self._store = store  # shared dict on purpose: the lock must survive across requests

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self._store:
            return None
        self._store[key] = value
        return True

    async def aclose(self):
        pass


class _FakeCelery:
    def __init__(self):
        self.sent = []

    def send_task(self, name, kwargs=None):
        self.sent.append((name, kwargs or {}))
        return None


@pytest.fixture()
def client(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/r.db")
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def override_db():
        async with maker() as s:
            yield s

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.get_event_loop().run_until_complete(_create())

    store = {}
    import redis.asyncio as aioredis_module

    monkeypatch.setattr(
        aioredis_module, "from_url", lambda *a, **k: _FakeAsyncRedis(store)
    )
    fake_celery = _FakeCelery()
    monkeypatch.setattr(celery_app_module.celery, "send_task", fake_celery.send_task)

    app.dependency_overrides[get_current_admin] = lambda: "admin"
    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as c:
        yield c, fake_celery
    app.dependency_overrides.clear()


def test_refresh_queues_slow_task_with_force(client):
    c, fake_celery = client
    r = c.post("/api/v1/analytics/refresh")
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "queued"}
    assert fake_celery.sent == [
        ("tasks.analytics_tasks.fetch_all_analytics", {"force_refresh": True})
    ]


def test_second_refresh_while_running_gets_429(client):
    c, fake_celery = client
    assert c.post("/api/v1/analytics/refresh").status_code == 200
    r = c.post("/api/v1/analytics/refresh")
    assert r.status_code == 429
    # No second task was dispatched.
    assert len(fake_celery.sent) == 1


def test_refresh_allowed_when_redis_down(client, monkeypatch):
    c, fake_celery = client
    import redis.asyncio as aioredis_module

    def _boom(*a, **k):
        raise ConnectionError("redis down")

    monkeypatch.setattr(aioredis_module, "from_url", _boom)
    r = c.post("/api/v1/analytics/refresh")
    assert r.status_code == 200, r.text
    assert len(fake_celery.sent) == 1


# ------------------------------------------------- per-post refresh -------


def test_post_refresh_queues_single_task(client):
    c, fake_celery = client
    r = c.post("/api/v1/analytics/posts/5/refresh")
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "queued"}
    assert fake_celery.sent == [
        ("tasks.analytics_tasks.fetch_post_analytics", {"post_id": 5})
    ]


def test_second_post_refresh_while_running_gets_429(client):
    c, fake_celery = client
    assert c.post("/api/v1/analytics/posts/5/refresh").status_code == 200
    r = c.post("/api/v1/analytics/posts/5/refresh")
    assert r.status_code == 429
    # No second task was dispatched.
    assert len(fake_celery.sent) == 1


def test_post_refresh_lock_is_per_post(client):
    """The lock is per post id — refreshing post 6 while post 5 is locked
    is allowed."""
    c, fake_celery = client
    assert c.post("/api/v1/analytics/posts/5/refresh").status_code == 200
    r = c.post("/api/v1/analytics/posts/6/refresh")
    assert r.status_code == 200, r.text
    assert fake_celery.sent == [
        ("tasks.analytics_tasks.fetch_post_analytics", {"post_id": 5}),
        ("tasks.analytics_tasks.fetch_post_analytics", {"post_id": 6}),
    ]


def test_post_refresh_allowed_when_redis_down(client, monkeypatch):
    c, fake_celery = client
    import redis.asyncio as aioredis_module

    def _boom(*a, **k):
        raise ConnectionError("redis down")

    monkeypatch.setattr(aioredis_module, "from_url", _boom)
    r = c.post("/api/v1/analytics/posts/5/refresh")
    assert r.status_code == 200, r.text
    assert len(fake_celery.sent) == 1
