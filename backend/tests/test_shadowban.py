"""Shadowban watch + Instagram action-block handling.

Covers:
- _classify: feedback_required -> action_blocked (never challenge), priority
  over other markers, case-insensitivity
- _feed_with_retry: action_blocked returns immediately (no 5s sleep+retry);
  transient blips still retried once; persistent generic errors keep old behavior
- is_views_collapsed: pure verdict incl. edge cases
- scan_shadowban_sync (beat task): pause on collapse, all no-op cases,
  recovery + auto-resume, manual-cooldown protection, episode handling,
  disabled scan, garbage settings, never-raises
- execute_post: feedback_required from upload -> cooldown + proxy rotation +
  dedicated warning notification; no short retry; no proxy health penalty;
  no post_failed critical for the same event

Hermetic: DB is temp SQLite, Redis faked, IG faked, no network.
"""
import datetime as dt
import time

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
    Notification,
    Post,
    PostStatus,
    Proxy,
    ProxyProtocol,
    Setting,
    Video,
    VideoStatus,
)
from app.services import instagram_service as ig_module
from app.services.account_health import is_views_collapsed, scan_shadowban_sync
from app.tasks.sync_helpers import as_aware_utc
from app.tasks import post_tasks


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


def _now():
    return dt.datetime.now(dt.timezone.utc)


# ------------------------------------------------------- classification ---


@pytest.mark.parametrize(
    "message",
    [
        "feedback_required",
        "Feedback_required: this action was blocked",
        "ClientError: feedback_required - Please try again later",
        "FEEDBACK_REQUIRED",
    ],
)
def test_feedback_required_classifies_action_blocked(message):
    assert ig_module._classify(Exception(message)) == "action_blocked"


def test_feedback_required_never_counts_as_challenge():
    """The old bug: feedback_required was a challenge marker, demanding a
    manual session refresh for a restriction that lifts on its own."""
    assert "feedback_required" not in ig_module.CHALLENGE_MARKERS
    assert ig_module._classify(Exception("feedback_required")) == "action_blocked"


def test_action_block_wins_over_other_markers():
    assert (
        ig_module._classify(Exception("feedback_required + challenge_required"))
        == "action_blocked"
    )


@pytest.mark.parametrize(
    "message,expected",
    [
        ("challenge_required", "challenge"),
        ("checkpoint_required: verify", "challenge"),
        ("login_required", "login_required"),
        ("Login Required", "login_required"),
        ("throttled: slow down", "throttled"),
        ("Please try again later", "throttled"),
        ("rate limit exceeded", "throttled"),
        ("some random 500", "generic"),
    ],
)
def test_other_kinds_unchanged(message, expected):
    assert ig_module._classify(Exception(message)) == expected


class _FlakyClient:
    """get_timeline_feed fails `failures` times, then succeeds."""

    def __init__(self, failures, message="boom"):
        self.failures = failures
        self.message = message
        self.calls = 0

    def get_timeline_feed(self):
        self.calls += 1
        if self.calls <= self.failures:
            raise Exception(self.message)
        return [{"id": 1}]


def test_feed_with_retry_returns_action_block_immediately():
    cl = _FlakyClient(99, "feedback_required: action blocked")
    start = time.monotonic()
    ok, kind = ig_module._feed_with_retry(cl)
    elapsed = time.monotonic() - start
    assert (ok, kind) == (False, "action_blocked")
    assert cl.calls == 1, "must not retry an action block"
    assert elapsed < 4, f"took {elapsed:.1f}s — the 5s sleep must be skipped"


def test_feed_with_retry_returns_challenge_immediately():
    cl = _FlakyClient(99, "challenge_required")
    ok, kind = ig_module._feed_with_retry(cl)
    assert (ok, kind) == (False, "challenge")
    assert cl.calls == 1


def test_feed_with_retry_still_retries_transient_blips():
    cl = _FlakyClient(1, "connection reset by peer")
    ok, kind = ig_module._feed_with_retry(cl)
    assert (ok, kind) == (True, "")
    assert cl.calls == 2


def test_feed_with_retry_persistent_generic_error_keeps_old_behavior():
    cl = _FlakyClient(99, "weird 500")
    start = time.monotonic()
    ok, kind = ig_module._feed_with_retry(cl)
    elapsed = time.monotonic() - start
    assert (ok, kind) == (False, "generic")
    assert cl.calls == 2
    assert elapsed >= 4.5, "generic errors still get the one 5s retry"


# ------------------------------------------------------ collapse verdict ---


@pytest.mark.parametrize(
    "recent,baseline,ratio,expected",
    [
        ([10, 12, 8], 1000.0, 0.10, True),      # 10 < 100 -> collapsed
        ([150, 160, 155], 1000.0, 0.10, False),   # 155 > 100 -> fine
        ([99, 101, 100], 1000.0, 0.10, False),   # boundary: not strictly below
        ([99, 99, 99], 1000.0, 0.10, True),      # strictly below ratio*baseline
        ([10, 12, 8], 1000.0, 0.01, False),      # ratio 1%: 10 !< 10
        ([0, 0, 5], 1000.0, 0.10, True),         # zero views -> collapsed
        ([10, 12, 8], 50.0, 0.10, False),        # low baseline: 10 !< 5
        ([], 1000.0, 0.10, False),               # no recent data -> no verdict
        ([10, 12], 0.0, 0.10, False),            # no baseline -> no verdict
        ([10, 12], -5.0, 0.10, False),          # degenerate baseline
        ([10, 12], 1000.0, 0.0, False),         # degenerate ratio
        ([10, 12], 1000.0, -1.0, False),        # degenerate ratio
        # median (not mean): one viral recent post must not mask a collapse
        ([10, 12, 5000], 1000.0, 0.10, True),
        # ...and one flop must not fake a collapse on a healthy account
        ([900, 950, 5], 1000.0, 0.10, False),
    ],
)
def test_is_views_collapsed(recent, baseline, ratio, expected):
    assert is_views_collapsed(recent, baseline, ratio) is expected


# ------------------------------------------------------------- scan db ---


def _mk_video(s, i=0):
    v = Video(
        original_filename=f"v{i}.mp4",
        raw_path=f"/tmp/v{i}.mp4",
        processed_path=f"/tmp/v{i}.mp4",
        md5_hash=f"md5-{i}-{time.monotonic_ns()}",
        status=VideoStatus.processed,
    )
    s.add(v)
    s.flush()
    return v.id


def _seed_scan_account(
    factory,
    *,
    username="shadow1",
    status=AccountStatus.active,
    n_baseline=8,
    baseline_views=1000,
    n_recent=3,
    recent_views=10,
    recent_age_h=48,
):
    """Baseline: settled posts 10+ days old. Recent: posts `recent_age_h` old."""
    now = _now()
    with factory() as s:
        acc = Account(
            username=username,
            password_enc=encrypt_secret("pw"),
            status=status,
            max_daily_posts=3,
            created_at=now - dt.timedelta(days=90),
        )
        s.add(acc)
        s.flush()
        for i in range(n_baseline):
            s.add(
                Post(
                    account_id=acc.id,
                    video_id=_mk_video(s, i),
                    status=PostStatus.posted,
                    posted_at=now - dt.timedelta(days=10 + i),
                    views_7d=baseline_views,
                    views_24h=baseline_views,
                )
            )
        for i in range(n_recent):
            s.add(
                Post(
                    account_id=acc.id,
                    video_id=_mk_video(s, 100 + i),
                    status=PostStatus.posted,
                    posted_at=now - dt.timedelta(hours=recent_age_h),
                    views_7d=recent_views * 2,
                    views_24h=recent_views,
                )
            )
        s.commit()
        return acc.id


def _set_setting(factory, key, value):
    with factory() as s:
        s.merge(Setting(key=key, value=value, category="scheduler"))
        s.commit()


def _account(factory, acc_id):
    with factory() as s:
        return s.get(Account, acc_id)


def _notifications(factory, **filters):
    with factory() as s:
        q = select(Notification)
        for k, v in filters.items():
            q = q.where(getattr(Notification, k) == v)
        return s.execute(q).scalars().all()


def test_scan_pauses_collapsed_account(factory):
    acc_id = _seed_scan_account(factory)
    with factory() as s:
        out = scan_shadowban_sync(s)
    assert out and out[0]["verdict"] == "paused"
    assert out[0]["account_id"] == acc_id
    acc = _account(factory, acc_id)
    assert acc.status == AccountStatus.cooldown
    assert acc.cooldown_until is not None
    assert abs((as_aware_utc(acc.cooldown_until) - _now()).total_seconds() - 48 * 3600) < 120
    notes = _notifications(factory, dedup_key=f"shadowban:{acc_id}")
    assert len(notes) == 1
    assert notes[0].severity.value == "critical"
    assert notes[0].ntype == "possible_shadowban"
    assert notes[0].read_at is None


def test_scan_healthy_account_noop(factory):
    _seed_scan_account(factory, recent_views=900)
    with factory() as s:
        out = scan_shadowban_sync(s)
    assert out == []
    notes = _notifications(factory)
    assert notes == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"n_baseline": 4},                       # too few settled posts
        {"baseline_views": 50},                  # historically low reach
        {"n_recent": 2},                         # too few recent posts
        {"recent_age_h": 12},                    # views not settled yet
        {"recent_age_h": 100},                   # stale signal
        {"n_baseline": 0, "n_recent": 0},        # brand-new account
    ],
)
def test_scan_insufficient_data_noop(factory, kwargs):
    _seed_scan_account(factory, **kwargs)
    with factory() as s:
        assert scan_shadowban_sync(s) == []
    assert _notifications(factory) == []


def test_scan_disabled_setting_noop(factory):
    _seed_scan_account(factory)
    _set_setting(factory, "shadowban_scan_enabled", "false")
    with factory() as s:
        assert scan_shadowban_sync(s) == []
    assert _account(factory, 1).status == AccountStatus.active
    assert _notifications(factory) == []


def test_scan_garbage_settings_fall_back_to_defaults(factory):
    _seed_scan_account(factory)
    _set_setting(factory, "shadowban_collapse_ratio", "not-a-number")
    _set_setting(factory, "shadowban_pause_hours", "-5")
    _set_setting(factory, "shadowban_min_baseline_views", "")
    with factory() as s:
        out = scan_shadowban_sync(s)
    assert out and out[0]["verdict"] == "paused"  # defaults applied, no crash
    acc = _account(factory, 1)
    assert abs((as_aware_utc(acc.cooldown_until) - _now()).total_seconds() - 48 * 3600) < 120


def test_scan_custom_pause_hours_honored(factory):
    _seed_scan_account(factory)
    _set_setting(factory, "shadowban_pause_hours", "12")
    with factory() as s:
        scan_shadowban_sync(s)
    acc = _account(factory, 1)
    assert abs((as_aware_utc(acc.cooldown_until) - _now()).total_seconds() - 12 * 3600) < 120


def test_scan_custom_collapse_ratio_honored(factory):
    # 30% of baseline with ratio=0.10 -> no collapse; with ratio=0.5 -> collapse
    _seed_scan_account(factory, baseline_views=1000, recent_views=300)
    with factory() as s:
        assert scan_shadowban_sync(s) == []
    _set_setting(factory, "shadowban_collapse_ratio", "0.5")
    with factory() as s:
        out = scan_shadowban_sync(s)
    assert out and out[0]["verdict"] == "paused"


def test_scan_skips_non_active_non_cooldown_accounts(factory):
    for status in (
        AccountStatus.challenge_required,
        AccountStatus.banned,
        AccountStatus.disabled,
    ):
        _seed_scan_account(factory, username=f"u_{status.value}", status=status)
    with factory() as s:
        assert scan_shadowban_sync(s) == []
    assert _notifications(factory) == []


def test_scan_dedups_while_unread(factory):
    _seed_scan_account(factory)
    with factory() as s:
        scan_shadowban_sync(s)
    with factory() as s:
        out = scan_shadowban_sync(s)  # still collapsed, still active? no — paused
    assert out == []  # paused account isn't re-paused
    assert len(_notifications(factory, dedup_key="shadowban:1")) == 1


def test_scan_recovery_resumes_account(factory):
    acc_id = _seed_scan_account(factory)
    with factory() as s:
        scan_shadowban_sync(s)
    assert _account(factory, acc_id).status == AccountStatus.cooldown
    # reach recovers: recent views back to normal
    with factory() as s:
        s.execute(
            Post.__table__.update()
            .where(Post.account_id == acc_id)
            .where(Post.posted_at > _now() - dt.timedelta(days=7))
            .values(views_24h=950)
        )
        s.commit()
    with factory() as s:
        out = scan_shadowban_sync(s)
    assert out and out[0]["verdict"] == "recovered"
    acc = _account(factory, acc_id)
    assert acc.status == AccountStatus.active
    assert acc.cooldown_until is None
    # the alert auto-resolved...
    down = _notifications(factory, dedup_key=f"shadowban:{acc_id}")
    assert len(down) == 1 and down[0].read_at is not None
    # ...and a recovery note was posted
    rec = _notifications(factory, ntype="shadowban_recovered")
    assert len(rec) == 1 and rec[0].severity.value == "success"


def test_scan_recovery_never_overrides_manual_cooldown(factory):
    """If the admin shortened/changed the cooldown by hand, the scan must
    not silently reactivate the account when views recover."""
    acc_id = _seed_scan_account(factory)
    with factory() as s:
        scan_shadowban_sync(s)
        note = _notifications(factory, dedup_key=f"shadowban:{acc_id}")[0]
        created = note.created_at
    # admin intervenes: 5h cooldown for another reason (not our 48h pause)
    with factory() as s:
        acc = s.get(Account, acc_id)
        acc.cooldown_until = _now() + dt.timedelta(hours=5)
        s.commit()
    assert abs((as_aware_utc(created) + dt.timedelta(hours=48) - (_now() + dt.timedelta(hours=5))).total_seconds()) > 3600
    # reach recovers
    with factory() as s:
        s.execute(
            Post.__table__.update()
            .where(Post.account_id == acc_id)
            .where(Post.posted_at > _now() - dt.timedelta(days=7))
            .values(views_24h=950)
        )
        s.commit()
    with factory() as s:
        out = scan_shadowban_sync(s)
    assert out and out[0]["verdict"] == "alert_resolved"
    acc = _account(factory, acc_id)
    assert acc.status == AccountStatus.cooldown  # untouched
    assert acc.cooldown_until is not None


def test_scan_respects_admin_override_during_episode(factory):
    """Admin reactivates mid-episode while views are still collapsed: the
    scan must not immediately re-pause (their call), and must not spam."""
    acc_id = _seed_scan_account(factory)
    with factory() as s:
        scan_shadowban_sync(s)
    with factory() as s:
        acc = s.get(Account, acc_id)
        acc.status = AccountStatus.active
        acc.cooldown_until = None
        s.commit()
    with factory() as s:
        assert scan_shadowban_sync(s) == []
    assert _account(factory, acc_id).status == AccountStatus.active
    assert len(_notifications(factory, dedup_key=f"shadowban:{acc_id}")) == 1


def test_scan_new_episode_after_expiry_repauses(factory):
    """Pause expired, views STILL collapsed afterwards: fresh episode ->
    fresh pause + fresh alert (old one closed first, not dedup-swallowed)."""
    acc_id = _seed_scan_account(factory)
    with factory() as s:
        scan_shadowban_sync(s)
    with factory() as s:
        # re-fetch inside THIS session: the helper's session is closed and
        # its objects are detached — mutating them would not persist.
        note = (
            s.execute(
                select(Notification).where(
                    Notification.dedup_key == f"shadowban:{acc_id}"
                )
            )
            .scalars()
            .first()
        )
        # pretend the episode is old: detected 72h ago (pause was 48h)
        note.created_at = _now() - dt.timedelta(hours=72)
        acc = s.get(Account, acc_id)
        acc.status = AccountStatus.active  # pause expired / admin reactivated
        acc.cooldown_until = None
        s.commit()
    with factory() as s:
        out = scan_shadowban_sync(s)
    assert out and out[0]["verdict"] == "paused"
    notes = _notifications(factory, dedup_key=f"shadowban:{acc_id}")
    assert len(notes) == 2
    assert notes[0].read_at is not None  # old episode closed
    assert notes[1].read_at is None      # fresh alert


def test_scan_never_raises_on_broken_rows(factory):
    _seed_scan_account(factory)
    with factory() as s:
        # a post with NULL posted_at must not kill the scan
        s.add(Post(account_id=1, video_id=_mk_video(s, 999), status=PostStatus.posted))
        s.commit()
    with factory() as s:
        out = scan_shadowban_sync(s)  # must not raise
    assert out and out[0]["verdict"] == "paused"


def test_scan_task_registered_and_on_fast_lane():
    import app.tasks.celery_app as celery_app_module

    assert "tasks.account_tasks.scan_shadowban" in celery_app_module.celery.tasks
    schedule = celery_app_module.celery.conf.beat_schedule
    assert "shadowban-scan" in schedule
    routes = celery_app_module.celery.conf.task_routes
    default = celery_app_module.celery.conf.task_default_queue
    task = schedule["shadowban-scan"]["task"]
    assert routes.get(task, {}).get("queue", default) == "fast"


# ------------------------------------------------- execute_post: block ---


class _BlockIG:
    """upload_reel always hits Instagram's action block."""

    instances = []

    def __init__(self, proxy_url=None, session_path=None):
        _BlockIG.instances.append(self)

    def upload_reel(self, *a, **k):
        return ("", "", "action_blocked: feedback_required - try again later")


@pytest.fixture(autouse=True)
def _reset_block_ig():
    _BlockIG.instances.clear()
    yield
    _BlockIG.instances.clear()


def _seed_posting(factory, tmp_path, n_proxies=2):
    (tmp_path / "media").mkdir(exist_ok=True)
    raw = tmp_path / "raw.mp4"
    raw.write_bytes(b"fake-video")
    thumb = tmp_path / "thumb.jpg"
    thumb.write_bytes(b"fake-thumb")
    with factory() as s:
        proxies = []
        for i in range(n_proxies):
            p = Proxy(
                url=f"http://127.0.0.1:808{i}",
                protocol=ProxyProtocol.http,
                is_healthy=True,
                is_active=True,
                source="manual",
                country="US",
                last_checked=_now(),  # "proven" -> pick_spare_proxy may use it
            )
            s.add(p)
            proxies.append(p)
        s.flush()
        acc = Account(
            username="blockacc",
            password_enc=encrypt_secret("pw"),
            proxy_id=proxies[0].id,
            status=AccountStatus.active,
            max_daily_posts=3,
            created_at=_now() - dt.timedelta(days=90),
        )
        s.add(acc)
        s.flush()
        v = Video(
            original_filename="v.mp4",
            raw_path=str(raw),
            processed_path=str(raw),
            thumbnail_path=str(thumb),
            md5_hash="md5-block",
            status=VideoStatus.processed,
        )
        s.add(v)
        s.flush()
        post = Post(
            video_id=v.id,
            account_id=acc.id,
            status=PostStatus.scheduled,
            scheduled_for=_now() - dt.timedelta(minutes=1),
        )
        s.add(post)
        s.commit()
        return acc.id, post.id, [p.id for p in proxies]


def test_execute_post_action_block_cools_down_account(factory, tmp_path, monkeypatch):
    monkeypatch.setattr(ig_module, "InstagramService", _BlockIG)
    acc_id, pid, (p1, p2) = _seed_posting(factory, tmp_path)
    result = post_tasks.execute_post.apply(args=[pid]).get()

    assert result["status"] == "failed", result
    assert result["error"].startswith("action_blocked")
    with factory() as s:
        post = s.get(Post, pid)
        assert post.status == PostStatus.failed
        assert post.retry_count == 1  # marked once — NOT a celery retry loop
        acc = s.get(Account, acc_id)
        assert acc.status == AccountStatus.cooldown  # not challenge_required!
        assert acc.cooldown_until is not None
        assert abs((as_aware_utc(acc.cooldown_until) - _now()).total_seconds() - 24 * 3600) < 120
        assert acc.proxy_id == p2  # rotated to the spare
        assert acc.posts_today == 0  # failure doesn't consume quota


def test_execute_post_action_block_notifies_once(factory, tmp_path, monkeypatch):
    monkeypatch.setattr(ig_module, "InstagramService", _BlockIG)
    acc_id, pid, _ = _seed_posting(factory, tmp_path)
    post_tasks.execute_post.apply(args=[pid]).get()

    notes = _notifications(factory, ntype="action_blocked")
    assert len(notes) == 1
    assert notes[0].severity.value == "warning"
    assert "blockacc" in notes[0].title
    # the generic post_failed critical must NOT fire for the same event
    assert _notifications(factory, ntype="post_failed") == []
    day = _now().strftime("%Y-%m-%d")
    assert notes[0].dedup_key == f"action_block:blockacc:{day}"


def test_execute_post_action_block_no_spare_proxy(factory, tmp_path, monkeypatch):
    monkeypatch.setattr(ig_module, "InstagramService", _BlockIG)
    acc_id, pid, (p1,) = _seed_posting(factory, tmp_path, n_proxies=1)
    post_tasks.execute_post.apply(args=[pid]).get()
    with factory() as s:
        acc = s.get(Account, acc_id)
        assert acc.status == AccountStatus.cooldown  # still cools down
        assert acc.proxy_id == p1  # nothing to rotate to


def test_execute_post_action_block_no_proxy_health_penalty(factory, tmp_path, monkeypatch):
    """An action block is account-level, not egress-IP: the proxy's health
    streak must not take the hit (unlike throttled/proxy errors)."""
    monkeypatch.setattr(ig_module, "InstagramService", _BlockIG)
    acc_id, pid, (p1, p2) = _seed_posting(factory, tmp_path)
    with factory() as s:
        before = s.get(Proxy, p1).fail_count
    post_tasks.execute_post.apply(args=[pid]).get()
    with factory() as s:
        assert s.get(Proxy, p1).fail_count == before


def test_execute_post_action_block_custom_cooldown(factory, tmp_path, monkeypatch):
    monkeypatch.setattr(ig_module, "InstagramService", _BlockIG)
    _set_setting(factory, "action_block_cooldown_hours", "6")
    acc_id, pid, _ = _seed_posting(factory, tmp_path)
    post_tasks.execute_post.apply(args=[pid]).get()
    with factory() as s:
        acc = s.get(Account, acc_id)
        assert abs((as_aware_utc(acc.cooldown_until) - _now()).total_seconds() - 6 * 3600) < 120


def test_execute_post_action_block_does_not_retry(factory, tmp_path, monkeypatch):
    """action_blocked must not take the throttled/login short-retry path
    (2/4/8-min celery retries) — it won't clear in minutes."""
    monkeypatch.setattr(ig_module, "InstagramService", _BlockIG)
    acc_id, pid, _ = _seed_posting(factory, tmp_path)
    result = post_tasks.execute_post.apply(args=[pid]).get()
    # a celery retry would raise, not return a failure dict
    assert result["status"] == "failed"
    assert len(_BlockIG.instances) == 1
    with factory() as s:
        post = s.get(Post, pid)
        # still 'scheduled' would mean parked-for-retry; must be failed
        assert post.status == PostStatus.failed
