"""Account health guard: one score per account + automatic parking.

Why this exists: failure signals are scattered (throttle cooldowns,
proxy streaks, challenge flags, trial 500s). This module folds them into
a single 0-100 score with a level + human reasons, and parks accounts
that fail repeatedly *for any reason* — not just the two kinds
execute_post already handles (challenge/throttled).

Design rules (anti-interference):
- Scoring core is pure (compute_health): no DB, fully unit-tested.
- Row aggregation is pure too (summarize_statuses), shared by the sync
  worker path and the async API path so the two can never drift apart.
- maybe_park_account_sync only mutates when status is "active" — it
  never fights an existing cooldown/challenge/ban, and it never commits
  (the caller owns the transaction).
- Parking reuses the existing cooldown machinery (status + 6h
  cooldown_until) so every existing reader (eligible_account, UI,
  realtime) behaves unchanged.
"""
import datetime as dt

HEALTHY_MIN = 70
WATCH_MIN = 40
PARK_AFTER_CONSECUTIVE_FAILS = 5
PARK_HOURS = 6
STREAK_WINDOW_DAYS = 7


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def compute_health(
    *,
    status: str,
    cooldown_until: dt.datetime | None = None,
    proxy_fail_count: int = 0,
    proxy_healthy: bool = True,
    posts_today: int = 0,
    daily_cap: int = 3,
    fail_streak: int = 0,
    failed_7d: int = 0,
    posted_7d: int = 0,
    now: dt.datetime | None = None,
) -> dict:
    """Pure 0-100 score. Returns {score, level, reasons}."""
    now = now or _now()
    score = 100
    reasons: list[str] = []

    if status == "banned":
        return {"score": 0, "level": "critical", "reasons": ["account is banned"]}
    if status == "challenge_required":
        return {"score": 10, "level": "critical", "reasons": ["login challenge pending — re-verify the session"]}
    if status == "disabled":
        return {"score": 0, "level": "critical", "reasons": ["account disabled by admin"]}
    if status == "cooldown":
        ts = cooldown_until
        if ts is not None and ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
        if ts is not None and ts <= now:
            # Expired but not yet cleared (no sweeper resets it): the next
            # tick treats it as eligible, so don't scar the score — say so.
            score -= 10
            reasons.append("cooldown expired — resumes on next tick")
        else:
            score -= 60
            reasons.append("cooling down after throttling")
            if ts is not None and ts > now:
                reasons.append(f"cooldown lifts in {int((ts - now).total_seconds() // 3600)}h")

    if not proxy_healthy or proxy_fail_count >= 5:
        score -= 25
        reasons.append(f"egress proxy failing ({proxy_fail_count} consecutive fails)")
    elif proxy_fail_count >= 2:
        score -= 10
        reasons.append(f"egress proxy flaky ({proxy_fail_count} recent fails)")

    if fail_streak >= PARK_AFTER_CONSECUTIVE_FAILS:
        score -= 30
        reasons.append(f"{fail_streak} consecutive post failures")
    elif fail_streak >= 3:
        score -= 10 * fail_streak
        reasons.append(f"{fail_streak} consecutive post failures")

    total_7d = failed_7d + posted_7d
    if total_7d >= 3 and failed_7d / total_7d > 0.5:
        score -= 20
        reasons.append(f"failure rate {failed_7d}/{total_7d} in the last 7 days")

    if posts_today >= daily_cap:
        reasons.append("daily post cap reached (recovers tomorrow)")

    score = max(0, min(100, score))
    level = "healthy" if score >= HEALTHY_MIN else ("watch" if score >= WATCH_MIN else "critical")
    if not reasons:
        reasons.append("no warning signals")
    return {"score": score, "level": level, "reasons": reasons}


def summarize_statuses(statuses: list) -> tuple[int, int, int]:
    """(consecutive-failure streak, failed count, posted count) over post
    statuses newest-first. Pure — shared by the sync worker path and the
    async API path so the two can never drift apart."""
    from app.models import PostStatus

    streak = 0
    for st in statuses:
        if st == PostStatus.failed:
            streak += 1
        else:
            break
    return (
        streak,
        sum(1 for st in statuses if st == PostStatus.failed),
        sum(1 for st in statuses if st == PostStatus.posted),
    )


def _consecutive_failures(session, account_id: int, now: dt.datetime) -> tuple[int, int, int]:
    """(streak, failed_7d, posted_7d) from recent posts. Sync, worker-safe."""
    from sqlalchemy import desc, select

    from app.models import Post

    since = now - dt.timedelta(days=STREAK_WINDOW_DAYS)
    rows = (
        session.execute(
            select(Post.status)
            .where(Post.account_id == account_id, Post.created_at >= since)
            .order_by(desc(Post.id))
            .limit(30)
        )
    ).all()
    return summarize_statuses([st for (st,) in rows])


def maybe_park_account_sync(session, account, now: dt.datetime | None = None) -> bool:
    """Park an active account after a generic failure streak.

    Returns True when it parked. Only touches active accounts; sets
    status=cooldown + 6h cooldown_until. No commit, no publish, no log —
    the caller (execute_post.touch_account) already commits, logs the
    note and publishes account_status_change.
    """
    from app.models import AccountStatus

    now = now or _now()
    if account.status != AccountStatus.active:
        return False
    streak, _, _ = _consecutive_failures(session, account.id, now)
    # +1: the failure currently being recorded isn't a Post row yet.
    if streak + 1 < PARK_AFTER_CONSECUTIVE_FAILS:
        return False
    account.status = AccountStatus.cooldown
    account.cooldown_until = now + dt.timedelta(hours=PARK_HOURS)
    return True

async def evaluate_account(db, account_id: int) -> dict | None:
    """Full health dict for one account over the caller's async session.

    Reads through the passed session (test/prod identical) — the only
    other DB user is the worker's auto-park above. Row aggregation goes
    through summarize_statuses, shared with the sync path.
    """
    from sqlalchemy import desc, select

    from app.models import Account, Post, Proxy
    from app.tasks.sync_helpers import effective_max_posts

    now = _now()
    account = await db.get(Account, account_id)
    if account is None:
        return None
    proxy = await db.get(Proxy, account.proxy_id) if account.proxy_id else None
    since = now - dt.timedelta(days=STREAK_WINDOW_DAYS)
    rows = (
        await db.execute(
            select(Post.status)
            .where(Post.account_id == account_id, Post.created_at >= since)
            .order_by(desc(Post.id))
            .limit(30)
        )
    ).all()
    streak, failed_7d, posted_7d = summarize_statuses([st for (st,) in rows])
    from app.models import Setting as SettingModel
    from app.tasks.sync_helpers import WARMUP_DAYS as _WARMUP_DEFAULT

    wrow = (
        (await db.execute(select(SettingModel).where(SettingModel.key == "warmup_days")))
        .scalars()
        .first()
    )
    try:
        wdays = max(0, int(wrow.value)) if wrow and wrow.value else _WARMUP_DEFAULT
    except (TypeError, ValueError):
        wdays = _WARMUP_DEFAULT
    out = compute_health(
        status=account.status.value,
        cooldown_until=account.cooldown_until,
        proxy_fail_count=proxy.fail_count if proxy else 0,
        proxy_healthy=bool(proxy.is_healthy) if proxy else True,
        posts_today=account.posts_today,
        daily_cap=effective_max_posts(account.created_at, account.max_daily_posts, now, warmup_days=wdays),
        fail_streak=streak,
        failed_7d=failed_7d,
        posted_7d=posted_7d,
        now=now,
    )
    out["account_id"] = account.id
    out["username"] = account.username
    out["fail_streak"] = streak
    return out


# ---- Shadowban / action-block watch ----
#
# Instagram never tells you "this account is shadowbanned" — the symptom is
# a views collapse: recent reels get a small fraction of the account's usual
# reach. This scan (beat: tasks.account_tasks.scan_shadowban, every 6h)
# turns that symptom into an automatic pause + notification, and resumes
# the account when reach recovers. Design rules, same as the parking guard
# above: the verdict core is pure (is_views_collapsed), pausing reuses the
# existing cooldown machinery (no scheduler changes needed), and the scan
# never raises.

#: Baseline window: posted reels older than this (settled posts).
SHADOWBAN_BASELINE_DAYS = 7
#: Recent window: posted reels aged between these bounds. Younger than 24h
#: the views haven't settled (false positives); older than 72h the signal
#: is stale.
SHADOWBAN_RECENT_MIN_HOURS = 24
SHADOWBAN_RECENT_MAX_HOURS = 72
#: Minimum settled posts for a trustworthy baseline.
SHADOWBAN_MIN_BASELINE_POSTS = 5


def _median(values: "list[float]") -> float:
    s = sorted(values)
    n = len(s)
    if n == 0:
        return 0.0
    mid = n // 2
    return float(s[mid]) if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def is_views_collapsed(
    recent_views: "list[int]", baseline_median: float, ratio: float
) -> bool:
    """Pure views-collapse verdict.

    ``recent_views`` — views_24h of the account's settled recent reels;
    ``baseline_median`` — median views_7d of its older posts. Collapsed when
    the recent median drops below ``ratio`` × baseline. An empty recent
    list, a non-positive baseline, or a non-positive ratio can never
    collapse — not enough signal, never a verdict.
    """
    if not recent_views or baseline_median <= 0 or ratio <= 0:
        return False
    return _median([float(v) for v in recent_views]) < baseline_median * ratio


def _as_pos_int(raw: object, default: int) -> int:
    try:
        v = int(raw)  # type: ignore[arg-type]
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def _as_pos_float(raw: object, default: float) -> float:
    try:
        v = float(raw)  # type: ignore[arg-type]
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def _scan_one_account(
    session,
    acc,
    now: dt.datetime,
    baseline_cutoff: dt.datetime,
    recent_min: dt.datetime,
    recent_max: dt.datetime,
    min_baseline_views: int,
    min_recent: int,
    ratio: float,
    pause_hours: int,
) -> "dict | None":
    """Evaluate one account; pause on collapse, resume on recovery.

    Returns a summary dict when it acted, else None. Never commits — the
    caller owns the transaction.
    """
    from sqlalchemy import select

    from app.models import AccountStatus, Notification, Post, PostStatus
    from app.tasks.sync_helpers import (
        as_aware_utc,
        log_event_sync,
        notify_sync,
        publish_sync,
    )

    dedup_key = f"shadowban:{acc.id}"
    unread = (
        session.execute(
            select(Notification)
            .where(
                Notification.dedup_key == dedup_key,
                Notification.read_at.is_(None),
            )
            .limit(1)
        )
        .scalars()
        .first()
    )

    baseline_rows = (
        session.execute(
            select(Post.views_7d).where(
                Post.account_id == acc.id,
                Post.status == PostStatus.posted,
                Post.posted_at < baseline_cutoff,
                Post.views_7d.is_not(None),
            )
        )
        .scalars()
        .all()
    )
    baseline = [float(v) for v in baseline_rows if v is not None]
    collapsed = False
    baseline_median = 0.0
    recent_n = 0
    recent_median = 0.0
    if len(baseline) >= SHADOWBAN_MIN_BASELINE_POSTS:
        baseline_median = _median(baseline)
        if baseline_median >= min_baseline_views:
            recent_rows = (
                session.execute(
                    select(Post.views_24h).where(
                        Post.account_id == acc.id,
                        Post.status == PostStatus.posted,
                        Post.posted_at >= recent_min,
                        Post.posted_at < recent_max,
                        Post.views_24h.is_not(None),
                    )
                )
                .scalars()
                .all()
            )
            recent = [int(v) for v in recent_rows if v is not None]
            recent_n = len(recent)
            if recent_n >= min_recent:
                recent_median = _median([float(v) for v in recent])
                collapsed = is_views_collapsed(recent, baseline_median, ratio)

    if collapsed and acc.status == AccountStatus.active:
        created = as_aware_utc(unread.created_at) if unread is not None else None
        episode_expired = (
            created is not None
            and (now - created).total_seconds() > pause_hours * 3600
        )
        if unread is None or episode_expired:
            # New episode (or the first one): pause the account. A previous
            # episode's notification is closed first so the fresh alert isn't
            # swallowed by the unread dedup. An unread notification from the
            # *current* episode means the admin already knows — and may have
            # deliberately reactivated the account — so we don't re-pause.
            if unread is not None:
                unread.read_at = now
            acc.status = AccountStatus.cooldown
            acc.cooldown_until = now + dt.timedelta(hours=pause_hours)
            notify_sync(
                "possible_shadowban",
                "critical",
                f"@{acc.username}: possible shadowban",
                f"The last {recent_n} reels for @{acc.username} are getting a "
                f"fraction of their usual reach (recent median "
                f"{int(recent_median)} views vs baseline "
                f"{int(baseline_median)}). Posting paused for {pause_hours}h. "
                "If reach recovers, the account resumes automatically — "
                "reactivate it manually any time from the Accounts page.",
                link="/dashboard/accounts",
                dedup_key=dedup_key,
                session=session,  # same txn: the read_at above is visible to dedup
            )
            publish_sync(
                "account_status_change",
                {"account_id": acc.id, "status": AccountStatus.cooldown.value},
            )
            log_event_sync(
                "WARNING",
                "account",
                f"@{acc.username} paused: possible shadowban "
                f"(recent median {int(recent_median)} vs baseline "
                f"{int(baseline_median)})",
            )
            return {
                "account_id": acc.id,
                "username": acc.username,
                "verdict": "paused",
                "recent_median": recent_median,
                "baseline_median": baseline_median,
            }
        return None

    if not collapsed and unread is not None:
        # Recovery: the collapse is gone. Auto-resolve our alert and resume
        # the account — but ONLY when the still-active cooldown is the one
        # this scan set (notification created_at + pause_hours ≈
        # cooldown_until, within an hour). A cooldown the admin set manually
        # for another reason is never overridden.
        unread.read_at = now
        resumed = False
        cd = as_aware_utc(acc.cooldown_until)
        created = as_aware_utc(unread.created_at)
        expected = (
            created + dt.timedelta(hours=pause_hours) if created is not None else None
        )
        if (
            acc.status == AccountStatus.cooldown
            and cd is not None
            and cd > now
            and expected is not None
            and abs((cd - expected).total_seconds()) <= 3600
        ):
            acc.status = AccountStatus.active
            acc.cooldown_until = None
            resumed = True
            publish_sync(
                "account_status_change",
                {"account_id": acc.id, "status": AccountStatus.active.value},
            )
        notify_sync(
            "shadowban_recovered",
            "success",
            f"@{acc.username}: reach recovered",
            f"Reel views for @{acc.username} are back to normal. "
            + (
                "The account was resumed automatically."
                if resumed
                else "No automatic resume was needed."
            ),
            link="/dashboard/accounts",
            session=session,
        )
        log_event_sync(
            "INFO", "account", f"@{acc.username}: shadowban alert resolved"
        )
        return {
            "account_id": acc.id,
            "username": acc.username,
            "verdict": "recovered" if resumed else "alert_resolved",
        }
    return None


def scan_shadowban_sync(session, now: "dt.datetime | None" = None) -> list[dict]:
    """Pause accounts whose recent reels' views collapsed; resume on recovery.

    See _scan_one_account for the per-account logic. Returns a per-account
    summary list. Never raises: monitoring must not break the worker.
    """
    import logging as _logging

    from sqlalchemy import select

    from app.models import Account, AccountStatus
    from app.tasks.sync_helpers import get_setting

    log = _logging.getLogger("igfunnel.account_health")
    now = now or _now()
    out: list[dict] = []
    try:
        if get_setting(session, "shadowban_scan_enabled", "true").strip().lower() != "true":
            return out
        min_baseline_views = _as_pos_int(
            get_setting(session, "shadowban_min_baseline_views", "100"), 100
        )
        min_recent = _as_pos_int(
            get_setting(session, "shadowban_min_recent_posts", "3"), 3
        )
        ratio = _as_pos_float(
            get_setting(session, "shadowban_collapse_ratio", "0.10"), 0.10
        )
        pause_hours = _as_pos_int(
            get_setting(session, "shadowban_pause_hours", "48"), 48
        )
        baseline_cutoff = now - dt.timedelta(days=SHADOWBAN_BASELINE_DAYS)
        recent_min = now - dt.timedelta(hours=SHADOWBAN_RECENT_MAX_HOURS)
        recent_max = now - dt.timedelta(hours=SHADOWBAN_RECENT_MIN_HOURS)

        accounts = (
            session.execute(
                select(Account).where(
                    Account.status.in_([AccountStatus.active, AccountStatus.cooldown])
                )
            )
            .scalars()
            .all()
        )
        for acc in accounts:
            try:
                result = _scan_one_account(
                    session,
                    acc,
                    now,
                    baseline_cutoff,
                    recent_min,
                    recent_max,
                    min_baseline_views,
                    min_recent,
                    ratio,
                    pause_hours,
                )
                if result:
                    out.append(result)
            except Exception:  # noqa: BLE001 — one bad account must not kill the scan
                log.warning("shadowban scan failed for account %s", acc.id, exc_info=True)
        session.commit()
    except Exception:  # noqa: BLE001 — monitoring must never raise
        log.warning("shadowban scan failed", exc_info=True)
    return out
