"""A matched rule slot must never be skipped silently.

When check_and_post matches a slot but can't fire it (daily cap reached,
no processed video, account ineligible, …) the dashboard bell gets exactly
one warning per slot explaining why — the "upcoming" countdown reaching
zero with no follow-up was the reported bug.

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
from app.core.security import encrypt_secret
from app.database import Base
from app.models import (
    Account,
    AccountStatus,
    Notification,
    Proxy,
    ProxyProtocol,
    ScheduleRule,
    Setting,
    Video,
    VideoStatus,
)
from app.tasks import post_tasks
from app.tasks.sync_helpers import account_skip_reason


@pytest.fixture()
def factory(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SyncSessionLocal", maker)
    monkeypatch.setattr(settings, "IG_PRE_POST_DELAY_MIN", 0)
    monkeypatch.setattr(settings, "IG_PRE_POST_DELAY_MAX", 0)
    monkeypatch.setattr(settings, "MEDIA_ROOT", str(tmp_path / "media"))
    monkeypatch.setattr(settings, "SCHEDULE_GRACE_MINUTES", 5)
    return maker


def _seed(factory, tmp_path, rules, n_videos=2, posts_today=0,
          account_age_days=30, account_status=AccountStatus.active):
    (tmp_path / "media").mkdir(exist_ok=True)
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
            username="skipacc",
            password_enc=encrypt_secret("pw"),
            proxy_id=proxy.id,
            status=account_status,
            max_daily_posts=3,
            posts_today=posts_today,
            created_at=dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(days=account_age_days),
        )
        s.add(acc)
        s.flush()
        vids = []
        for i in range(n_videos):
            raw = tmp_path / f"sraw{i}.mp4"
            raw.write_bytes(b"fake-video")
            thumb = tmp_path / f"sthumb{i}.jpg"
            thumb.write_bytes(b"fake-thumb")
            v = Video(
                original_filename=f"sv{i}.mp4",
                raw_path=str(raw),
                processed_path=str(raw),
                thumbnail_path=str(thumb),
                md5_hash=f"smd5-{i}",
                status=VideoStatus.processed,
            )
            s.add(v)
            vids.append(v)
        s.flush()
        for r in rules:
            rule = ScheduleRule(
                name=r.get("name", "skip-rule"),
                day_of_week=r.get("dow", -1),
                hour=r["hour"],
                minute=r["minute"],
                account_id=acc.id,
                is_active=True,
            )
            s.add(rule)
            s.flush()
        s.add(Setting(key="post_jitter_minutes", value="0", category="schedule"))
        s.commit()
        return acc.id


def _freeze(monkeypatch, at):
    monkeypatch.setattr(sync_helpers, "_schedule_now", lambda: at)


def _tick(factory, monkeypatch):
    fired = []
    monkeypatch.setattr(
        post_tasks,
        "execute_post",
        type("FakeTask", (), {"delay": staticmethod(lambda pid: fired.append(pid))}),
    )
    return post_tasks.check_and_post.apply().get(), fired


def _skips(factory):
    with factory() as s:
        return (
            s.execute(
                select(Notification)
                .where(Notification.ntype == "slot_skipped")
                .order_by(Notification.id)
            )
            .scalars()
            .all()
        )


NOW = dt.datetime(2026, 9, 27, 12, 0, tzinfo=dt.timezone.utc)


def test_cap_skip_notifies_once_per_slot(factory, tmp_path, monkeypatch):
    """Warm-up cap reached: one warning naming the limit, deduped per slot."""
    _seed(
        factory, tmp_path,
        [{"hour": NOW.hour, "minute": NOW.minute}],
        n_videos=2, posts_today=1, account_age_days=3,  # warm-up: cap = 1/day
    )
    _freeze(monkeypatch, NOW)
    res, _ = _tick(factory, monkeypatch)
    assert res["slots_matched"] == 1
    assert res["created"] == 0
    # A second tick inside the grace window must not duplicate it.
    _tick(factory, monkeypatch)
    notes = _skips(factory)
    assert len(notes) == 1
    n = notes[0]
    assert n.severity.value == "warning"
    assert "daily post limit" in n.message
    assert "warm-up" in n.message
    assert n.dedup_key.startswith("slot_skip:")


def test_empty_queue_skip_notifies(factory, tmp_path, monkeypatch):
    _seed(factory, tmp_path, [{"hour": NOW.hour, "minute": NOW.minute}], n_videos=0)
    _freeze(monkeypatch, NOW)
    res, _ = _tick(factory, monkeypatch)
    assert res["created"] == 0
    notes = _skips(factory)
    assert len(notes) == 1
    assert "no processed video" in notes[0].message


def test_no_skip_notification_when_slot_fires(factory, tmp_path, monkeypatch):
    _seed(factory, tmp_path, [{"hour": NOW.hour, "minute": NOW.minute}], n_videos=2)
    _freeze(monkeypatch, NOW)
    res, fired = _tick(factory, monkeypatch)
    assert res["created"] == 1
    assert len(fired) == 1
    assert _skips(factory) == []


def test_account_skip_reason_cap_without_warmup(factory, tmp_path):
    acc_id = _seed(factory, tmp_path, [], posts_today=3, account_age_days=30)
    with factory() as s:
        reason = account_skip_reason(s, acc_id)
    assert reason is not None
    assert "daily post limit" in reason
    assert "3/3" in reason
    assert "warm-up" not in reason


def test_account_skip_reason_cooldown_and_status(factory, tmp_path):
    acc_id = _seed(factory, tmp_path, [], account_age_days=30,
                   account_status=AccountStatus.cooldown)
    with factory() as s:
        acc = s.get(Account, acc_id)
        acc.cooldown_until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)
        s.commit()
        reason = account_skip_reason(s, acc_id)
    assert reason is not None
    assert "cooldown" in reason


def test_account_skip_reason_none_when_eligible(factory, tmp_path):
    acc_id = _seed(factory, tmp_path, [], posts_today=0, account_age_days=30)
    with factory() as s:
        assert account_skip_reason(s, acc_id) is None
