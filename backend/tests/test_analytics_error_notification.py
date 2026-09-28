"""A crashing analytics sweep must never silently freeze view counts.

fetch_all_analytics refreshes views/likes every 4h. Its top-level
``except`` used to only log to the worker's stderr and return
{"error": "failed"} — the dashboard kept showing frozen numbers with zero
user-visible signal. The skipped-0018-migration incident is the concrete
case: select(Post) raised OperationalError on every run, so no view count
moved for hours and nobody was told.

Now a failed sweep writes a system-log ERROR and emits a warning
'analytics_error' notification (deduped by error class while unread).

Each test gets a fresh temp-file SQLite DB; SyncSessionLocal is patched so
the tasks hit the same DB. No network, no broker, no Redis.
"""
import datetime as dt

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.database as database
import app.tasks.sync_helpers as sync_helpers
from app.config import settings
from app.database import Base
from app.models import Account, Notification, NotificationSeverity, Post, PostStatus, Video, VideoStatus
from app.tasks import periodic_tasks


@pytest.fixture()
def factory(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SyncSessionLocal", maker)
    monkeypatch.setattr(settings, "SCHEDULE_GRACE_MINUTES", 5)
    return maker


def _seed_posted(factory):
    now = dt.datetime.now(dt.timezone.utc)
    with factory() as s:
        s.add(Account(username="ana", password_enc="x",
                      created_at=now - dt.timedelta(days=30)))
        s.add(Video(original_filename="a.mp4", raw_path="/tmp/a.mp4",
                    md5_hash="ana1", status=VideoStatus.processed))
        s.flush()
        acc = s.execute(select(Account).where(Account.username == "ana")).scalar_one()
        vid = s.execute(select(Video).where(Video.md5_hash == "ana1")).scalar_one()
        s.add(Post(video_id=vid.id, account_id=acc.id, status=PostStatus.posted,
                   posted_at=now - dt.timedelta(hours=2), ig_media_id="123",
                   views_7d=500))
        s.commit()


def _fail_sweep(monkeypatch, exc):
    def _boom(ts):
        raise exc

    monkeypatch.setattr(sync_helpers, "as_aware_utc", _boom)


def _notifications(factory):
    with factory() as s:
        return s.execute(select(Notification).order_by(Notification.id)).scalars().all()


def test_analytics_error_notifies_warning(factory, monkeypatch):
    _seed_posted(factory)
    _fail_sweep(monkeypatch, RuntimeError("db gone"))
    result = periodic_tasks.fetch_all_analytics.apply().get()
    assert result == {"error": "failed"}

    notifs = _notifications(factory)
    assert len(notifs) == 1
    n = notifs[0]
    assert n.ntype == "analytics_error"
    assert n.severity == NotificationSeverity.WARNING
    assert n.dedup_key == "analytics_error:RuntimeError"
    assert "RuntimeError" in n.message and "db gone" in n.message
    assert n.link == "/dashboard/logs"
    assert n.read_at is None


def test_analytics_error_deduped_while_unread(factory, monkeypatch):
    _seed_posted(factory)
    _fail_sweep(monkeypatch, RuntimeError("db gone"))
    periodic_tasks.fetch_all_analytics.apply().get()
    periodic_tasks.fetch_all_analytics.apply().get()
    assert len(_notifications(factory)) == 1
