"""Analytics aggregation queries."""
import datetime as dt

from sqlalchemy import func, select

from app.models import Account, Post, PostStatus, Video


async def overview(session, since: dt.datetime | None = None) -> dict:
    post_q = select(func.count(Post.id), func.coalesce(func.sum(Post.views_7d), 0)).where(
        Post.status == PostStatus.posted
    )
    if since:
        post_q = post_q.where(Post.posted_at >= since)
    total_posts, total_views = (await session.execute(post_q)).one()

    # Freshness of the numbers above: when the analytics sweep last
    # refreshed any posted post (null when nothing was ever checked).
    last_refresh = (
        await session.execute(
            select(func.max(Post.last_analytics_check)).where(
                Post.status == PostStatus.posted
            )
        )
    ).scalar()

    eng_q = select(func.avg(Post.engagement_rate)).where(
        Post.status == PostStatus.posted, Post.engagement_rate.is_not(None)
    )
    if since:
        eng_q = eng_q.where(Post.posted_at >= since)
    avg_eng = (await session.execute(eng_q)).scalar() or 0.0

    from app.models import AccountStatus, VideoStatus

    active_accounts = (
        await session.execute(select(func.count(Account.id)).where(Account.status == AccountStatus.active))
    ).scalar() or 0
    queue = (
        await session.execute(
            select(func.count(Video.id)).where(
                Video.status.in_([VideoStatus.uploaded, VideoStatus.processing, VideoStatus.processed])
            )
        )
    ).scalar() or 0
    scheduled = (
        await session.execute(select(func.count(Post.id)).where(Post.status == PostStatus.scheduled))
    ).scalar() or 0
    return {
        "total_posts": total_posts,
        "total_views": int(total_views or 0),
        "last_analytics_refresh": last_refresh.isoformat() if last_refresh else None,
        "avg_engagement_rate": round(float(avg_eng), 2),
        "active_accounts": active_accounts,
        "queue_size": queue,
        "scheduled_count": scheduled,
    }


async def views_over_time(session, days: int = 30) -> list[dict]:
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    rows = (
        await session.execute(
            select(
                func.date(Post.posted_at).label("day"),
                func.count(Post.id),
                func.coalesce(func.sum(Post.views_7d), 0),
            )
            .where(Post.status == PostStatus.posted, Post.posted_at >= since)
            .group_by(func.date(Post.posted_at))
            .order_by(func.date(Post.posted_at))
        )
    ).all()
    return [{"date": str(r[0]), "posts": r[1], "views": int(r[2] or 0)} for r in rows]


async def account_comparison(session) -> list[dict]:
    rows = (
        await session.execute(
            select(
                Account.username,
                func.count(Post.id),
                func.coalesce(func.sum(Post.views_7d), 0),
                func.avg(Post.engagement_rate),
            )
            .outerjoin(Post, (Post.account_id == Account.id) & (Post.status == PostStatus.posted))
            .group_by(Account.id, Account.username)
            .order_by(func.coalesce(func.sum(Post.views_7d), 0).desc())
        )
    ).all()
    return [
        {"username": r[0], "posts": r[1], "views": int(r[2] or 0), "avg_engagement": round(float(r[3] or 0), 2)}
        for r in rows
    ]


async def time_slot_performance(session) -> list[dict]:
    """Avg views by weekday × hour of posting."""
    rows = (
        await session.execute(
            select(
                func.extract("dow", Post.posted_at).label("dow"),
                func.extract("hour", Post.posted_at).label("hour"),
                func.count(Post.id),
                func.avg(Post.views_7d),
            )
            .where(Post.status == PostStatus.posted, Post.posted_at.is_not(None))
            .group_by("dow", "hour")
        )
    ).all()
    return [
        {"dow": int(r[0] or 0), "hour": int(r[1] or 0), "posts": r[2], "avg_views": int(r[3] or 0)}
        for r in rows
    ]
