"""Grace-window tests for the scheduler.

Covers: a slot stays fireable for SCHEDULE_GRACE_MINUTES after its minute
(outage recovery), exactly-once firing per slot (no double-post inside the
window, across status changes, or on concurrent ticks), grace=0 legacy
behavior, midnight crossover, rule deactivation mid-window, account
eligibility changes mid-window, and pinned-video wait→fire.

Each test gets a fresh temp-file SQLite DB; SyncSessionLocal is patched so
the tasks hit the same DB. No network, no broker, no Redis.
"""
import datetime as dt

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

import app.database as database
import app.tasks.sync_helpers as sync_helpers
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
from app.tasks.sync_helpers import as_aware_utc

# 2026-09-26 is a Saturday (matches the standing 'Sat 21:00' rule).
SAT_21_00 = dt.datetime(2026, 9, 26, 21, 0, tzinfo=dt.timezone.utc)


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
    monkeypatch.setattr(settings, "SCHEDULE_GRACE_MINUTES", 5)
    return maker


def _seed(factory, tmp_path, rules, n_videos=2, posts_today=0):
    """Healthy proxy + active account + processed videos + explicit rules.

    rules: list of dicts with hour/minute, optional dow (-1 = daily),
    name, and pin_index (index into the seeded videos for a pinned rule).
    """
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
            username="graceacc",
            password_enc=encrypt_secret("pw"),
            proxy_id=proxy.id,
            status=AccountStatus.active,
            max_daily_posts=3,
            posts_today=posts_today,
            created_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30),
        )
        s.add(acc)
        s.flush()
        vids = []
        for i in range(n_videos):
            raw = tmp_path / f"graw{i}.mp4"
            raw.write_bytes(b"fake-video")
            thumb = tmp_path / f"gthumb{i}.jpg"
            thumb.write_bytes(b"fake-thumb")
            v = Video(
                original_filename=f"gv{i}.mp4",
                raw_path=str(raw),
                processed_path=str(raw),
                thumbnail_path=str(thumb),
                md5_hash=f"gmd5-{i}",
                status=VideoStatus.processed,
            )
            s.add(v)
            vids.append(v)
        s.flush()
        rule_ids = []
        for r in rules:
            pin = r.get("pin_index")
            rule = ScheduleRule(
                name=r.get("name", "grule"),
                day_of_week=r.get("dow", -1),
                hour=r["hour"],
                minute=r["minute"],
                account_id=acc.id,
                is_active=True,
                pinned_video_id=vids[pin].id if pin is not None else None,
            )
            s.add(rule)
            s.flush()
            rule_ids.append(rule.id)
        s.add(Setting(key="post_jitter_minutes", value="0", category="schedule"))
        s.commit()
        return acc.id, [v.id for v in vids], rule_ids


def _freeze(monkeypatch, at):
    """Pin the scheduler's wall-clock (SCHEDULE_TZ) to a fixed instant."""
    monkeypatch.setattr(sync_helpers, "_schedule_now", lambda: at)


def _tick(factory, monkeypatch):
    """Run one beat tick with a fake executor; return (result, fired_ids)."""
    fired = []

    def _apply_async(args=None, countdown=0, **kw):
        # Mirrors the production call: execute_post.apply_async(args=[post_id], countdown=...).
        fired.append(args[0] if args else None)

    def _delay(pid):
        # The tick backstop dispatches unstamped/stale rows via .delay().
        fired.append(pid)

    monkeypatch.setattr(
        post_tasks,
        "execute_post",
        type("FakeTask", (), {"apply_async": staticmethod(_apply_async), "delay": staticmethod(_delay)}),
    )
    return post_tasks.check_and_post.apply().get(), fired


def _posts(factory):
    with factory() as s:
        return s.execute(select(Post).order_by(Post.id)).scalars().all()


def test_exact_minute_fires_and_records_slot(factory, tmp_path, monkeypatch):
    _seed(factory, tmp_path, [{"hour": 21, "minute": 0}])
    _freeze(monkeypatch, SAT_21_00)
    result, fired = _tick(factory, monkeypatch)
    assert result["created"] == 1
    assert len(fired) == 1
    posts = _posts(factory)
    assert len(posts) == 1
    assert as_aware_utc(posts[0].slot_for) == SAT_21_00


def test_grace_refires_after_outage(factory, tmp_path, monkeypatch):
    # Beat was down 21:00–21:02 (no tick ran). The first tick at 21:03 must
    # still post the 21:00 slot — a few minutes late, not lost.
    _seed(factory, tmp_path, [{"hour": 21, "minute": 0}])
    _freeze(monkeypatch, SAT_21_00 + dt.timedelta(minutes=3))
    result, _ = _tick(factory, monkeypatch)
    assert result["created"] == 1
    assert as_aware_utc(_posts(factory)[0].slot_for) == SAT_21_00


def test_no_double_fire_within_window(factory, tmp_path, monkeypatch):
    _seed(factory, tmp_path, [{"hour": 21, "minute": 0}], n_videos=3)
    created_total = 0
    for m in range(0, 6):  # ticks at 21:00 … 21:05, all inside the window
        _freeze(monkeypatch, SAT_21_00 + dt.timedelta(minutes=m))
        result, _ = _tick(factory, monkeypatch)
        created_total += result["created"]
    assert created_total == 1
    posts = _posts(factory)
    assert len(posts) == 1
    assert as_aware_utc(posts[0].slot_for) == SAT_21_00


def test_slot_stays_fired_after_posted(factory, tmp_path, monkeypatch):
    # Once the post went live, already_scheduled() can't see it anymore —
    # only the slot record prevents a duplicate.
    _seed(factory, tmp_path, [{"hour": 21, "minute": 0}], n_videos=2)
    _freeze(monkeypatch, SAT_21_00)
    _tick(factory, monkeypatch)
    with factory() as s:
        p = s.execute(select(Post)).scalars().one()
        p.status = PostStatus.posted
        p.posted_at = dt.datetime.now(dt.timezone.utc)
        s.commit()
    _freeze(monkeypatch, SAT_21_00 + dt.timedelta(minutes=4))
    result, _ = _tick(factory, monkeypatch)
    assert result["created"] == 0
    assert len(_posts(factory)) == 1


def test_failed_slot_does_not_refire(factory, tmp_path, monkeypatch):
    # A failed slot is owned by the retry/reprocess machinery — re-firing it
    # would risk duplicate content on false-negative failures and failure
    # loops on bad videos. One slot → at most one post row, ever.
    _seed(factory, tmp_path, [{"hour": 21, "minute": 0}], n_videos=2)
    _freeze(monkeypatch, SAT_21_00)
    _tick(factory, monkeypatch)
    with factory() as s:
        p = s.execute(select(Post)).scalars().one()
        p.status = PostStatus.failed
        p.fail_reason = "boom"
        s.commit()
    _freeze(monkeypatch, SAT_21_00 + dt.timedelta(minutes=4))
    result, _ = _tick(factory, monkeypatch)
    assert result["created"] == 0
    assert len(_posts(factory)) == 1


def test_grace_zero_is_exact_minute(factory, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SCHEDULE_GRACE_MINUTES", 0)
    _seed(factory, tmp_path, [{"hour": 21, "minute": 0}])
    _freeze(monkeypatch, SAT_21_00 + dt.timedelta(minutes=1))
    result, _ = _tick(factory, monkeypatch)
    assert result["created"] == 0
    assert _posts(factory) == []


def test_slot_older_than_grace_ignored(factory, tmp_path, monkeypatch):
    _seed(factory, tmp_path, [{"hour": 21, "minute": 0}])
    _freeze(monkeypatch, SAT_21_00 + dt.timedelta(minutes=6))  # grace is 5
    result, _ = _tick(factory, monkeypatch)
    assert result["created"] == 0
    assert _posts(factory) == []


def test_midnight_crossover(factory, tmp_path, monkeypatch):
    # Daily rule at 00:01; the tick runs at 00:03 — the slot belongs to the
    # new day and its day_of_week still matches.
    _seed(factory, tmp_path, [{"hour": 0, "minute": 1}])
    _freeze(monkeypatch, dt.datetime(2026, 9, 27, 0, 3, tzinfo=dt.timezone.utc))
    result, _ = _tick(factory, monkeypatch)
    assert result["created"] == 1
    expected = dt.datetime(2026, 9, 27, 0, 1, tzinfo=dt.timezone.utc)
    assert as_aware_utc(_posts(factory)[0].slot_for) == expected


def test_rule_deactivated_mid_window(factory, tmp_path, monkeypatch):
    _, _, (rule_id,) = _seed(factory, tmp_path, [{"hour": 21, "minute": 0}])
    # Outage at 21:00 (no tick ran), then the user disables the rule.
    with factory() as s:
        s.get(ScheduleRule, rule_id).is_active = False
        s.commit()
    _freeze(monkeypatch, SAT_21_00 + dt.timedelta(minutes=3))
    result, _ = _tick(factory, monkeypatch)
    assert result["created"] == 0
    assert _posts(factory) == []


def test_ineligible_then_eligible(factory, tmp_path, monkeypatch):
    # Account at its daily cap at 21:00 → the slot stays armed → the cap
    # frees at 21:02 → the slot fires late instead of being lost.
    acc_id, _, _ = _seed(factory, tmp_path, [{"hour": 21, "minute": 0}], posts_today=3)
    _freeze(monkeypatch, SAT_21_00)
    result, _ = _tick(factory, monkeypatch)
    assert result["created"] == 0
    with factory() as s:
        s.get(Account, acc_id).posts_today = 0
        s.commit()
    _freeze(monkeypatch, SAT_21_00 + dt.timedelta(minutes=2))
    result, _ = _tick(factory, monkeypatch)
    assert result["created"] == 1
    assert as_aware_utc(_posts(factory)[0].slot_for) == SAT_21_00


def test_pinned_video_waits_then_fires_in_window(factory, tmp_path, monkeypatch):
    # Pinned video still processing at slot time → "wait", the slot stays
    # armed → the video finishes at 21:02 → the slot fires.
    _, vids, (rule_id,) = _seed(
        factory, tmp_path, [{"hour": 21, "minute": 0, "pin_index": 0}], n_videos=1
    )
    with factory() as s:
        s.get(Video, vids[0]).status = VideoStatus.processing
        s.commit()
    _freeze(monkeypatch, SAT_21_00)
    result, _ = _tick(factory, monkeypatch)
    assert result["created"] == 0  # waiting — not lost
    with factory() as s:
        s.get(Video, vids[0]).status = VideoStatus.processed
        s.commit()
    _freeze(monkeypatch, SAT_21_00 + dt.timedelta(minutes=2))
    result, _ = _tick(factory, monkeypatch)
    assert result["created"] == 1
    assert as_aware_utc(_posts(factory)[0].slot_for) == SAT_21_00
    with factory() as s:  # one-shot pinned rule retires after firing
        assert s.get(ScheduleRule, rule_id).is_active is False


def test_concurrent_tick_race_is_swallowed(factory, tmp_path, monkeypatch):
    # Two ticks racing on the same rule slot: both pass the pre-checks, the
    # loser's INSERT hits uq_posts_account_slot. It must be swallowed —
    # one post total, and the tick completes normally.
    acc_id, vids, rule_ids = _seed(factory, tmp_path, [{"hour": 21, "minute": 0}], n_videos=2)
    with factory() as s:  # the "winner" tick's post, already committed…
        s.add(
            Post(
                video_id=vids[0],
                account_id=acc_id,
                rule_id=rule_ids[0],
                caption="c",
                hashtags="",
                status=PostStatus.posted,
                posted_at=dt.datetime.now(dt.timezone.utc),
                scheduled_for=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2),
                slot_for=SAT_21_00,
            )
        )
        s.commit()
    # …but this tick's pre-checks can't see it (simulating the race).
    monkeypatch.setattr(sync_helpers, "slot_already_fired", lambda s, r, slot: False)
    _freeze(monkeypatch, SAT_21_00 + dt.timedelta(minutes=2))
    result, _ = _tick(factory, monkeypatch)
    assert "error" not in result
    assert result["created"] == 0
    assert len(_posts(factory)) == 1


def test_due_rule_slots_oldest_first(factory, tmp_path, monkeypatch):
    _seed(
        factory,
        tmp_path,
        [
            {"hour": 21, "minute": 0, "name": "r0"},
            {"hour": 21, "minute": 2, "name": "r2"},
        ],
    )
    _freeze(monkeypatch, SAT_21_00 + dt.timedelta(minutes=4))
    with factory() as s:
        pairs = sync_helpers.due_rule_slots(s)
    assert [(r.name, sl) for r, sl in pairs] == [
        ("r0", SAT_21_00),
        ("r2", SAT_21_00 + dt.timedelta(minutes=2)),
    ]


def test_slot_unique_per_account_not_global(factory, tmp_path):
    # Same slot on two different accounts → both rows allowed.
    with factory() as s:
        accs = []
        for u in ("u1", "u2"):
            a = Account(
                username=u,
                password_enc=encrypt_secret("x"),
                status=AccountStatus.active,
                max_daily_posts=3,
                posts_today=0,
                created_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1),
            )
            s.add(a)
            accs.append(a)
        s.flush()
        v = Video(
            original_filename="x.mp4",
            raw_path="r",
            processed_path="r",
            thumbnail_path="t",
            md5_hash="h",
            status=VideoStatus.processed,
        )
        s.add(v)
        s.flush()
        for a in accs:
            s.add(
                Post(
                    video_id=v.id,
                    account_id=a.id,
                    caption="",
                    hashtags="",
                    status=PostStatus.scheduled,
                    slot_for=SAT_21_00,
                )
            )
        s.commit()  # must not raise
        assert s.execute(select(func.count(Post.id))).scalar() == 2


def test_duplicate_slot_for_same_account_raises(factory, tmp_path):
    # Same (account, slot, rule) twice → the backstop constraint fires.
    with factory() as s:
        a = Account(
            username="u3",
            password_enc=encrypt_secret("x"),
            status=AccountStatus.active,
            max_daily_posts=3,
            posts_today=0,
            created_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1),
        )
        s.add(a)
        s.flush()
        v = Video(
            original_filename="y.mp4",
            raw_path="r",
            processed_path="r",
            thumbnail_path="t",
            md5_hash="h2",
            status=VideoStatus.processed,
        )
        s.add(v)
        r = ScheduleRule(name="rr", day_of_week=-1, hour=21, minute=0, account_id=a.id, is_active=True)
        s.add(r)
        s.flush()
        for _ in range(2):
            s.add(
                Post(
                    video_id=v.id,
                    account_id=a.id,
                    rule_id=r.id,
                    caption="",
                    hashtags="",
                    status=PostStatus.scheduled,
                    slot_for=SAT_21_00,
                )
            )
        with pytest.raises(IntegrityError):
            s.commit()


def test_same_slot_different_rules_allowed(factory, tmp_path):
    # Same (account, slot) from two different rules → both rows allowed.
    # This is the two-rules-at-4:30 case: each rule owns its slot.
    with factory() as s:
        a = Account(
            username="u4",
            password_enc=encrypt_secret("x"),
            status=AccountStatus.active,
            max_daily_posts=3,
            posts_today=0,
            created_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1),
        )
        s.add(a)
        s.flush()
        v = Video(
            original_filename="z.mp4",
            raw_path="r",
            processed_path="r",
            thumbnail_path="t",
            md5_hash="h3",
            status=VideoStatus.processed,
        )
        s.add(v)
        rules = []
        for i in (1, 2):
            r = ScheduleRule(name=f"rz{i}", day_of_week=-1, hour=21, minute=0, account_id=a.id, is_active=True)
            s.add(r)
            rules.append(r)
        s.flush()
        for r in rules:
            s.add(
                Post(
                    video_id=v.id,
                    account_id=a.id,
                    rule_id=r.id,
                    caption="",
                    hashtags="",
                    status=PostStatus.scheduled,
                    slot_for=SAT_21_00,
                )
            )
        s.commit()  # must not raise
        assert s.execute(select(func.count(Post.id))).scalar() == 2


# 2026-09-30 is a Wednesday; the seeded rules are daily (dow=-1) so any day works.
WED_04_30 = dt.datetime(2026, 9, 30, 4, 30, tzinfo=dt.timezone.utc)


def test_two_rules_same_slot_both_fire(factory, tmp_path, monkeypatch):
    # Regression: two rules at the same minute for the same account — each
    # must fire its own post. The second used to be swallowed silently as
    # "slot already fired" and never posted.
    _, _, rule_ids = _seed(
        factory, tmp_path,
        [{"hour": 4, "minute": 30, "name": "r1"}, {"hour": 4, "minute": 30, "name": "r2"}],
        n_videos=3,
    )
    _freeze(monkeypatch, WED_04_30)
    result, fired = _tick(factory, monkeypatch)
    assert result["created"] == 2
    assert len(fired) == 2
    posts = _posts(factory)
    assert len(posts) == 2
    assert {p.rule_id for p in posts} == set(rule_ids)
    assert posts[0].video_id != posts[1].video_id  # different videos, no double-post
    for p in posts:
        assert as_aware_utc(p.slot_for) == WED_04_30


def test_same_slot_pair_no_refire_across_ticks(factory, tmp_path, monkeypatch):
    # Each rule still fires exactly once: later ticks inside the grace
    # window must not create duplicates for either rule.
    _seed(
        factory, tmp_path,
        [{"hour": 4, "minute": 30, "name": "r1"}, {"hour": 4, "minute": 30, "name": "r2"}],
        n_videos=3,
    )
    _freeze(monkeypatch, WED_04_30)
    result, _ = _tick(factory, monkeypatch)
    assert result["created"] == 2
    for m in range(1, 6):
        _freeze(monkeypatch, WED_04_30 + dt.timedelta(minutes=m))
        result, _ = _tick(factory, monkeypatch)
        assert result["created"] == 0
    assert len(_posts(factory)) == 2


def test_legacy_null_rule_id_row_still_blocks(factory, tmp_path, monkeypatch):
    # Rows created before migration 0019 have rule_id NULL and keep the old
    # account-wide exactly-once semantics: they block every rule at that
    # account+slot. (status=posted so already_scheduled can't be the blocker.)
    acc_id, vids, _ = _seed(factory, tmp_path, [{"hour": 4, "minute": 30}], n_videos=2)
    with factory() as s:
        s.add(
            Post(
                video_id=vids[0],
                account_id=acc_id,
                rule_id=None,
                caption="",
                hashtags="",
                status=PostStatus.posted,
                posted_at=WED_04_30,
                scheduled_for=WED_04_30,
                slot_for=WED_04_30,
            )
        )
        s.commit()
    _freeze(monkeypatch, WED_04_30)
    result, _ = _tick(factory, monkeypatch)
    assert result["created"] == 0
    assert len(_posts(factory)) == 1


def test_already_scheduled_exempts_same_slot(factory, tmp_path, monkeypatch):
    # A sibling rule's post for the identical slot must not trip the
    # ±10 min guard; a post for any other slot (or a manual post) still does.
    from app.tasks import sync_helpers as sched

    _, _, rule_ids = _seed(factory, tmp_path, [{"hour": 4, "minute": 30}], n_videos=2)
    now = dt.datetime.now(dt.timezone.utc)
    with factory() as s:
        v = s.execute(select(Video)).scalars().first()
        rule = s.get(ScheduleRule, rule_ids[0])
        s.add(
            Post(
                video_id=v.id,
                account_id=rule.account_id,
                rule_id=rule.id,
                caption="",
                hashtags="",
                status=PostStatus.scheduled,
                scheduled_for=now,
                slot_for=WED_04_30,
            )
        )
        s.commit()
        assert sched.already_scheduled(s, rule, WED_04_30) is False
        assert sched.already_scheduled(s, rule, WED_04_30 + dt.timedelta(minutes=5)) is True
        assert sched.already_scheduled(s, rule) is True  # no exemption without slot
