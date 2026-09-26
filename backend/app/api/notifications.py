"""Dashboard notifications: the bell feed, upcoming slots, read state."""
import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from zoneinfo import ZoneInfo

from app.api.deps import get_current_admin, get_db
from app.config import settings
from app.models import Account, Notification, ScheduleRule

notifications_router = APIRouter()

#: How many upcoming rule firings the bell panel shows.
UPCOMING_LIMIT = 8


def _notification_out(n: Notification) -> dict:
    return {
        "id": n.id,
        "type": n.ntype,
        "severity": n.severity.value if hasattr(n.severity, "value") else n.severity,
        "title": n.title,
        "message": n.message,
        "link": n.link,
        "read": n.read_at is not None,
        "created_at": n.created_at.isoformat() if n.created_at else None,
    }


def _next_fire(rule: ScheduleRule, now: dt.datetime) -> dt.datetime | None:
    """Next wall-clock firing strictly after `now` (both in SCHEDULE_TZ).

    Scans forward day by day (max 8) for the next minute matching the rule's
    day/hour/minute — the same convention as the scheduler (day_of_week ==
    date.weekday(), -1 = daily).
    """
    base = now.replace(second=0, microsecond=0)
    for day_offset in range(9):
        day = (base + dt.timedelta(days=day_offset)).date()
        if rule.day_of_week != -1 and day.weekday() != rule.day_of_week:
            continue
        candidate = dt.datetime.combine(
            day, dt.time(rule.hour, rule.minute), tzinfo=now.tzinfo
        )
        if candidate > now:
            return candidate
    return None


async def upcoming_slots(db: AsyncSession, limit: int = UPCOMING_LIMIT) -> list[dict]:
    """Next scheduled postings across all active rules, soonest first."""
    tz = ZoneInfo(settings.SCHEDULE_TZ)
    now = dt.datetime.now(tz)
    rows = (
        (
            await db.execute(
                select(ScheduleRule, Account.username)
                .outerjoin(Account, Account.id == ScheduleRule.account_id)
                .where(ScheduleRule.is_active.is_(True))
            )
        )
        .all()
    )
    upcoming = []
    for rule, username in rows:
        nxt = _next_fire(rule, now)
        if nxt is None:
            continue
        upcoming.append(
            {
                "rule_id": rule.id,
                "rule_name": rule.name,
                "account": username,
                "fires_at": nxt.isoformat(),
                # Wall-clock label in the schedule timezone, e.g. "Sat 21:00".
                "label": nxt.strftime("%a %H:%M"),
                "in_seconds": int((nxt - now).total_seconds()),
            }
        )
    upcoming.sort(key=lambda x: x["in_seconds"])
    return upcoming[:limit]


@notifications_router.get("")
async def list_notifications(
    limit: int = Query(50, ge=1, le=200),
    _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """The bell feed: recent notifications + unread count + upcoming slots."""
    rows = (
        (await db.execute(select(Notification).order_by(desc(Notification.id)).limit(limit)))
        .scalars()
        .all()
    )
    unread_count = (
        await db.execute(
            select(func.count(Notification.id)).where(Notification.read_at.is_(None))
        )
    ).scalar() or 0
    return {
        "notifications": [_notification_out(n) for n in rows],
        "unread_count": unread_count,
        "upcoming": await upcoming_slots(db),
    }


@notifications_router.post("/read-all")
async def mark_all_read(
    _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)
):
    now = dt.datetime.now(dt.timezone.utc)
    await db.execute(
        update(Notification)
        .where(Notification.read_at.is_(None))
        .values(read_at=now)
    )
    await db.commit()
    return {"ok": True}


@notifications_router.post("/{notification_id}/read")
async def mark_read(
    notification_id: int,
    _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    n = await db.get(Notification, notification_id)
    if n is None:
        raise HTTPException(status_code=404, detail="Notification not found")
    if n.read_at is None:
        n.read_at = dt.datetime.now(dt.timezone.utc)
        await db.commit()
    return {"ok": True}
