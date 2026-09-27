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
