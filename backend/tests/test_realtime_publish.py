"""Tests for the realtime WS event publishers.

The /ws feed is the dashboard's primary freshness channel; these tests pin
the publish side: log_event_sync -> new_log, notify_sync -> notification
(only on creation, never on dedup hits), and the best-effort contract —
a down Redis must never break logging, notifying, or the API.
"""
import asyncio
import json

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import app.database as database
import app.services.realtime as realtime
from app.config import settings
from app.database import Base
from app.models import Notification, SystemLog
from app.tasks import sync_helpers
from app.tasks.sync_helpers import log_event_sync, notify_sync


class _FakePubRedis:
    """Sync stand-in for redis.Redis capturing publish() calls."""

    def __init__(self, fail=False):
        self.published: list[tuple[str, dict]] = []
        self.fail = fail

    def publish(self, channel, message):
        if self.fail:
            raise ConnectionError("redis down")
        self.published.append((channel, json.loads(message)))
        return 1

    def set(self, *args, **kwargs):
        if self.fail:
            raise ConnectionError("redis down")
        return True

    def close(self):
        pass


class _FakeAsyncRedis:
    """Async stand-in for redis.asyncio.Redis capturing publish() calls."""

    def __init__(self, fail=False):
        self.published: list[tuple[str, dict]] = []
        self.fail = fail

    async def publish(self, channel, message):
        if self.fail:
            raise ConnectionError("redis down")
        self.published.append((channel, json.loads(message)))
        return 1

    async def aclose(self):
        pass


@pytest.fixture()
def factory(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SyncSessionLocal", maker)
    monkeypatch.setattr(settings, "SCHEDULE_TZ", "Asia/Tehran")
    return maker


@pytest.fixture()
def pubred(monkeypatch):
    fake = _FakePubRedis()
    monkeypatch.setattr(sync_helpers, "_redis_client", lambda: fake)
    return fake


def _events(fake):
    return [(channel, msg["event"], msg) for channel, msg in fake.published]


# ---- log_event_sync -> new_log ----


def test_log_event_persists_and_publishes_new_log(factory, pubred):
    log_event_sync("INFO", "system", "hello")
    with factory() as s:
        row = s.execute(select(SystemLog)).scalars().first()
        assert row is not None
        assert row.message == "hello"
    assert _events(pubred) == [
        ("igfunnel:events", "new_log", {"event": "new_log", "level": "INFO", "category": "system"})
    ]


def test_log_event_level_normalized_in_payload(factory, pubred):
    log_event_sync("warning", "scheduler", "late tick")
    _, _, msg = _events(pubred)[0]
    assert msg["level"] == "WARNING"


def test_log_event_survives_redis_down(factory, monkeypatch):
    monkeypatch.setattr(sync_helpers, "_redis_client", lambda: _FakePubRedis(fail=True))
    log_event_sync("ERROR", "system", "still persisted")  # must not raise
    with factory() as s:
        count = s.execute(select(func.count(SystemLog.id))).scalar()
    assert count == 1


# ---- notify_sync -> notification ----


def test_notify_publishes_notification_on_create(factory, pubred):
    nid = notify_sync("post_posted", "success", "Posted", "reel is live")
    assert nid is not None
    assert _events(pubred) == [
        (
            "igfunnel:events",
            "notification",
            {"event": "notification", "id": nid, "ntype": "post_posted", "severity": "success"},
        )
    ]


def test_notify_no_publish_on_dedup_hit(factory, pubred):
    first = notify_sync("tick_error", "critical", "t", "m", dedup_key="tick:ValueError")
    second = notify_sync("tick_error", "critical", "t", "m2", dedup_key="tick:ValueError")
    assert first == second
    assert len(pubred.published) == 1


def test_notify_publishes_again_after_read(factory, pubred):
    import datetime as dt

    notify_sync("tick_error", "critical", "t", "m", dedup_key="tick:ValueError")
    with factory() as s:
        n = s.execute(select(Notification)).scalars().first()
        n.read_at = dt.datetime.now(dt.timezone.utc)
        s.commit()
    notify_sync("tick_error", "critical", "t", "m2", dedup_key="tick:ValueError")
    assert len(pubred.published) == 2


def test_notify_survives_redis_down(factory, monkeypatch):
    monkeypatch.setattr(sync_helpers, "_redis_client", lambda: _FakePubRedis(fail=True))
    nid = notify_sync("post_posted", "success", "t", "m")  # must not raise
    assert nid is not None
    with factory() as s:
        count = s.execute(select(func.count(Notification.id))).scalar()
    assert count == 1


# ---- async publish() best-effort ----


def _run(coro):
    return asyncio.run(coro)


def test_async_publish_sends_event(monkeypatch):
    fake = _FakeAsyncRedis()
    monkeypatch.setattr(realtime.aioredis, "from_url", lambda *a, **k: fake)
    _run(realtime.publish("schedule_update", {"rule_id": 7}))
    assert fake.published == [
        ("igfunnel:events", {"event": "schedule_update", "rule_id": 7})
    ]


def test_async_publish_never_raises(monkeypatch):
    monkeypatch.setattr(
        realtime.aioredis, "from_url", lambda *a, **k: _FakeAsyncRedis(fail=True)
    )
    _run(realtime.publish("schedule_update", {}))  # must not raise


def test_async_publish_client_factory_failure_never_raises(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("no redis")

    monkeypatch.setattr(realtime.aioredis, "from_url", boom)
    _run(realtime.publish("new_log", {}))  # must not raise


# ---- async log_event -> new_log ----


def test_async_log_event_persists_and_publishes(tmp_path, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.services.log_service as log_service

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    _run(_init())
    maker = async_sessionmaker(bind=engine, expire_on_commit=False)
    # log_service binds SessionLocal via `from app.database import ...`,
    # so patch the reference it actually uses.
    monkeypatch.setattr(log_service, "SessionLocal", maker)
    fake = _FakeAsyncRedis()
    monkeypatch.setattr(realtime.aioredis, "from_url", lambda *a, **k: fake)

    _run(log_service.log_event("INFO", "account", "async hello"))

    async def _check():
        async with maker() as s:
            row = (await s.execute(select(SystemLog))).scalars().first()
            return row.message if row else None

    assert _run(_check()) == "async hello"
    assert fake.published == [
        ("igfunnel:events", {"event": "new_log", "level": "INFO", "category": "account"})
    ]
