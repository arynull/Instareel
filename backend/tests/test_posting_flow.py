"""Integration tests for the scheduler → executor path.

Covers the previously untested core: check_and_post (rule → scheduled post)
and execute_post (scheduled → posted via Instagram), with a fake IG client —
no network, no broker, no Redis.

Each test gets a fresh temp-file SQLite DB; SyncSessionLocal is patched so
both the tasks and their helpers (logging, thumbnails) hit the same DB.
"""
import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.database as database
from app.config import settings
from app.core.security import encrypt_secret
from app.database import Base
from app.models import (
    Account,
    AccountStatus,
    Post,
    PostStatus,
    Proxy,
    ProxyProtocol,
    ScheduleRule,
    Setting,
    Video,
    VideoStatus,
)
from app.tasks import post_tasks


@pytest.fixture()
def factory(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SyncSessionLocal", maker)
    # Deterministic scheduler: no jitter, no pre-post delay, tmp media root.
    monkeypatch.setattr(settings, "IG_PRE_POST_DELAY_MIN", 0)
    monkeypatch.setattr(settings, "IG_PRE_POST_DELAY_MAX", 0)
    monkeypatch.setattr(settings, "MEDIA_ROOT", str(tmp_path / "media"))
    return maker


def _seed(factory, tmp_path, n_videos=1):
    """One healthy manual proxy, one active account, processed video(s),
    one rule due right now, zero post jitter."""
    (tmp_path / "media").mkdir(exist_ok=True)
    vids = []
    for i in range(n_videos):
        raw = tmp_path / f"raw{i}.mp4"
        raw.write_bytes(b"fake-video")
        thumb = tmp_path / f"thumb{i}.jpg"
        thumb.write_bytes(b"fake-thumb")
        vids.append((str(raw), str(thumb)))
    with factory() as s:
        proxy = Proxy(
            url="http://127.0.0.1:8080",
            protocol=ProxyProtocol.http,
            is_healthy=True,
            is_active=True,
            source="manual",
            country="US",
        )
        s.add(proxy)
        s.flush()
        acc = Account(
            username="testacc",
            password_enc=encrypt_secret("pw"),
            proxy_id=proxy.id,
            status=AccountStatus.active,
            max_daily_posts=3,
            posts_today=0,
            created_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30),
        )
        s.add(acc)
        s.flush()
        videos = []
        for i, (raw, thumb) in enumerate(vids):
            v = Video(
                original_filename=f"v{i}.mp4",
                raw_path=raw,
                processed_path=raw,
                thumbnail_path=thumb,
                md5_hash=f"md5-{i}",
                status=VideoStatus.processed,
            )
            s.add(v)
            videos.append(v)
        s.flush()
        now = dt.datetime.now(dt.timezone.utc)
        rule = ScheduleRule(
            name="r1",
            day_of_week=-1,
            hour=now.hour,
            minute=now.minute,
            account_id=acc.id,
            is_active=True,
        )
        s.add(rule)
        s.add(Setting(key="post_jitter_minutes", value="0", category="schedule"))
        s.commit()
        return acc.id, [v.id for v in videos], rule.id


class _FakeIG:
    """Stand-in for InstagramService — records uploads, never touches network."""

    instances = []

    def __init__(self, proxy_url=None, session_path=None):
        self.uploads = []
        _FakeIG.instances.append(self)

    def upload_reel(self, username, password, video_path, caption, trial=False,
                    trial_strategy="manual", thumbnail_path=None):
        self.uploads.append(
            {"username": username, "video_path": video_path, "caption": caption,
             "trial": trial, "thumbnail_path": thumbnail_path}
        )
        return ("mid_1", "https://instagram.com/p/mid_1", "")


class _FailingIG(_FakeIG):
    def upload_reel(self, *a, **k):
        self.uploads.append({"failed": True})
        return ("", "", "auth: invalid credentials")


@pytest.fixture(autouse=True)
def _reset_fake_ig():
    _FakeIG.instances.clear()
    yield
    _FakeIG.instances.clear()


def _patch_ig(monkeypatch, cls=_FakeIG):
    import app.services.instagram_service as ig_module

    monkeypatch.setattr(ig_module, "InstagramService", cls)


def test_check_and_post_creates_scheduled_post_and_fires(factory, tmp_path, monkeypatch):
    _seed(factory, tmp_path)
    fired = []
    monkeypatch.setattr(post_tasks, "execute_post",
                        type("FakeTask", (), {"delay": staticmethod(lambda pid: fired.append(pid))}))

    result = post_tasks.check_and_post.apply().get()

    assert result["created"] == 1, result
    assert len(fired) == 1
    with factory() as s:
        post = s.get(Post, fired[0])
        assert post is not None
        assert post.status == PostStatus.scheduled
        assert post.video.status == VideoStatus.processed  # untouched until executed


def test_execute_post_success_marks_posted_and_archives(factory, tmp_path, monkeypatch):
    acc_id, (vid,), _ = _seed(factory, tmp_path)
    _patch_ig(monkeypatch)
    with factory() as s:
        post = Post(video_id=vid, account_id=acc_id, caption="cap", hashtags="#t",
                    status=PostStatus.scheduled,
                    scheduled_for=dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1))
        s.add(post)
        s.commit()
        pid = post.id

    result = post_tasks.execute_post.apply(args=[pid]).get()

    assert result["status"] == "posted", result
    assert result["url"] == "https://instagram.com/p/mid_1"
    assert len(_FakeIG.instances) == 1
    up = _FakeIG.instances[0].uploads[0]
    assert up["username"] == "testacc" and up["caption"] == "cap\n#t"
    with factory() as s:
        post = s.get(Post, pid)
        assert post.status == PostStatus.posted
        assert post.ig_media_id == "mid_1"
        assert post.posted_at is not None
        assert s.get(Video, vid).status == VideoStatus.posted  # archived — never reposted
        acc = s.get(Account, acc_id)
        assert acc.posts_today == 1 and acc.total_posts == 1
        assert acc.last_post is not None


def test_execute_post_is_idempotent(factory, tmp_path, monkeypatch):
    """Second execution must not re-upload (single-flight claim)."""
    acc_id, (vid,), _ = _seed(factory, tmp_path)
    _patch_ig(monkeypatch)
    with factory() as s:
        post = Post(video_id=vid, account_id=acc_id, status=PostStatus.scheduled,
                    scheduled_for=dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1))
        s.add(post)
        s.commit()
        pid = post.id

    assert post_tasks.execute_post.apply(args=[pid]).get()["status"] == "posted"
    second = post_tasks.execute_post.apply(args=[pid]).get()

    assert second["status"] == "already-handled"
    assert sum(len(i.uploads) for i in _FakeIG.instances) == 1


def test_execute_post_auth_failure_marks_failed(factory, tmp_path, monkeypatch):
    acc_id, (vid,), _ = _seed(factory, tmp_path)
    _patch_ig(monkeypatch, _FailingIG)
    with factory() as s:
        post = Post(video_id=vid, account_id=acc_id, status=PostStatus.scheduled,
                    scheduled_for=dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1))
        s.add(post)
        s.commit()
        pid = post.id

    result = post_tasks.execute_post.apply(args=[pid]).get()

    assert result["status"] == "failed", result
    with factory() as s:
        post = s.get(Post, pid)
        assert post.status == PostStatus.failed
        assert "auth" in (post.fail_reason or "")
        # One failure must not park the account (health guard parks on streaks).
        assert s.get(Account, acc_id).status == AccountStatus.active
        # Video stays processed so a later retry/manual post can still use it.
        assert s.get(Video, vid).status == VideoStatus.processed


def test_full_tick_posts_end_to_end(factory, tmp_path, monkeypatch):
    """check_and_post → execute_post wired together (delay bridged in-process)."""
    import app.tasks.post_tasks as pt_mod

    _seed(factory, tmp_path)
    _patch_ig(monkeypatch)

    real_task = pt_mod.execute_post
    fired: list[int] = []
    monkeypatch.setattr(
        pt_mod, "execute_post",
        type("Bridge", (), {"delay": staticmethod(fired.append)}),
    )

    tick = pt_mod.check_and_post.apply().get()
    assert tick["created"] == 1 and len(fired) == 1

    # Swap the bridge back for the real executor and run the fired id.
    monkeypatch.setattr(pt_mod, "execute_post", real_task)
    out = real_task.apply(args=[fired[0]]).get()
    assert out["status"] == "posted"


class TestReapStalePosting:
    def _posting_post(self, factory, minutes_ago, md5):
        import datetime as dt

        from sqlalchemy import update

        with factory() as s:
            acc = Account(
                username=f"reapacc{md5}", password_enc=encrypt_secret("pw"),
                status=AccountStatus.active)
            s.add(acc)
            s.flush()
            v = Video(original_filename="r.mp4", raw_path="/tmp/r.mp4",
                      file_size=1, md5_hash=md5)
            s.add(v)
            s.flush()
            p = Post(video_id=v.id, account_id=acc.id, status=PostStatus.posting)
            s.add(p)
            s.flush()
            pid = p.id
            s.execute(
                update(Post).where(Post.id == pid).values(
                    updated_at=dt.datetime.now(dt.timezone.utc)
                    - dt.timedelta(minutes=minutes_ago)))
            s.commit()
        return pid

    def test_reaps_stale_posting_row(self, factory):
        from app.models import Notification, NotificationSeverity
        from app.tasks.periodic_tasks import reap_stale_posting

        pid = self._posting_post(factory, minutes_ago=60, md5="reap1")
        res = reap_stale_posting.run()
        assert res == {"reaped": 1}
        with factory() as s:
            p = s.get(Post, pid)
            assert p.status == PostStatus.failed
            assert "Stale 'posting'" in (p.fail_reason or "")
            n = s.query(Notification).filter_by(
                dedup_key=f"stale-posting:{pid}").one()
            assert n.severity == NotificationSeverity.CRITICAL

    def test_fresh_posting_row_untouched(self, factory):
        from app.tasks.periodic_tasks import reap_stale_posting

        pid = self._posting_post(factory, minutes_ago=5, md5="reap2")
        res = reap_stale_posting.run()
        assert res == {"reaped": 0}
        with factory() as s:
            assert s.get(Post, pid).status == PostStatus.posting

    def test_reaped_video_becomes_reschedulable(self, factory):
        # After the reaper marks the row failed, find_blocking_sibling must
        # no longer see a stale 'posting' sibling for the same video.
        from app.tasks import sync_helpers as sched
        from app.tasks.periodic_tasks import reap_stale_posting

        pid = self._posting_post(factory, minutes_ago=60, md5="reap3")
        reap_stale_posting.run()
        with factory() as s:
            p = s.get(Post, pid)
            assert sched.find_blocking_sibling(s, 999999, p.video_id) is None
