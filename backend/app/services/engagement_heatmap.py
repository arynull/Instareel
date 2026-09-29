"""Day x hour engagement heatmap + pipeline funnel aggregates.

Both are read-only aggregates over already-collected analytics —
no schema change, no tick change, deliberately plain-row Python
aggregation (like best_slots) so SQLite and Postgres behave the same.

- Heatmap: 7x24 grid (dow 0=Monday..6=Sunday, hour in SCHEDULE_TZ
  wall-clock) of posted reels, each cell carrying post count and
  average views_7d. Powers the dashboard "best time to post" heatmap.
- Funnel: counts of each pipeline stage (sources -> acquired videos ->
  processed -> scheduled -> posted) for the dashboard command center.
"""
import datetime as dt
from collections import defaultdict
from zoneinfo import ZoneInfo

from app.config import settings

MIN_POSTS_PERSONALIZED = 3
DEFAULT_DAYS = 90


def _as_utc(ts: dt.datetime) -> dt.datetime:
    if getattr(ts, "tzinfo", None) is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    return ts


def aggregate_heatmap(pairs: list[tuple[dt.datetime, int]]) -> list[dict]:
    """Build the 7x24 grid. pairs = [(posted_at, views_7d), ...].

    Returns 168 cells ordered dow 0..6, hour 0..23. Every cell is always
    present (posts=0, avg_views=0 when empty) so the UI can render a
    stable grid without null handling.
    """
    tz = ZoneInfo(settings.SCHEDULE_TZ)
    by_cell: dict[tuple[int, int], list[int]] = defaultdict(list)
    for posted_at, views in pairs:
        local = _as_utc(posted_at).astimezone(tz)
        by_cell[(local.weekday(), local.hour)].append(int(views or 0))
    cells = []
    for dow in range(7):
        for hour in range(24):
            vals = by_cell.get((dow, hour), [])
            cells.append(
                {
                    "dow": dow,
                    "hour": hour,
                    "posts": len(vals),
                    "avg_views": int(sum(vals) / len(vals)) if vals else 0,
                }
            )
    return cells


async def heatmap_for_account(db, account_id: int | None = None, *, days: int = DEFAULT_DAYS) -> dict:
    """Engagement heatmap for one account, global fallback when the
    account has too little posted history (personalized=false)."""
    from fastapi import HTTPException
    from sqlalchemy import select

    from app.models import Account, Post, PostStatus

    username: str | None = None
    if account_id is not None:
        acc = await db.get(Account, account_id)
        if acc is None:
            raise HTTPException(404, "Account not found")
        username = acc.username

    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)

    async def _pairs(aid: int | None) -> list[tuple[dt.datetime, int]]:
        q = select(Post.posted_at, Post.views_7d).where(
            Post.status == PostStatus.posted,
            Post.posted_at.is_not(None),
            Post.posted_at >= since,
        )
        if aid is not None:
            q = q.where(Post.account_id == aid)
        rows = (await db.execute(q.limit(5000))).all()
        return [(_as_utc(ts), v or 0) for ts, v in rows]

    pairs = await _pairs(account_id) if account_id is not None else []
    personalized = len(pairs) >= MIN_POSTS_PERSONALIZED
    if not personalized:
        pairs = await _pairs(None)

    cells = aggregate_heatmap(pairs)
    return {
        "account_id": account_id,
        "username": username,
        "personalized": personalized,
        "min_posts_for_personalized": MIN_POSTS_PERSONALIZED,
        "days": days,
        "tz": settings.SCHEDULE_TZ,
        "tz_label": settings.SCHEDULE_TZ.split("/")[-1].replace("_", " "),
        "dow_labels": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        "max_avg_views": max((c["avg_views"] for c in cells), default=0),
        "total_posts": sum(c["posts"] for c in cells),
        "cells": cells,
    }


async def funnel_counts(db) -> dict:
    """One count per pipeline stage for the dashboard funnel widget."""
    from sqlalchemy import func, select

    from app.models import Post, PostStatus, Video, VideoStatus, VideoSource

    async def count(model, *filters) -> int:
        q = select(func.count()).select_from(model)
        for f in filters:
            q = q.where(f)
        return (await db.execute(q)).scalar_one()

    sources = await count(VideoSource)
    library = await count(Video)
    ready = await count(Video, Video.status == VideoStatus.processed)
    scheduled = await count(Post, Post.status == PostStatus.scheduled)
    posting = await count(Post, Post.status == PostStatus.posting)
    posted = await count(Post, Post.status == PostStatus.posted)
    return {
        "stages": [
            {"key": "sources", "label": "Sources", "count": sources},
            {"key": "library", "label": "Videos in library", "count": library},
            {"key": "ready", "label": "Ready to post", "count": ready},
            {"key": "scheduled", "label": "Scheduled", "count": scheduled + posting},
            {"key": "posted", "label": "Posted", "count": posted},
        ]
    }
