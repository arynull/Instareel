"""Hourly young-post analytics fast lane (fetch_fresh_analytics).

Reels posted within FRESH_ANALYTICS_MAX_AGE_HOURS (24h) move fast — views
can jump 6 -> 163 in ten minutes while the 4h sweep still shows the stale
number. The fast lane re-checks only young posts, at most hourly per
post, so the panel tracks the Instagram app within ~1h.

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
    Account, Post, PostStatus, SystemLog, Video, VideoStatus,
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


def _seed(factory, posted_hours_ago, views_7d=None, checked_hours_ago=None):
    now = dt.datetime.now(dt.timezone.utc)
    n = next(_seed_counter)
    uname = f"ana{n}"
    md5 = f"ana1-{n}"
    with factory() as s:
        s.add(Account(username=uname, password_enc="pw",
                      created_at=now - dt.timedelta(days=30)))
        s.add(Video(original_filename="a.mp4", raw_path="/tmp/a.mp4",
                    md5_hash=md5, status=VideoStatus.processed))
        s.flush()
        acc = s.execute(select(Account).where(Account.username == uname)).scalar_one()
        vid = s.execute(select(Video).where(Video.md5_hash == md5)).scalar_one()
        post = Post(video_id=vid.id, account_id=acc.id, status=PostStatus.posted,
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


def _log_lines(factory):
    with factory() as s:
        return [row.message for row in s.execute(select(SystemLog)).scalars().all()]


def test_young_post_refreshed_by_fast_lane(factory, fast_sweep):
    """A 2h-old reel is picked up and written by fetch_fresh_analytics."""
    pid = _seed(factory, posted_hours_ago=2)
    fast_sweep({"like_count": 3, "comment_count": 1, "view_count": 182})

    out = periodic_tasks.fetch_fresh_analytics.apply().get()

    assert out == {"updated": 1, "pending": 0}
    p = _post(factory, pid)
    assert p.views_7d == 182
    assert p.last_analytics_check is not None
    assert any("Fresh analytics refresh" in m for m in _log_lines(factory))


def test_old_post_ignored_by_fast_lane(factory, fast_sweep):
    """A 30h-old reel is left to the 4h sweep — untouched here."""
    pid = _seed(factory, posted_hours_ago=30, views_7d=50)
    fast_sweep({"like_count": 0, "comment_count": 0, "view_count": 999})

    out = periodic_tasks.fetch_fresh_analytics.apply().get()

    assert out == {"updated": 0, "pending": 0}
    p = _post(factory, pid)
    assert p.views_7d == 50
    assert p.last_analytics_check is None


def test_recently_checked_young_post_skipped(factory, fast_sweep):
    """1h per-post interval: checked 30 min ago -> skip; 2h ago -> refresh."""
    fast_sweep({"like_count": 0, "comment_count": 0, "view_count": 42})

    pid_fresh = _seed(factory, posted_hours_ago=2, checked_hours_ago=0.5)
    out = periodic_tasks.fetch_fresh_analytics.apply().get()
    assert out == {"updated": 0, "pending": 0}
    assert _post(factory, pid_fresh).views_7d is None

    pid_due = _seed(factory, posted_hours_ago=3, checked_hours_ago=2)
    out = periodic_tasks.fetch_fresh_analytics.apply().get()
    assert out == {"updated": 1, "pending": 0}
    assert _post(factory, pid_due).views_7d == 42


def test_all_zero_young_post_stays_pending_in_fast_lane(factory, fast_sweep):
    """The pending guard is shared: all-zero on a young post writes nothing
    and leaves no stamp, so it is retried on the next hourly tick."""
    pid = _seed(factory, posted_hours_ago=5)
    fast_sweep({"like_count": 0, "comment_count": 0, "view_count": 0})

    out = periodic_tasks.fetch_fresh_analytics.apply().get()

    assert out == {"updated": 0, "pending": 1}
    p = _post(factory, pid)
    assert p.views_7d is None
    assert p.last_analytics_check is None


def test_fast_lane_respects_per_run_cap(factory, fast_sweep, monkeypatch):
    """At most MAX_FRESH_ANALYTICS_PER_RUN posts per hourly run — bounds IG
    API volume no matter how many young reels exist."""
    monkeypatch.setattr(
        periodic_tasks, "MAX_FRESH_ANALYTICS_PER_RUN", 2
    )
    for _ in range(4):
        _seed(factory, posted_hours_ago=2)
    fast_sweep({"like_count": 1, "comment_count": 0, "view_count": 10})

    out = periodic_tasks.fetch_fresh_analytics.apply().get()

    assert out == {"updated": 2, "pending": 0}


def test_fast_lane_failure_notifies_once(factory, fast_sweep, monkeypatch):
    """A crashing fast lane surfaces the same warning notification as the
    4h sweep instead of silently freezing view counts."""
    fast_sweep({"like_count": 0, "comment_count": 0, "view_count": 42})
    _seed(factory, posted_hours_ago=2)

    def _boom(items, now):
        raise RuntimeError("sweep exploded")

    monkeypatch.setattr(periodic_tasks, "_run_analytics_sweep", _boom)
    out = periodic_tasks.fetch_fresh_analytics.apply().get()

    assert out == {"error": "failed"}
    assert any("Analytics refresh failed" in m for m in _log_lines(factory))
