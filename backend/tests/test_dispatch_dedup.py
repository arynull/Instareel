"""Dispatch dedup: exactly-once handoff from the fast-lane tick to the slow lane.

Covers:
- fresh posts are ETA-dispatched (apply_async with countdown) at creation,
  preserving the second-resolution jitter (no minute-tick rounding)
- the row is stamped dispatched_at so the tick backstop never double-enqueues
- the backstop is atomic (UPDATE ... WHERE ... RETURNING): concurrent ticks
  can't stamp the same row twice
- stale stamps (older than dispatch_stale_minutes) are re-dispatched, not lost
- unstamped manual/API posts are picked up by the backstop
- broker publish failure clears the stamp -> next tick retries within a minute
- garbage dispatch_stale_minutes falls back to the 30-min default

Hermetic: temp SQLite, fake IG, execute_post bridged (captured, never run).
"""
import datetime as dt

import pytest
from sqlalchemy import create_engine, select
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
from app.tasks import post_tasks as pt_mod


@pytest.fixture()
def factory(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SyncSessionLocal", maker)
    monkeypatch.setattr(settings, "IG_PRE_POST_DELAY_MIN", 0)
    monkeypatch.setattr(settings, "IG_PRE_POST_DELAY_MAX", 0)
    monkeypatch.setattr(settings, "MEDIA_ROOT", str(tmp_path / "media"))
    return maker


class _DispatchBridge:
    """Captures delay/apply_async instead of touching the broker."""

    def __init__(self, fail_eta=False):
        self.delayed = []
        self.eta = []  # (post_id, countdown)
        self.fail_eta = fail_eta

    def delay(self, pid):
        self.delayed.append(pid)

    def apply_async(self, args=None, countdown=0, **kwargs):
        if self.fail_eta:
            raise ConnectionError("broker down")
        self.eta.append((args[0], countdown))
        return None


@pytest.fixture()
def bridge(monkeypatch):
    b = _DispatchBridge()
    monkeypatch.setattr(pt_mod, "execute_post", b)
    return b


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _seed_rule(factory, tmp_path):
    """One account + video + rule due right now, zero jitter."""
    (tmp_path / "media").mkdir(exist_ok=True)
    raw = tmp_path / "raw.mp4"
    raw.write_bytes(b"fake-video")
    with factory() as s:
        s.add(
            Setting(key="post_jitter_minutes", value="0", category="scheduler")
        )
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
            username="dispatchacc",
            password_enc=encrypt_secret("pw"),
            proxy_id=proxy.id,
            status=AccountStatus.active,
            max_daily_posts=10,
            created_at=_now() - dt.timedelta(days=90),
        )
        s.add(acc)
        s.flush()
        v = Video(
            original_filename="v.mp4",
            raw_path=str(raw),
            processed_path=str(raw),
            md5_hash="md5-dispatch",
            status=VideoStatus.processed,
        )
        s.add(v)
        s.flush()
        now = _now()
        rule = ScheduleRule(
            name="r1",
            day_of_week=-1,
            hour=now.hour,
            minute=now.minute,
            account_id=acc.id,
        )
        s.add(rule)
        s.commit()
        return acc.id, v.id, rule.id


def _due_posts(factory):
    with factory() as s:
        return s.execute(select(Post)).scalars().all()


def test_fresh_post_eta_dispatched_with_countdown(factory, tmp_path, bridge):
    _seed_rule(factory, tmp_path)
    tick = pt_mod.check_and_post.apply().get()

    assert tick["created"] == 1, tick
    # ETA path taken (not the backstop): exactly one apply_async ...
    assert len(bridge.eta) == 1
    pid, countdown = bridge.eta[0]
    assert countdown == 0  # jitter=0 in this fixture
    # ... and the backstop did NOT also enqueue it
    assert bridge.delayed == []
    assert tick["fired"] == 0  # backstop found nothing to do
    posts = _due_posts(factory)
    assert len(posts) == 1
    assert posts[0].id == pid
    assert posts[0].dispatched_at is not None


def test_eta_countdown_matches_jitter_seconds(factory, tmp_path, bridge, monkeypatch):
    _seed_rule(factory, tmp_path)
    with factory() as s:
        s.merge(Setting(key="post_jitter_minutes", value="5", category="scheduler"))
        s.commit()
    monkeypatch.setattr(pt_mod.random, "randint", lambda a, b: 137)  # 137s jitter
    pt_mod.check_and_post.apply().get()
    assert len(bridge.eta) == 1
    _, countdown = bridge.eta[0]
    assert countdown == pytest.approx(137, abs=5)


def test_second_tick_does_not_reenqueue(factory, tmp_path, bridge):
    """The slow worker hasn't claimed the post yet (still scheduled): the
    next tick must not enqueue it a second time."""
    _seed_rule(factory, tmp_path)
    pt_mod.check_and_post.apply().get()
    assert len(bridge.eta) == 1

    tick2 = pt_mod.check_and_post.apply().get()
    assert tick2["created"] == 0  # slot already fired
    assert len(bridge.eta) == 1, "no second ETA dispatch"
    assert bridge.delayed == [], "backstop must skip the stamped row"


def test_backstop_picks_up_unstamped_manual_post(factory, tmp_path, bridge):
    """Manual/API posts (dispatched_at NULL) are dispatched by the backstop
    with a plain delay — the ETA path is only for tick-created posts."""
    acc_id, vid, _ = _seed_rule(factory, tmp_path)
    with factory() as s:
        # deactivate the rule so the tick creates nothing itself
        rule = s.execute(select(ScheduleRule)).scalars().first()
        rule.is_active = False
        p = Post(
            video_id=vid,
            account_id=acc_id,
            status=PostStatus.scheduled,
            scheduled_for=_now() - dt.timedelta(minutes=2),
            dispatched_at=None,
        )
        s.add(p)
        s.commit()
        pid = p.id
    tick = pt_mod.check_and_post.apply().get()
    assert bridge.eta == []
    assert bridge.delayed == [pid]
    assert tick["fired"] == 1
    with factory() as s:
        assert s.get(Post, pid).dispatched_at is not None


def test_backstop_is_atomic_across_ticks(factory, tmp_path, bridge):
    """Two back-to-back ticks racing on the same unstamped row: the atomic
    UPDATE ... WHERE stamps it once — the loser finds nothing to dispatch."""
    acc_id, vid, _ = _seed_rule(factory, tmp_path)
    with factory() as s:
        s.execute(select(ScheduleRule)).scalars().first().is_active = False
        p = Post(
            video_id=vid, account_id=acc_id, status=PostStatus.scheduled,
            scheduled_for=_now() - dt.timedelta(minutes=2),
        )
        s.add(p)
        s.commit()
        pid = p.id
    pt_mod.check_and_post.apply().get()
    assert bridge.delayed == [pid]
    pt_mod.check_and_post.apply().get()
    assert bridge.delayed == [pid], "second tick must not re-dispatch"


def test_stale_stamp_is_redispatched_not_lost(factory, tmp_path, bridge):
    """A dispatch stamp older than dispatch_stale_minutes means the ETA
    publish was lost (broker hiccup): re-dispatch instead of losing the post."""
    acc_id, vid, _ = _seed_rule(factory, tmp_path)
    with factory() as s:
        s.execute(select(ScheduleRule)).scalars().first().is_active = False
        p = Post(
            video_id=vid, account_id=acc_id, status=PostStatus.scheduled,
            scheduled_for=_now() - dt.timedelta(minutes=2),
            dispatched_at=_now() - dt.timedelta(minutes=120),
        )
        s.add(p)
        s.commit()
        pid = p.id
    tick = pt_mod.check_and_post.apply().get()
    assert bridge.delayed == [pid]
    assert tick["fired"] == 1


def test_fresh_stamp_is_not_redispatched(factory, tmp_path, bridge):
    """A recent stamp (post waiting in the slow queue) is left alone."""
    acc_id, vid, _ = _seed_rule(factory, tmp_path)
    with factory() as s:
        s.execute(select(ScheduleRule)).scalars().first().is_active = False
        p = Post(
            video_id=vid, account_id=acc_id, status=PostStatus.scheduled,
            scheduled_for=_now() - dt.timedelta(minutes=2),
            dispatched_at=_now() - dt.timedelta(minutes=5),
        )
        s.add(p)
        s.commit()
    tick = pt_mod.check_and_post.apply().get()
    assert bridge.delayed == []
    assert tick["fired"] == 0


def test_garbage_stale_setting_falls_back_to_default(factory, tmp_path, bridge):
    with factory() as s:
        s.merge(
            Setting(key="dispatch_stale_minutes", value="not-a-number",
                    category="scheduler")
        )
        s.commit()
    acc_id, vid, _ = _seed_rule(factory, tmp_path)
    with factory() as s:
        s.execute(select(ScheduleRule)).scalars().first().is_active = False
        p = Post(
            video_id=vid, account_id=acc_id, status=PostStatus.scheduled,
            scheduled_for=_now() - dt.timedelta(minutes=2),
            dispatched_at=_now() - dt.timedelta(minutes=31),  # > default 30
        )
        s.add(p)
        s.commit()
        pid = p.id
    pt_mod.check_and_post.apply().get()
    assert bridge.delayed == [pid]


def test_eta_failure_falls_back_to_backstop(factory, tmp_path, monkeypatch):
    """Broker down at creation: the stamp is cleared so the backstop
    re-dispatches within the same tick (not 30 min later via the stale
    window) — the post is never stranded."""
    b = _DispatchBridge(fail_eta=True)
    monkeypatch.setattr(pt_mod, "execute_post", b)
    _seed_rule(factory, tmp_path)
    pt_mod.check_and_post.apply().get()

    assert b.eta == []  # publish raised
    posts = _due_posts(factory)
    assert len(posts) == 1
    pid = posts[0].id
    # stamp was cleared by _dispatch_eta, then the same-tick backstop
    # atomically re-stamped and delay()-dispatched it
    assert b.delayed == [pid]
    assert posts[0].dispatched_at is not None


def test_non_due_post_never_dispatched(factory, tmp_path, bridge):
    acc_id, vid, _ = _seed_rule(factory, tmp_path)
    with factory() as s:
        s.execute(select(ScheduleRule)).scalars().first().is_active = False
        s.add(Post(
            video_id=vid, account_id=acc_id, status=PostStatus.scheduled,
            scheduled_for=_now() + dt.timedelta(minutes=10),
        ))
        s.commit()
    tick = pt_mod.check_and_post.apply().get()
    assert bridge.delayed == [] and bridge.eta == []
    assert tick["fired"] == 0
