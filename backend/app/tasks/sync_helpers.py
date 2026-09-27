"""Sync helpers for Celery tasks: blocking realtime pub/sub, sync logging, sync scheduler."""
import json
import logging
import random
from typing import TYPE_CHECKING

import redis

from app.config import settings

if TYPE_CHECKING:
    from app.models import ScheduleRule, Video

_channel = "igfunnel:events"
log = logging.getLogger("igfunnel")


def _redis_client() -> redis.Redis:
    return redis.from_url(settings.REDIS_URL, decode_responses=True)


def publish_sync(event: str, payload: dict) -> None:
    """Blocking publish (Celery-safe). Failures are swallowed — best effort."""
    try:
        client = _redis_client()
        try:
            client.publish(_channel, json.dumps({"event": event, **payload}))
        finally:
            client.close()
    except Exception:
        pass


def set_progress_sync(video_id: int, percentage: float, stage: str) -> None:
    # Redis SET only — no event publish. The old per-tick publish made every
    # client refetch videos+sources+overview several times a second during
    # any encode (nothing displays live % from queries; the detail page
    # polls /status). Terminal states still publish video_processing_complete.
    try:
        client = _redis_client()
        try:
            client.set(
                f"igfunnel:progress:{video_id}",
                json.dumps({"percentage": percentage, "stage": stage}),
                ex=3600,
            )
        finally:
            client.close()
    except Exception:
        pass


def log_event_sync(level: str, category: str, message: str, details: dict | None = None) -> None:
    """Sync audit-log write for Celery tasks (commit included)."""
    from app.database import SyncSessionLocal
    from app.models import LogLevel, SystemLog

    try:
        lvl = LogLevel[level.upper()]
    except KeyError:
        lvl = LogLevel.INFO
    getattr(log, lvl.name.lower(), log.info)("[%s] %s", category, message)
    try:
        with SyncSessionLocal() as session:
            session.add(SystemLog(level=lvl, category=category, message=message, details=details))
            session.commit()
    except Exception:
        log.exception("Failed to persist system log")


#: Setting key holding the SCHEDULE_TZ date (YYYY-MM-DD) of the last
#: successful daily-counts reset.
DAILY_RESET_DATE_KEY = "daily_counts_reset_date"


def ensure_daily_counts_reset() -> bool:
    """Zero Account.posts_today once per SCHEDULE_TZ day, with catch-up.

    The beat entry fires at midnight, but if the worker/beat was down then,
    the old code never reset — accounts stayed capped at max_daily_posts
    indefinitely. The date stamp makes the reset idempotent: any caller
    (the midnight task, or the per-minute scheduler as a backstop) performs
    it exactly once per local day, catching up a missed midnight on the
    next run. Returns True when it actually reset.
    """
    from sqlalchemy import update

    from app.database import SyncSessionLocal
    from app.models import Account, Setting

    today = _schedule_now().date().isoformat()
    try:
        with SyncSessionLocal() as s:
            row = s.get(Setting, DAILY_RESET_DATE_KEY)
            if row is not None and row.value == today:
                return False
            s.execute(update(Account).values(posts_today=0))
            if row is None:
                s.add(Setting(key=DAILY_RESET_DATE_KEY, value=today, category="system"))
            else:
                row.value = today
            s.commit()
        log_event_sync("INFO", "system", f"Daily post counts reset ({today})")
        return True
    except Exception:  # noqa: BLE001
        log.exception("ensure_daily_counts_reset failed")
        return False


# ---- User-facing notifications (dashboard bell) ----

#: How long read notifications are kept before the watchdog prunes them.
NOTIFICATION_RETENTION_DAYS = 30


def notify_sync(
    ntype: str,
    severity: str,
    title: str,
    message: str,
    link: str | None = None,
    dedup_key: str | None = None,
) -> int | None:
    """Create a dashboard notification (commit included).

    With dedup_key, an identical UNREAD notification suppresses the new one —
    recurring conditions notify once instead of spamming every tick. Once the
    user reads it (or the watchdog auto-resolves it), the condition may
    notify again. Never raises: a notification must not break the task that
    triggered it.
    """
    from app.database import SyncSessionLocal
    from app.models import Notification, NotificationSeverity

    try:
        sev = NotificationSeverity(severity)
    except ValueError:
        sev = NotificationSeverity.INFO
    try:
        with SyncSessionLocal() as session:
            if dedup_key:
                existing = (
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
                if existing is not None:
                    return existing.id
            n = Notification(
                ntype=ntype,
                severity=sev,
                title=title,
                message=message,
                link=link,
                dedup_key=dedup_key,
            )
            session.add(n)
            session.commit()
            session.refresh(n)
            return n.id
    except Exception:
        log.exception("Failed to persist notification %s", ntype)
        return None


# ---- Sync scheduler helpers (mirrors the async service used by the web API) ----

import datetime as dt

from sqlalchemy import func, select

# ---- Account/proxy health policy (pure helpers — unit tested) ----

#: Fresh accounts stay in warm-up this long (see effective_max_posts).
WARMUP_DAYS = 7
#: Posting cap applied during warm-up regardless of the account setting.
WARMUP_MAX_POSTS = 1
#: Consecutive proxy failures before the proxy is auto-disabled.
MAX_PROXY_FAILS = 5
#: Cooldown given to accounts whose proxy was just auto-disabled.
PROXY_FAIL_COOLDOWN_HOURS = 6
#: (Legacy rotation guard, kept for reference.) Fresh accounts changing bio = flag.
BIO_MIN_AGE_DAYS = 14
#: Analytics skips accounts younger than this (saves logins on day-0 accounts).
ANALYTICS_MIN_AGE_DAYS = 3


def account_age_days(created_at: "dt.datetime | None", now: "dt.datetime | None" = None) -> float:
    """Age in days; tolerates naive datetimes (SQLite) by assuming UTC."""
    now = now or _now()
    if created_at is None:
        return 10**9
    ts = created_at
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    return (now - ts).total_seconds() / 86400


def effective_max_posts(created_at: "dt.datetime | None", max_daily_posts: int, now: "dt.datetime | None" = None, warmup_days: "int | None" = None) -> int:
    """Warm-up cap: accounts younger than ``warmup_days`` post at most 1/day.

    ``warmup_days=None`` keeps the module default (WARMUP_DAYS); pass 0 to
    disable the cap entirely — e.g. for a long-established Instagram
    account that was only recently connected here (``created_at`` is when
    the account joined this system, not the Instagram account's real age).

    Handles naive datetimes (SQLite stores func.now() without tz) by
    assuming UTC, so the same code works on SQLite and Postgres.
    """
    now = now or _now()
    days = WARMUP_DAYS if warmup_days is None else max(0, warmup_days)
    if created_at is None or days <= 0:
        return max_daily_posts
    ts = created_at
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    if (now - ts).days < days:
        return min(WARMUP_MAX_POSTS, max_daily_posts)
    return max_daily_posts


def warmup_days_setting(session) -> int:
    """Configured warm-up length (days); 0 disables the new-account cap.

    Editable on the dashboard Settings page (scheduler → warmup_days).
    Falls back to WARMUP_DAYS when the setting row is missing/invalid.
    """
    try:
        return max(0, int(get_setting(session, "warmup_days", str(WARMUP_DAYS))))
    except (TypeError, ValueError):
        return WARMUP_DAYS


def throttle_cooldown_hours(retry_count: int) -> int:
    """Exponential backoff for throttled accounts: 6h -> 12h -> 24h (capped)."""
    return 6 * (2 ** max(0, min(retry_count, 2)))


def rank_spare_proxies(candidates: list[dict], prefer_country: str = ""):
    """Pick the best spare proxy (pure — unit tested).

    Each candidate: {"proxy": obj, "load": int, "country": str,
    "latency": int|None, "fail_count": int}. Sort key, in order:
    same-country (no sudden geo-hop), fewest recent failures, fewest
    accounts, lowest latency. Returns the proxy object or None.
    """
    if not candidates:
        return None
    want = (prefer_country or "").upper()

    def key(c: dict):
        same = 0 if (want and (c.get("country") or "").upper() == want) else 1
        lat = c.get("latency")
        return (same, c.get("fail_count", 0), c.get("load", 0), lat if lat is not None else 10**9)

    return sorted(candidates, key=key)[0]["proxy"]


def pick_spare_proxy(session, exclude_id: "int | None" = None, prefer_country: "str | None" = None):
    """Highest-scoring spare proxy (active + healthy + proven, not exclude_id).

    Proven = checked at least once. Fresh auto rows enter life unhealthy and
    unchecked, so traffic never rides an unproven proxy.
    """
    from sqlalchemy import func as _func
    from sqlalchemy import select as _select

    from app.models import Account, Proxy

    q = (
        _select(Proxy, _func.count(Account.id))
        .outerjoin(Account, Account.proxy_id == Proxy.id)
        .where(Proxy.is_active.is_(True), Proxy.is_healthy.is_(True),
               Proxy.last_checked.is_not(None))
        .group_by(Proxy.id)
    )
    if exclude_id:
        q = q.where(Proxy.id != exclude_id)
    rows = session.execute(q).all()
    cands = [
        {"proxy": p, "load": cnt or 0, "country": p.country or "",
         "latency": p.latency_ms, "fail_count": p.fail_count or 0}
        for p, cnt in rows
    ]
    return rank_spare_proxies(cands, prefer_country or "")


def resolve_proxy(session, account):
    """The Proxy object to route this account through (or None).

    Own proxy while healthy, else the best-scoring spare. None means direct
    connection (no proxy assigned) or no healthy route — use
    account_reachable() to tell those apart.
    """
    from app.models import Proxy

    own = session.get(Proxy, account.proxy_id) if account.proxy_id else None
    if own is None and not account.proxy_id:
        return None
    if own is not None and own.is_active and own.is_healthy:
        return own
    return pick_spare_proxy(
        session,
        exclude_id=own.id if own else None,
        prefer_country=own.country if own else None,
    )


def resolve_proxy_url(session, account) -> "str | None":
    """Connection URL for this post: own proxy if healthy, else best spare."""
    from app.services.proxy_service import proxy_url_for

    return proxy_url_for(resolve_proxy(session, account))


def account_reachable(session, account) -> bool:
    """False only when the account needs a proxy but none healthy exists."""
    if not account.proxy_id:
        return True
    return resolve_proxy(session, account) is not None


#: Setting keys driving the auto pool (editable in dashboard Settings).
POOL_COUNTRY_KEY = "pool_country"
POOL_REQUIRE_COUNTRY_KEY = "pool_require_country"
#: Fresh auto rows get this many half-hours to prove themselves before the
#: purge may reap them (only inactive ones, never manual rows).
POOL_PURGE_AFTER_DAYS = 7
POOL_PURGE_LIMIT = 500
#: Auto rows that never went healthy a single time are stillborn: reaped this
#: fast regardless of active state. A proxy that can't prove itself in 48h of
#: 30-min check cycles is list filler, not capacity.
POOL_STILLBORN_HOURS = 48
#: Hard ceiling on auto rows; beyond it the worst go each refresh.
POOL_MAX_AUTO = 500
#: Safety caps per refresh cycle so one giant list can't flood the DB.
POOL_MAX_NEW_PER_SOURCE = 300
#: Health-check batching: one cycle covers this many stalest rows with a fast
#: TCP sweep, and fully verifies at most this many survivors. The sweep and
#: verify fans run on thread pools (IO-bound work); DB writes stay strictly
#: sequential so SQLite never sees concurrent writers.
PROXY_CHECK_BATCH = 60
PROXY_CHECK_THREADS = 20
PROXY_VERIFY_LIMIT = 20
PROXY_VERIFY_THREADS = 10
SWEEP_TCP_TIMEOUT = 3


def due_for_check(session, limit: int = PROXY_CHECK_BATCH) -> list[int]:
    """Oldest-checked proxy ids first (never-checked lead). Pure query, tested."""
    from app.models import Proxy

    return list(
        session.execute(
            select(Proxy.id).order_by(Proxy.last_checked.asc().nulls_first()).limit(limit)
        ).scalars().all()
    )


def get_setting(session, key: str, default: str = "") -> str:
    """Read a Setting row value with fallback (pure DB, no env)."""
    from app.models import Setting

    row = session.get(Setting, key)
    return row.value if row is not None else default


def pool_allows_country(spec_country: str, source_default: str, pool_country: str, require: bool) -> bool:
    """Single-location gate for auto-pool inserts (pure — unit tested).

    The line's own |CC/#CC tag wins, else the source default. When require
    is on, only that exact country passes; when off, everything passes.
    """
    want = (pool_country or "").strip().upper()
    if not require or not want:
        return True
    have = (spec_country or "").strip().upper() or (source_default or "").strip().upper()
    return have == want


def _park_accounts_for_purged_proxies(session, proxy_ids: list[int], reason: str) -> int:
    """Park accounts whose proxy is being deleted: unlink + disable them.

    Unlinking alone is dangerous — account_reachable() treats proxy_id=None
    as "direct connection OK" and the account would suddenly post from the
    server's datacenter IP (challenge / geo-hop risk). Parking sets
    status=disabled so nothing posts until an admin assigns a healthy proxy
    and re-enables the account. Returns the parked count.
    """
    from app.models import Account, AccountStatus

    if not proxy_ids:
        return 0
    parked = 0
    stamp = _now().strftime("%Y-%m-%d %H:%M UTC")
    for acc in session.execute(select(Account).where(Account.proxy_id.in_(proxy_ids))).scalars().all():
        acc.proxy_id = None  # required before the proxy row can be deleted (FK)
        acc.status = AccountStatus.disabled
        note = f"[{stamp}] Auto-parked: {reason}. Assign a healthy proxy and re-enable to resume posting."
        acc.notes = (acc.notes + "\n" + note) if acc.notes else note
        parked += 1
        log_event_sync(
            "WARNING",
            "proxy",
            f"Account '{acc.username}' auto-parked: {reason}",
            {"account_id": acc.id},
        )
        publish_sync("account_status_change", {"account_id": acc.id, "status": "disabled"})
    return parked


def purge_stale_auto_proxies(
    session,
    max_age_days: int = POOL_PURGE_AFTER_DAYS,
    stillborn_hours: int = POOL_STILLBORN_HOURS,
    limit: int = POOL_PURGE_LIMIT,
) -> int:
    """Delete dead AUTO pool rows. Manual rows are immortal. Returns count.

    The pool lifecycle, in one place:
      NEW (unchecked) -> first success -> HEALTHY (routable)
      failure streak -> 5 fails -> DISABLED (+ accounts parked)
      reaping, auto rows only:
        - stillborn: never healthy once + created past grace -> delete
        - proven dead: disabled + unhealthy (5 consecutive fails with no
          healing success in between) -> delete at the next cycle
        - retired: disabled leftovers past retention -> delete
    Proven-dead rows go immediately: the 5-fail streak IS the proof, keeping
    them longer only clutters the pool. Accounts on purged proxies are
    PARKED (disabled) first — never silently unlinked, which would make them
    post from the server's datacenter IP.
    """
    from sqlalchemy import and_ as _and
    from sqlalchemy import or_ as _or

    from app.models import Account, Proxy

    now = _now()
    rows = (
        session.execute(
            select(Proxy).where(
                Proxy.source.is_not(None),
                Proxy.source != "manual",
                _or(
                    _and(
                        Proxy.is_healthy.is_(False),
                        Proxy.created_at < now - dt.timedelta(hours=stillborn_hours),
                    ),
                    _and(
                        Proxy.is_active.is_(False),
                        Proxy.is_healthy.is_(False),
                    ),
                    _and(
                        Proxy.is_active.is_(False),
                        Proxy.last_checked.is_not(None),
                        Proxy.last_checked < now - dt.timedelta(days=max_age_days),
                    ),
                ),
            ).limit(limit)
        )
    ).scalars().all()
    if rows:
        gone_ids = [p.id for p in rows]
        _park_accounts_for_purged_proxies(
            session, gone_ids, "its auto proxy was purged from the pool"
        )
        for p in rows:
            session.delete(p)
        session.commit()
    return len(rows)


def cap_auto_pool(session, max_auto: int = POOL_MAX_AUTO) -> int:
    """Hard ceiling on auto rows: beyond the cap the worst go first
    (disabled, then most fails, then stalest). Manual rows never touched.
    Returns the deleted count. Commits."""
    from sqlalchemy import func as _func
    from sqlalchemy import select as _select

    from app.models import Proxy

    auto = _select(Proxy).where(Proxy.source.is_not(None), Proxy.source != "manual")
    n = session.execute(_select(_func.count()).select_from(auto.subquery())).scalar() or 0
    excess = n - max_auto
    if excess <= 0:
        return 0
    rows = (
        session.execute(
            auto.order_by(
                Proxy.is_active.asc(),
                Proxy.fail_count.desc(),
                Proxy.last_checked.asc().nulls_first(),
            ).limit(excess)
        )
    ).scalars().all()
    gone_ids = [p.id for p in rows]
    _park_accounts_for_purged_proxies(
        session, gone_ids, "its auto proxy was trimmed by the pool cap"
    )
    for p in rows:
        session.delete(p)
    session.commit()
    return len(rows)


def looks_like_proxy_error(err: str) -> bool:
    """Heuristic: did this failure come from the proxy/network path (pure)?

    Used to attribute post failures to the egress proxy (throttle is always
    attributed — it is IP reputation by definition).
    """
    text = (err or "").lower()
    markers = (
        "proxy", "connect", "timeout", "timed out", "connection reset",
        "connection aborted", "temporary failure", "name resolution",
        "nodename nor servname", "network is unreachable", "broken pipe",
        "connectionerror", "max retries exceeded",
    )
    return any(m in text for m in markers)


def record_proxy_check(session, proxy, ok: bool, latency_ms: "int | None" = None, error: str = "") -> bool:
    """Persist one health observation — from the checker OR live post traffic.

    Success heals (fail streak reset). Failure increments the shared streak;
    at MAX_PROXY_FAILS the proxy auto-disables and its accounts are parked.
    Returns True when this call newly disabled the proxy. Commits.
    """
    now = _now()
    proxy.last_checked = now
    if ok:
        proxy.is_healthy = True
        proxy.fail_count = 0
        proxy.last_error = None
        if latency_ms is not None:
            proxy.latency_ms = latency_ms
        session.commit()
        return False
    proxy.fail_count = (proxy.fail_count or 0) + 1
    proxy.is_healthy = False
    proxy.last_error = (error or "check failed")[:500]
    newly_disabled = False
    parked = 0
    if proxy.is_active and proxy.fail_count >= MAX_PROXY_FAILS:
        from app.models import Account

        proxy.is_active = False
        newly_disabled = True
        until = now + dt.timedelta(hours=PROXY_FAIL_COOLDOWN_HOURS)
        for acc in session.execute(select(Account).where(Account.proxy_id == proxy.id)).scalars().all():
            acc.cooldown_until = until
            parked += 1
        log_event_sync(
            "WARNING", "proxy",
            f"Proxy #{proxy.id} auto-disabled after {proxy.fail_count} failures; {parked} account(s) parked",
        )
    session.commit()
    return newly_disabled


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _schedule_now() -> dt.datetime:
    """Current time in the schedule timezone.

    Schedule-rule hours are wall-clock in SCHEDULE_TZ (not server/UTC time),
    so a rule set for 12:00 fires at the user's 12:00.
    """
    from zoneinfo import ZoneInfo

    return dt.datetime.now(ZoneInfo(settings.SCHEDULE_TZ))


def as_aware_utc(ts: "dt.datetime | None") -> "dt.datetime | None":
    """Normalize a DB datetime for comparison (central timezone guard).

    SQLite returns naive datetimes while Postgres returns aware ones;
    comparing either against aware ``now`` raises TypeError. Pass every
    DB timestamp through here before comparing or subtracting.
    """
    if ts is None:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=dt.timezone.utc)
    return ts


def due_rules(session, at: "dt.datetime | None" = None):
    from app.models import ScheduleRule

    # at is the wall-clock in SCHEDULE_TZ — rule hours are user's hours,
    # not the server's. An explicitly passed `at` is used as-is (tests).
    at = at or _schedule_now()
    q = select(ScheduleRule).where(
        ScheduleRule.is_active.is_(True),
        ((ScheduleRule.day_of_week == -1) | (ScheduleRule.day_of_week == at.weekday())),
        ScheduleRule.hour == at.hour,
        ScheduleRule.minute == at.minute,
    )
    return list(session.execute(q).scalars().all())


def _schedule_grace_minutes() -> int:
    """Grace window (minutes) a rule slot stays fireable after its minute."""
    from app.config import settings

    try:
        return max(0, int(settings.SCHEDULE_GRACE_MINUTES))
    except (TypeError, ValueError):
        return 0


def due_rule_slots(session, at: "dt.datetime | None" = None):
    """(rule, slot) pairs whose scheduled minute fell inside the grace window.

    A slot is the rule's wall-clock minute in SCHEDULE_TZ. It stays fireable
    for SCHEDULE_GRACE_MINUTES after the minute passes, so a brief
    worker/beat outage at the exact minute doesn't silently lose the slot —
    the next tick still posts it (a few minutes late).

    Returns pairs sorted oldest-slot-first. The slot is normalized to aware
    UTC — the same convention as Post.scheduled_for — so it can be stored
    in Post.slot_for and compared directly. ``grace=0`` degrades to the
    exact-minute behavior of due_rules().
    """
    from zoneinfo import ZoneInfo

    from app.config import settings
    from app.models import ScheduleRule

    at = at or _schedule_now()
    if at.tzinfo is None:
        at = at.replace(tzinfo=ZoneInfo(settings.SCHEDULE_TZ))
    at = at.replace(second=0, microsecond=0)
    grace = _schedule_grace_minutes()
    rules = list(
        session.execute(
            select(ScheduleRule).where(ScheduleRule.is_active.is_(True))
        )
        .scalars()
        .all()
    )
    seen: set[tuple[int, dt.datetime]] = set()
    pairs: list[tuple["ScheduleRule", dt.datetime]] = []
    for back in range(grace, -1, -1):
        slot = at - dt.timedelta(minutes=back)
        for rule in rules:
            if not (
                (rule.day_of_week == -1 or rule.day_of_week == slot.weekday())
                and rule.hour == slot.hour
                and rule.minute == slot.minute
            ):
                continue
            slot_utc = slot.astimezone(dt.timezone.utc)
            key = (rule.id, slot_utc)
            if key in seen:
                continue
            seen.add(key)
            pairs.append((rule, slot_utc))
    pairs.sort(key=lambda p: p[1])
    return pairs


def slot_already_fired(session, rule: "ScheduleRule", slot_utc: dt.datetime) -> bool:
    """True once ANY post row exists for this account+slot.

    One slot → at most one post row, ever: in-flight/done statuses AND
    terminal ones (failed/deleted) all count — a failed slot is owned by the
    retry/reprocess machinery, not re-fired (avoids duplicate content on
    false-negative failures and failure loops on bad videos). Without this,
    every beat tick inside the grace window would queue another post for the
    same slot.
    """
    from app.models import Post

    q = select(func.count(Post.id)).where(
        Post.slot_for == slot_utc,
        (Post.account_id == rule.account_id) if rule.account_id else True,
    )
    return (session.execute(q).scalar() or 0) > 0


def eligible_account(session, account_id: "int | None" = None):
    from app.models import Account, AccountStatus

    now = _now()
    if account_id:
        acc = session.get(Account, account_id)
        if acc and acc.status == AccountStatus.active:
            cd = as_aware_utc(acc.cooldown_until)
            if not cd or cd <= now:
                cap = effective_max_posts(
                    acc.created_at, acc.max_daily_posts, now,
                    warmup_days=warmup_days_setting(session),
                )
                if acc.posts_today < cap:
                    return acc
        return None
    q = (
        select(Account)
        .where(
            Account.status == AccountStatus.active,
            Account.posts_today < Account.max_daily_posts,
            ((Account.cooldown_until.is_(None)) | (Account.cooldown_until <= now)),
            ((Account.last_post.is_(None)) | (Account.last_post <= now - dt.timedelta(hours=2))),
        )
        .order_by(Account.last_post.asc().nulls_first())
    )
    # Warm-up cap is per-account age — filter in Python over the ordered set
    # so a fresh account yields to older ones instead of blocking the slot.
    # Capped at 500 rows: the accounts table is tiny, this is a guardrail.
    wdays = warmup_days_setting(session)
    for acc in session.execute(q.limit(500)).scalars().all():
        if acc.posts_today < effective_max_posts(acc.created_at, acc.max_daily_posts, now, warmup_days=wdays):
            return acc
    return None


def account_skip_reason(session, account_id: "int | None") -> str | None:
    """Why eligible_account() found nothing — None when an account is eligible.

    Mirrors the checks in eligible_account() so a skipped rule slot can say
    *why* instead of disappearing silently. Call only after eligible_account()
    returned None.
    """
    from app.models import Account, AccountStatus

    now = _now()
    if account_id:
        acc = session.get(Account, account_id)
        if acc is None:
            return "the rule's account no longer exists"
        if acc.status != AccountStatus.active:
            return f"@{acc.username} is {acc.status.value}"
        cd = as_aware_utc(acc.cooldown_until)
        if cd and cd > now:
            return f"@{acc.username} is in cooldown until {cd:%H:%M} UTC"
        wdays = warmup_days_setting(session)
        cap = effective_max_posts(acc.created_at, acc.max_daily_posts, now, warmup_days=wdays)
        if acc.posts_today >= cap:
            warm = ""
            created = acc.created_at
            if created is not None:
                if created.tzinfo is None:
                    created = created.replace(tzinfo=dt.timezone.utc)
                if wdays > 0 and (now - created).days < wdays:
                    warm = (
                        f" — new-account warm-up: max {WARMUP_MAX_POSTS}/day "
                        f"for the first {wdays} days (set scheduler → "
                        f"warmup_days to 0 to disable)"
                    )
            return (
                f"daily post limit reached for @{acc.username} "
                f"({acc.posts_today}/{cap} posted today){warm}"
            )
        return None  # eligible — shouldn't happen after a real skip
    return "no active account with remaining daily capacity"


def next_video(session, effect: "str | None" = None):
    """Oldest processed video, preferring the rule's effect; fallback to any."""
    from app.models import Video, VideoStatus

    base = select(Video).where(Video.status == VideoStatus.processed)
    if effect:
        preferred = base.where(Video.effect_preset == effect).order_by(Video.created_at.asc()).limit(1)
        video = session.execute(preferred).scalars().first()
        if video:
            return video
    return session.execute(base.order_by(Video.created_at.asc()).limit(1)).scalars().first()


def resolve_rule_video(session, rule: "ScheduleRule", used_ids: "set[int]") -> "tuple[Video | None, str]":
    """Pick the video for a due rule. Returns (video|None, disposition).

    Dispositions: "fire" (post it, retire rule if pinned), "retire_gone"
    (pin target deleted), "retire_posted" (pin already posted elsewhere),
    "wait" (pinned video not postable yet — still processing/failed/queued;
    rule stays armed), "empty" (queue mode, nothing processed).
    """
    from app.models import Video, VideoStatus

    pin = getattr(rule, "pinned_video_id", None)
    if pin:
        video = session.get(Video, pin)
        if video is None:
            return None, "retire_gone"
        if video.status == VideoStatus.posted:
            return None, "retire_posted"
        if video.status != VideoStatus.processed:
            return None, "wait"
        if video.id in used_ids or video_already_queued(session, video.id):
            return None, "wait"
        return video, "fire"
    video = next_video(session, rule.preferred_effect)
    if not video or video.id in used_ids:
        return None, "empty"
    if video_already_queued(session, video.id):
        return None, "empty"
    return video, "fire"


def claim_post(session, post_id: int) -> str:
    """Atomically claim a scheduled post for execution (single-flight).

    One UPDATE ... WHERE status=scheduled so concurrent workers, beat
    redelivery and celery retries can never upload the same post twice.
    Returns 'claimed' | 'missing' | 'busy'. Commits — call with a fresh
    session (never one holding uncommitted work you intend to roll back).
    """
    from sqlalchemy import update

    from app.models import Post, PostStatus

    res = session.execute(
        update(Post)
        .where(Post.id == post_id, Post.status == PostStatus.scheduled)
        .values(status=PostStatus.posting)
    )
    session.commit()
    if res.rowcount:
        return "claimed"
    post = session.get(Post, post_id)
    return "missing" if post is None else "busy"


def video_already_queued(session, video_id: int, window_min: int = 10) -> bool:
    """True when the video already has a pending scheduled post in the window.

    Prevents two rules in one tick (or API + beat) from queueing the same
    video twice — the video is only freed after it posts or the post fails.
    """
    from app.models import Post, PostStatus

    now = _now()
    q = select(func.count(Post.id)).where(
        Post.video_id == video_id,
        Post.status == PostStatus.scheduled,
        Post.scheduled_for >= now - dt.timedelta(minutes=window_min),
        Post.scheduled_for <= now + dt.timedelta(minutes=window_min),
    )
    return (session.execute(q).scalar() or 0) > 0


#: A 'posting' sibling older than this is a crashed worker's leftover — it
#: must not wedge the video forever (see find_blocking_sibling).
SIBLING_STALE_HOURS = 2


def find_blocking_sibling(session, post_id: int, video_id: int, now: "dt.datetime | None" = None):
    """Another post for the same video already in flight or done (or None).

    Fail-closed executor guard: if two different Post rows ever target one
    video (e.g. manual API double-scheduling), the loser aborts instead of
    double-uploading. A stale 'posting' sibling (crashed worker, older than
    SIBLING_STALE_HOURS) is ignored so one crash can't block the video.
    """
    from app.models import Post, PostStatus

    now = now or _now()
    rows = (
        session.execute(
            select(Post).where(
                Post.video_id == video_id,
                Post.id != post_id,
                Post.status.in_([PostStatus.posting, PostStatus.posted]),
            )
        )
        .scalars()
        .all()
    )
    for sib in rows:
        if sib.status == PostStatus.posted:
            return sib
        ts = sib.updated_at
        if ts is not None:
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=dt.timezone.utc)
            if (now - ts).total_seconds() < SIBLING_STALE_HOURS * 3600:
                return sib
    return None


def already_scheduled(session, rule, window_min: int = 10) -> bool:
    from app.models import Post, PostStatus

    now = _now()
    q = select(func.count(Post.id)).where(
        Post.status == PostStatus.scheduled,
        Post.scheduled_for >= now - dt.timedelta(minutes=window_min),
        Post.scheduled_for <= now + dt.timedelta(minutes=window_min),
        (Post.account_id == rule.account_id) if rule.account_id else True,
    )
    return (session.execute(q).scalar() or 0) > 0


def resolve_audio(session, name: "str | None"):
    """Active AudioTrack (by name) with an existing file — else None, never raises."""
    import os

    from app.models import AudioTrack

    clean = (name or "").strip()
    if not clean:
        return None
    t = (
        session.execute(
            select(AudioTrack).where(
                AudioTrack.name == clean, AudioTrack.is_active.is_(True)
            )
        )
        .scalars()
        .first()
    )
    if t is not None and t.file_path and os.path.exists(t.file_path):
        return t
    return None


def pick_audio(session):
    """Weighted-random active track whose file exists (least-used favored)."""
    import os

    from app.models import AudioTrack

    rows = session.execute(select(AudioTrack).where(AudioTrack.is_active.is_(True))).scalars().all()
    rows = [r for r in rows if r.file_path and os.path.exists(r.file_path)]
    if not rows:
        return None
    weights = [1.0 / (1.0 + (r.use_count or 0)) for r in rows]
    return random.choices(rows, weights=weights, k=1)[0]


def pick_caption(session, template_id: "int | None") -> "tuple[str, int | None]":
    from app.models import CaptionTemplate

    if template_id:
        t = session.get(CaptionTemplate, template_id)
        if t and t.is_active:
            return t.content, t.id
    rows = session.execute(select(CaptionTemplate).where(CaptionTemplate.is_active.is_(True))).scalars().all()
    if not rows:
        return "", None
    weights = [1.0 / (1.0 + (r.use_count or 0)) for r in rows]
    chosen = random.choices(rows, weights=weights, k=1)[0]
    # Feed the weighting: without this the least-used bias never learns.
    chosen.use_count = (chosen.use_count or 0) + 1
    return chosen.content, chosen.id


def pick_hashtags(session, last_tags: str = "") -> str:
    from app.models import HashtagSet

    rows = session.execute(select(HashtagSet).where(HashtagSet.is_active.is_(True))).scalars().all()
    cands = []
    for r in rows:
        raw = (r.tags or "").strip()
        if not raw or raw == last_tags.strip():
            continue
        tags = [t.strip() for t in raw.replace("\n", ",").split(",") if t.strip()]
        if tags:
            cands.append((r, tags))
    if not cands:
        return ""
    chosen, tags = random.choice(cands)
    chosen.use_count = (chosen.use_count or 0) + 1
    selected = random.sample(tags, k=min(len(tags), random.randint(3, 5)))
    return " ".join(t if t.startswith("#") else f"#{t}" for t in selected)


def resolve_fire_caption(session, prefer_source: bool, source_caption: "str | None",
                         template_id: "int | None") -> "tuple[str, str]":
    """Caption + hashtags for a firing post (pure decision, unit tested).

    A harvested source caption posts verbatim — its hashtags already ship
    inside it, so no extra set is appended. Otherwise template + hashtag
    set, exactly as before.
    """
    src = (source_caption or "").strip()
    if prefer_source and src:
        return src, ""
    caption, _ = pick_caption(session, template_id)
    return caption, pick_hashtags(session)
