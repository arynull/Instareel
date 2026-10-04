"""fetch_post_analytics — per-post manual analytics refresh.

The dashboard Posts page has a small Refresh button per posted post. It
queues this task, which refreshes exactly one post: an explicit user
action, so it bypasses the per-post minimum interval, but it keeps the
shared per-post logic (pacing, all-zero pending guard) via
_refresh_one_post.

Each test gets a fresh temp-file SQLite DB; SyncSessionLocal is patched
so the task hits the same DB. Network is fully mocked (media_info
patched, sleep/random neutered). No broker, no Redis.
"""
import datetime as dt
import itertools
import random
import time

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.database as database
from app.config import settings
from app.database import Base
from app.models import (
    Account, Post, PostStatus, Video, VideoStatus,
)
from app.services.instagram_service import InstagramService
from app.tasks import periodic_tasks


@pytest.fixture()
def factory(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SyncSessionLocal", maker)
    monkeypatch.setattr(settings, "SCHEDULE_GRACE_MINUTES", 5)
    return maker


@pytest.fixture()
def fast_sweep(monkeypatch):
    """Sweep with no pacing and a media_info we control."""
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(random, "uniform", lambda a, b: 0.0)

    def _media_info(result):
        def _fn(self, username, media_id):
            return result

        monkeypatch.setattr(InstagramService, "media_info", _fn)

    return _media_info


_seed_counter = itertools.count()


def _seed(factory, status=PostStatus.posted, posted_hours_ago=2,
          views_7d=None, checked_hours_ago=None):
    now = dt.datetime.now(dt.timezone.utc)
    n = next(_seed_counter)
    with factory() as s:
        s.add(Account(username=f"ana{n}", password_enc="pw",
                      created_at=now - dt.timedelta(days=30)))
        s.add(Video(original_filename="a.mp4", raw_path="/tmp/a.mp4",
                    md5_hash=f"ana1-{n}", status=VideoStatus.processed))
        s.flush()
        acc = s.execute(select(Account).where(Account.username == f"ana{n}")).scalar_one()
        vid = s.execute(select(Video).where(Video.md5_hash == f"ana1-{n}")).scalar_one()
        post = Post(video_id=vid.id, account_id=acc.id, status=status,
                    posted_at=now - dt.timedelta(hours=posted_hours_ago),
                    ig_media_id="123_456", views_7d=views_7d,
                    last_analytics_check=(
                        now - dt.timedelta(hours=checked_hours_ago)
                        if checked_hours_ago is not None else None))
        s.add(post)
        s.flush()
        pid = post.id
        s.commit()
        return pid


def _post(factory, post_id):
    with factory() as s:
        return s.get(Post, post_id)


def test_single_post_refreshed(factory, fast_sweep):
    pid = _seed(factory)
    fast_sweep({"like_count": 3, "comment_count": 1, "view_count": 182})

    out = periodic_tasks.fetch_post_analytics.apply(kwargs={"post_id": pid}).get()

    assert out == {"updated": 1, "pending": 0}
    p = _post(factory, pid)
    assert p.views_7d == 182
    assert p.last_analytics_check is not None


def test_bypasses_min_interval(factory, fast_sweep):
    """Explicit user action: a post checked 10 minutes ago still refreshes
    (the scheduled sweeps would skip it for 3h)."""
    pid = _seed(factory, checked_hours_ago=1 / 6, views_7d=50)
    fast_sweep({"like_count": 0, "comment_count": 0, "view_count": 77})

    out = periodic_tasks.fetch_post_analytics.apply(kwargs={"post_id": pid}).get()

    assert out == {"updated": 1, "pending": 0}
    assert _post(factory, pid).views_7d == 77


def test_only_target_post_touched(factory, fast_sweep):
    """A per-post refresh must not touch sibling posts."""
    pid = _seed(factory)
    other = _seed(factory)
    fast_sweep({"like_count": 0, "comment_count": 0, "view_count": 42})

    out = periodic_tasks.fetch_post_analytics.apply(kwargs={"post_id": pid}).get()

    assert out == {"updated": 1, "pending": 0}
    assert _post(factory, pid).views_7d == 42
    assert _post(factory, other).views_7d is None
    assert _post(factory, other).last_analytics_check is None


def test_pending_guard_applies(factory, fast_sweep):
    """All-zero on a never-converged post: pending, no stamp — same shared
    logic as the sweeps."""
    pid = _seed(factory)
    fast_sweep({"like_count": 0, "comment_count": 0, "view_count": 0})

    out = periodic_tasks.fetch_post_analytics.apply(kwargs={"post_id": pid}).get()

    assert out == {"updated": 0, "pending": 1}
    p = _post(factory, pid)
    assert p.views_7d is None
    assert p.last_analytics_check is None


def test_missing_post_returns_not_found(factory, fast_sweep):
    out = periodic_tasks.fetch_post_analytics.apply(kwargs={"post_id": 999}).get()
    assert out == {"error": "not_found"}


def test_non_posted_post_returns_not_found(factory, fast_sweep):
    pid = _seed(factory, status=PostStatus.scheduled)
    out = periodic_tasks.fetch_post_analytics.apply(kwargs={"post_id": pid}).get()
    assert out == {"error": "not_found"}
