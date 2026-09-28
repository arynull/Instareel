"""A failed scheduler tick must never die silently.

check_and_post runs every minute from beat. Its top-level ``except`` used to
only log to the worker's stderr and return {"error": "tick failed"} — the
dashboard bell stayed quiet and the watchdog (which only watches gaps
between watchdog runs, not errors inside a tick) saw nothing. The missing
``dispatched_at`` column incident is the concrete case: every tick raised
``sqlite3.OperationalError: no such column: dispatched_at`` for hours with
zero user-visible signal.

Now the tick emits a critical "tick_error" notification (deduped by error
class while unread) and a system-log ERROR entry.

Each test gets a fresh temp-file SQLite DB; SyncSessionLocal is patched so
the tasks hit the same DB. No network, no broker, no Redis.
"""
import datetime as dt

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import app.database as database
import app.tasks.sync_helpers as sync_helpers
from app.config import settings
from app.database import Base
from app.models import Notification, NotificationSeverity
from app.tasks import post_tasks


@pytest.fixture()
def factory(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SyncSessionLocal", maker)
    monkeypatch.setattr(settings, "SCHEDULE_GRACE_MINUTES", 5)
    return maker


def _fail_tick(monkeypatch, exc):
    """Make check_and_post raise ``exc`` at the top of its body."""
    def _boom():
        raise exc

    monkeypatch.setattr(sync_helpers, "ensure_daily_counts_reset", _boom)


def _notifications(factory):
    with factory() as s:
        return s.execute(select(Notification).order_by(Notification.id)).scalars().all()


def test_tick_error_notifies_critical(factory, monkeypatch):
    _fail_tick(monkeypatch, RuntimeError("boom"))
    result = post_tasks.check_and_post.apply().get()
    assert result == {"error": "tick failed"}

    notifs = _notifications(factory)
    assert len(notifs) == 1
    n = notifs[0]
    assert n.ntype == "tick_error"
    assert n.severity == NotificationSeverity.CRITICAL
    assert n.dedup_key == "tick_error:RuntimeError"
    assert "RuntimeError" in n.message and "boom" in n.message
    assert n.link == "/dashboard/logs"
    assert n.read_at is None


def test_tick_error_deduped_while_unread(factory, monkeypatch):
    _fail_tick(monkeypatch, RuntimeError("boom"))
    post_tasks.check_and_post.apply().get()
    # A persistent failure (e.g. every tick hitting the same missing column)
    # must not spam: one unread notification per error class.
    post_tasks.check_and_post.apply().get()
    post_tasks.check_and_post.apply().get()
    assert len(_notifications(factory)) == 1


def test_tick_error_renotifies_after_read(factory, monkeypatch):
    _fail_tick(monkeypatch, RuntimeError("boom"))
    post_tasks.check_and_post.apply().get()
    with factory() as s:
        n = s.execute(select(Notification)).scalars().one()
        n.read_at = dt.datetime.now(dt.timezone.utc)
        s.commit()
    # The admin acknowledged it but the tick is still broken — tell them again.
    post_tasks.check_and_post.apply().get()
    assert len(_notifications(factory)) == 2


def test_tick_error_distinct_per_error_class(factory, monkeypatch):
    _fail_tick(monkeypatch, RuntimeError("boom"))
    post_tasks.check_and_post.apply().get()
    _fail_tick(monkeypatch, ValueError("other"))
    post_tasks.check_and_post.apply().get()
    notifs = _notifications(factory)
    assert len(notifs) == 2
    assert {n.dedup_key for n in notifs} == {
        "tick_error:RuntimeError",
        "tick_error:ValueError",
    }
