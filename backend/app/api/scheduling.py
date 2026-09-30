"""Schedule rules, captions, hashtag sets."""
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin, get_db
from app.models import CaptionTemplate, HashtagSet, Post, ScheduleRule
from app.schemas.content import (
    CaptionIn, CaptionOut, CaptionUpdate, HashtagSetIn, HashtagSetOut, HashtagSetUpdate,
    ScheduleRuleIn, ScheduleRuleOut, ScheduleRuleUpdate,
)

if TYPE_CHECKING:
    from app.models import Video

schedule_router = APIRouter()
caption_router = APIRouter()
hashtag_router = APIRouter()


async def _emit_schedule_update(rule_id: int | None = None, is_active: bool | None = None) -> None:
    """Push a schedule_update event so dashboard rule lists refresh instantly.

    Best-effort: the async publish() in app.services.realtime swallows all
    errors, so a down Redis never breaks the API response.
    """
    from app.services.realtime import publish as _publish

    payload: dict = {}
    if rule_id is not None:
        payload["rule_id"] = rule_id
    if is_active is not None:
        payload["is_active"] = is_active
    await _publish("schedule_update", payload)


def _rule_out(r: ScheduleRule, pin_map: "dict[int, Video] | None" = None) -> ScheduleRuleOut:
    label = status = None
    if r.pinned_video_id and pin_map is not None:
        v = pin_map.get(r.pinned_video_id)
        if v is not None:
            label = f"#{v.id} {v.original_filename}"
            try:
                status = v.status.value
            except AttributeError:
                status = str(v.status)
    return ScheduleRuleOut(
        id=r.id, name=r.name, day_of_week=r.day_of_week, hour=r.hour, minute=r.minute,
        account_id=r.account_id, is_active=r.is_active, preferred_effect=r.preferred_effect,
        caption_template_id=r.caption_template_id, prefer_source_caption=r.prefer_source_caption,
        created_at=r.created_at,
        pinned_video_id=r.pinned_video_id,
        pinned_video_label=label, pinned_video_status=status,
    )


async def _pin_map(db: AsyncSession, rules: list[ScheduleRule]) -> "dict[int, Video]":
    from app.models import Video

    ids = {r.pinned_video_id for r in rules if r.pinned_video_id}
    if not ids:
        return {}
    rows = (await db.execute(select(Video).where(Video.id.in_(ids)))).scalars().all()
    return {v.id: v for v in rows}


async def _validate_pin(db: AsyncSession, video_id: int, exclude_rule_id: int | None = None):
    """Shared pin guards: exists, processed, not queued, not pinned elsewhere."""
    from app.models import Post, PostStatus, Video, VideoStatus

    v = await db.get(Video, video_id)
    if not v:
        raise HTTPException(404, "Pinned video not found")
    if v.status != VideoStatus.processed:
        raise HTTPException(422, f"Only processed videos can be pinned (video is {v.status.value})")
    pending = (
        await db.execute(
            select(Post.id).where(Post.video_id == video_id, Post.status == PostStatus.scheduled).limit(1)
        )
    ).first()
    if pending:
        raise HTTPException(422, "Video is already queued for posting")
    q = select(ScheduleRule).where(
        ScheduleRule.pinned_video_id == video_id, ScheduleRule.is_active.is_(True)
    )
    if exclude_rule_id:
        q = q.where(ScheduleRule.id != exclude_rule_id)
    clash = (await db.execute(q)).scalars().first()
    if clash:
        raise HTTPException(422, f"Video is already pinned by rule '{clash.name}'")
    return v


@schedule_router.get("", response_model=list[ScheduleRuleOut])
async def list_rules(_: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(ScheduleRule).order_by(ScheduleRule.hour, ScheduleRule.minute))).scalars().all()
    pins = await _pin_map(db, list(rows))
    return [_rule_out(r, pins) for r in rows]


@schedule_router.post("", response_model=ScheduleRuleOut, status_code=201)
async def create_rule(body: ScheduleRuleIn, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    # SQLite doesn't enforce FKs — validate here so rules can't orphan.
    from app.models import Account, CaptionTemplate

    if body.account_id is not None and await db.get(Account, body.account_id) is None:
        raise HTTPException(404, "Account not found")
    if body.caption_template_id is not None and await db.get(CaptionTemplate, body.caption_template_id) is None:
        raise HTTPException(404, "Caption template not found")
    if body.pinned_video_id is not None:
        await _validate_pin(db, body.pinned_video_id)
    r = ScheduleRule(**body.model_dump())
    db.add(r)
    await db.commit()
    await db.refresh(r)
    await _emit_schedule_update(rule_id=r.id, is_active=r.is_active)
    return _rule_out(r, await _pin_map(db, [r]))


@schedule_router.put("/{rule_id}", response_model=ScheduleRuleOut)
async def update_rule(rule_id: int, body: ScheduleRuleUpdate, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    from app.models import Account, CaptionTemplate

    r = await db.get(ScheduleRule, rule_id)
    if not r:
        raise HTTPException(404, "Rule not found")
    if body.account_id is not None and await db.get(Account, body.account_id) is None:
        raise HTTPException(404, "Account not found")
    if body.caption_template_id is not None and await db.get(CaptionTemplate, body.caption_template_id) is None:
        raise HTTPException(404, "Caption template not found")
    if body.pinned_video_id is not None:
        await _validate_pin(db, body.pinned_video_id, exclude_rule_id=rule_id)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(r, k, v)
    await db.commit()
    await db.refresh(r)
    await _emit_schedule_update(rule_id=r.id, is_active=r.is_active)
    return _rule_out(r, await _pin_map(db, [r]))


@schedule_router.delete("/{rule_id}", status_code=204)
async def delete_rule(rule_id: int, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    r = await db.get(ScheduleRule, rule_id)
    if not r:
        raise HTTPException(404, "Rule not found")
    # A fired rule owns posts via posts.rule_id (nullable FK, no ON DELETE
    # behavior). Null out the link first so the commit doesn't die with a
    # FK violation (500). The posts keep their full history
    # (status/views/analytics) — they just become un-attributed, like
    # pre-0019 rows, which slot_already_fired() already treats as
    # account-wide blockers for their exact slot instant.
    await db.execute(
        update(Post).where(Post.rule_id == rule_id).values(rule_id=None)
    )
    await db.delete(r)
    await db.commit()
    await _emit_schedule_update(rule_id=rule_id)
    return None


@schedule_router.post("/{rule_id}/toggle")
async def toggle_rule(rule_id: int, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    r = await db.get(ScheduleRule, rule_id)
    if not r:
        raise HTTPException(404, "Rule not found")
    r.is_active = not r.is_active
    await db.commit()
    await _emit_schedule_update(rule_id=r.id, is_active=r.is_active)
    return {"is_active": r.is_active}


@schedule_router.post("/{rule_id}/pin", response_model=ScheduleRuleOut)
async def pin_rule(rule_id: int, body: dict, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    """Pin a processed video to a rule (re-arms retired rules too)."""
    r = await db.get(ScheduleRule, rule_id)
    if not r:
        raise HTTPException(404, "Rule not found")
    video_id = body.get("video_id")
    # bool is a subclass of int — {"video_id": true} must not look up video 1.
    if isinstance(video_id, bool) or not isinstance(video_id, int):
        raise HTTPException(422, "video_id is required")
    await _validate_pin(db, video_id, exclude_rule_id=rule_id)
    r.pinned_video_id = video_id
    r.is_active = True
    await db.commit()
    await db.refresh(r)
    await _emit_schedule_update(rule_id=r.id, is_active=True)
    return _rule_out(r, await _pin_map(db, [r]))


@schedule_router.post("/{rule_id}/unpin", response_model=ScheduleRuleOut)
async def unpin_rule(rule_id: int, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    """Remove the pin — the rule falls back to queue mode."""
    r = await db.get(ScheduleRule, rule_id)
    if not r:
        raise HTTPException(404, "Rule not found")
    r.pinned_video_id = None
    await db.commit()
    await db.refresh(r)
    await _emit_schedule_update(rule_id=r.id, is_active=r.is_active)
    return _rule_out(r)


@schedule_router.get("/calendar/data")
async def calendar(_: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    from sqlalchemy import select as sel

    from app.models import Post, PostStatus

    rules = (await db.execute(select(ScheduleRule))).scalars().all()
    upcoming = (
        await db.execute(
            sel(Post).where(Post.status == PostStatus.scheduled).order_by(Post.scheduled_for.asc()).limit(200)
        )
    ).scalars().all()
    pins = await _pin_map(db, list(rules))
    return {
        "rules": [_rule_out(r, pins).model_dump() for r in rules],
        "upcoming": [{"id": p.id, "account_id": p.account_id, "scheduled_for": p.scheduled_for} for p in upcoming],
    }


# ---- Captions ----

@caption_router.get("", response_model=list[CaptionOut])
async def list_captions(_: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    from sqlalchemy import func

    from app.models import Post, PostStatus

    rows = (await db.execute(select(CaptionTemplate).order_by(CaptionTemplate.name))).scalars().all()
    # Engagement computed live (same exact-caption approximation as
    # /{cid}/performance) — the old stored column was never written.
    eng = {
        (r[0] or ""): r[1]
        for r in (
            await db.execute(
                select(Post.caption, func.avg(Post.engagement_rate))
                .where(Post.status == PostStatus.posted)
                .group_by(Post.caption)
            )
        ).all()
    }
    return [CaptionOut(id=c.id, name=c.name, content=c.content, category=c.category, is_active=c.is_active, use_count=c.use_count,
                       avg_engagement=round(float(eng.get(c.content or "") or 0), 2)) for c in rows]


@caption_router.post("", response_model=CaptionOut, status_code=201)
async def create_caption(body: CaptionIn, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    c = CaptionTemplate(**body.model_dump())
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return CaptionOut(id=c.id, name=c.name, content=c.content, category=c.category, is_active=c.is_active, use_count=c.use_count)


@caption_router.put("/{cid}", response_model=CaptionOut)
async def update_caption(cid: int, body: CaptionUpdate, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    c = await db.get(CaptionTemplate, cid)
    if not c:
        raise HTTPException(404, "Caption not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(c, k, v)
    await db.commit()
    await db.refresh(c)
    return CaptionOut(id=c.id, name=c.name, content=c.content, category=c.category, is_active=c.is_active, use_count=c.use_count)


@caption_router.delete("/{cid}", status_code=204)
async def delete_caption(cid: int, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    c = await db.get(CaptionTemplate, cid)
    if not c:
        raise HTTPException(404, "Caption not found")
    # Detach referencing rules first — with FK enforcement on, deleting a
    # referenced template would 500 instead of leaving NULLs.
    from sqlalchemy import update as _update

    await db.execute(
        _update(ScheduleRule)
        .where(ScheduleRule.caption_template_id == cid)
        .values(caption_template_id=None)
    )
    await db.delete(c)
    await db.commit()
    return None


@caption_router.get("/{cid}/performance")
async def caption_performance(cid: int, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    from sqlalchemy import func

    from app.models import Post, PostStatus

    c = await db.get(CaptionTemplate, cid)
    if not c:
        raise HTTPException(404, "Caption not found")
    # Approximate: posts whose caption matches this template exactly.
    row = (
        await db.execute(
            select(func.count(Post.id), func.avg(Post.engagement_rate), func.coalesce(func.sum(Post.views_7d), 0)).where(
                Post.caption == c.content, Post.status == PostStatus.posted
            )
        )
    ).one()
    return {"use_count": c.use_count, "matched_posts": row[0], "avg_engagement": round(float(row[1] or 0), 2), "total_views": int(row[2] or 0)}


# ---- Hashtags ----

@hashtag_router.get("", response_model=list[HashtagSetOut])
async def list_tags(_: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(HashtagSet).order_by(HashtagSet.name))).scalars().all()
    return [HashtagSetOut(id=h.id, name=h.name, tags=h.tags, is_active=h.is_active, use_count=h.use_count) for h in rows]


@hashtag_router.post("", response_model=HashtagSetOut, status_code=201)
async def create_tags(body: HashtagSetIn, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    h = HashtagSet(**body.model_dump())
    db.add(h)
    await db.commit()
    await db.refresh(h)
    return HashtagSetOut(id=h.id, name=h.name, tags=h.tags, is_active=h.is_active, use_count=h.use_count)


@hashtag_router.put("/{hid}", response_model=HashtagSetOut)
async def update_tags(hid: int, body: HashtagSetUpdate, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    h = await db.get(HashtagSet, hid)
    if not h:
        raise HTTPException(404, "Hashtag set not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(h, k, v)
    await db.commit()
    await db.refresh(h)
    return HashtagSetOut(id=h.id, name=h.name, tags=h.tags, is_active=h.is_active, use_count=h.use_count)


@hashtag_router.delete("/{hid}", status_code=204)
async def delete_tags(hid: int, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    h = await db.get(HashtagSet, hid)
    if not h:
        raise HTTPException(404, "Hashtag set not found")
    await db.delete(h)
    await db.commit()
    return None
