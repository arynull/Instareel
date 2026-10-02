"""All-zero analytics on a young post is "not indexed yet", not data.

Verified 2026-10-02 on the live server: IG's private API served 0/0/0
for a reel at 25h old (the app already showed views) and real numbers by
36h. The sweep used to store that zero as final and stamp
last_analytics_check, freezing a zero in the panel for hours.

Now an all-zero result on a post younger than ZERO_PENDING_HOURS (48h)
leaves the row untouched (no overwrite, no check stamp) and counts as
pending, so the post stays first in line for the next sweep. Past the
window, all-zero is written as genuine data so the sweep keeps moving.

Each test gets a fresh temp-file SQLite DB; SyncSessionLocal is patched
so the task hits the same DB. Network is fully mocked (media_info
patched, sleep/random neutered). No broker, no Redis.
"""
import datetime as dt
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


def _seed(factory, posted_hours_ago, views_7d=None, checked_hours_ago=None):
    now = dt.datetime.now(dt.timezone.utc)
    with factory() as s:
        s.add(Account(username="ana", password_enc="pw",
                      created_at=now - dt.timedelta(days=30)))
        s.add(Video(original_filename="a.mp4", raw_path="/tmp/a.mp4",
                    md5_hash="ana1", status=VideoStatus.processed))
        s.flush()
        acc = s.execute(select(Account).where(Account.username == "ana")).scalar_one()
        vid = s.execute(select(Video).where(Video.md5_hash == "ana1")).scalar_one()
        s.add(Post(video_id=vid.id, account_id=acc.id, status=PostStatus.posted,
                   posted_at=now - dt.timedelta(hours=posted_hours_ago),
                   ig_media_id="123_456", views_7d=views_7d,
                   last_analytics_check=(
                       now - dt.timedelta(hours=checked_hours_ago)
                       if checked_hours_ago is not None else None)))
        s.commit()
        return s.execute(select(Post.id)).scalar_one()


def _post(factory, post_id):
    with factory() as s:
        return s.get(Post, post_id)


def _log_lines(factory):
    with factory() as s:
        return [row.message for row in s.execute(select(SystemLog)).scalars().all()]


def test_all_zero_young_post_stays_pending(factory, fast_sweep):
    """25h-old post, IG serves 0/0/0: nothing written, no stamp, pending."""
    pid = _seed(factory, posted_hours_ago=25)
    fast_sweep({"like_count": 0, "comment_count": 0, "view_count": 0})

    out = periodic_tasks.fetch_all_analytics.apply(kwargs={"force_refresh": True}).get()

    assert out == {"updated": 0, "pending": 1}
    p = _post(factory, pid)
    assert p.views_7d is None
    assert p.last_analytics_check is None
    assert any("1 pending" in m for m in _log_lines(factory))


def test_pending_post_retried_first_next_sweep(factory, fast_sweep):
    """A pending post keeps its NULL stamp, so the next sweep picks it up
    again — convergence the moment IG serves numbers."""
    pid = _seed(factory, posted_hours_ago=25)
    fast_sweep({"like_count": 0, "comment_count": 0, "view_count": 0})
    periodic_tasks.fetch_all_analytics.apply(kwargs={"force_refresh": True}).get()

    fast_sweep({"like_count": 1, "comment_count": 0, "view_count": 164})
    out = periodic_tasks.fetch_all_analytics.apply(kwargs={"force_refresh": True}).get()

    assert out == {"updated": 1, "pending": 0}
    p = _post(factory, pid)
    assert p.views_7d == 164
    assert p.last_analytics_check is not None


def test_all_zero_old_post_written_as_data(factory, fast_sweep):
    """Past the 48h window an all-zero result is genuine data (dead reel):
    written and stamped so the sweep keeps moving."""
    pid = _seed(factory, posted_hours_ago=72, views_7d=10)
    fast_sweep({"like_count": 0, "comment_count": 0, "view_count": 0})

    out = periodic_tasks.fetch_all_analytics.apply(kwargs={"force_refresh": True}).get()

    assert out == {"updated": 1, "pending": 0}
    p = _post(factory, pid)
    assert p.views_7d == 0
    assert p.last_analytics_check is not None


def test_nonzero_young_post_written_normally(factory, fast_sweep):
    pid = _seed(factory, posted_hours_ago=2)
    fast_sweep({"like_count": 3, "comment_count": 1, "view_count": 182})

    out = periodic_tasks.fetch_all_analytics.apply(kwargs={"force_refresh": True}).get()

    assert out == {"updated": 1, "pending": 0}
    p = _post(factory, pid)
    assert p.views_7d == 182
    assert p.likes_7d == 3
    assert p.last_analytics_check is not None


def test_views_zero_but_likes_nonzero_is_data(factory, fast_sweep):
    """The guard only fires when ALL counters are zero — a reel with
    plays-not-yet-but-likes present is data, not pending."""
    pid = _seed(factory, posted_hours_ago=2)
    fast_sweep({"like_count": 5, "comment_count": 0, "view_count": 0})

    out = periodic_tasks.fetch_all_analytics.apply(kwargs={"force_refresh": True}).get()

    assert out == {"updated": 1, "pending": 0}
    assert _post(factory, pid).views_7d == 0
