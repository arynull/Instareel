"""Video sources: ingest reels from IG pages into the videos library."""
import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin, get_db, limiter
from app.models import SourceItem, SourceItemStatus, SourceStatus, VideoSource
from app.schemas.source import SourceIn, SourceItemOut, SourceOut, SourceUpdate
from app.services.log_service import log_event

source_router = APIRouter()

_RUNNABLE = (SourceStatus.idle, SourceStatus.completed, SourceStatus.failed)


def _out(r: VideoSource, total: int = 0, pending: int = 0) -> SourceOut:
    return SourceOut(
        id=r.id, username=r.username, account_id=r.account_id, status=r.status.value,
        max_items=r.max_items, reels_only=r.reels_only, with_covers=r.with_covers,
        auto_process=r.auto_process, delay_min_s=r.delay_min_s, delay_max_s=r.delay_max_s,
        has_cursor=bool(r.end_cursor), fetched=r.fetched, downloaded=r.downloaded,
        skipped=r.skipped, failed_count=r.failed_count, total_items=total,
        pending_items=pending, last_error=r.last_error, current_stage=r.current_stage,
        started_at=r.started_at, finished_at=r.finished_at, created_at=r.created_at,
    )


async def _item_counts(db: AsyncSession, sid: int) -> "tuple[int, int]":
    total = (
        await db.execute(select(func.count(SourceItem.id)).where(SourceItem.source_id == sid))
    ).scalar() or 0
    pending = (
        await db.execute(
            select(func.count(SourceItem.id)).where(
                SourceItem.source_id == sid,
                SourceItem.status.in_([SourceItemStatus.pending, SourceItemStatus.downloading]),
            )
        )
    ).scalar() or 0
    return total, pending


@source_router.get("", response_model=list[SourceOut])
async def list_sources(_: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(VideoSource).order_by(VideoSource.id.desc()))).scalars().all()
    out = []
    for r in rows:
        total, pending = await _item_counts(db, r.id)
        out.append(_out(r, total, pending))
    return out


@source_router.get("/{sid}", response_model=SourceOut)
async def get_source(sid: int, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    r = await db.get(VideoSource, sid)
    if not r:
        raise HTTPException(404, "Source not found")
    total, pending = await _item_counts(db, sid)
    return _out(r, total, pending)


@source_router.post("", response_model=SourceOut, status_code=201)
async def create_source(body: SourceIn, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    from app.models import Account

    if body.account_id is not None and await db.get(Account, body.account_id) is None:
        raise HTTPException(404, "Download account not found")
    dup = (
        await db.execute(select(VideoSource).where(VideoSource.username == body.username))
    ).scalars().first()
    if dup:
        raise HTTPException(409, f"@{body.username} is already a source (#{dup.id})")
    r = VideoSource(**body.model_dump())
    db.add(r)
    await db.commit()
    await db.refresh(r)
    await log_event("INFO", "source", f"Source @{r.username} added")
    return _out(r)


@source_router.put("/{sid}", response_model=SourceOut)
async def update_source(sid: int, body: SourceUpdate, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    from app.models import Account

    r = await db.get(VideoSource, sid)
    if not r:
        raise HTTPException(404, "Source not found")
    if r.status in (SourceStatus.running, SourceStatus.stopping):
        raise HTTPException(409, "Stop the source before changing its settings")
    if body.account_id is not None and await db.get(Account, body.account_id) is None:
        raise HTTPException(404, "Download account not found")
    # exclude_unset: a partial body must not clobber untouched fields with
    # schema defaults (e.g. {"max_items": 50} used to reset reels_only=True).
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(r, k, v)
    await db.commit()
    await db.refresh(r)
    total, pending = await _item_counts(db, sid)
    return _out(r, total, pending)


@source_router.delete("/{sid}", status_code=204)
async def delete_source(sid: int, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    r = await db.get(VideoSource, sid)
    if not r:
        raise HTTPException(404, "Source not found")
    if r.status in (SourceStatus.running, SourceStatus.stopping):
        raise HTTPException(409, "Stop the source before deleting it")
    # Items go with the source; downloaded Video rows stay in the library.
    await db.delete(r)
    await db.commit()
    await log_event("INFO", "source", f"Source @{r.username} deleted")
    return None


@source_router.post("/{sid}/start", response_model=SourceOut)
@limiter.limit("10/minute")
async def start_source(request: Request, sid: int, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    # Atomic claim: exactly one runner even under double-clicks.
    res = await db.execute(
        update(VideoSource)
        .where(VideoSource.id == sid, VideoSource.status.in_(_RUNNABLE))
        .values(status=SourceStatus.running, started_at=dt.datetime.now(dt.timezone.utc),
                finished_at=None, last_error=None, current_stage="queued")
    )
    if res.rowcount == 0:
        r = await db.get(VideoSource, sid)
        if not r:
            raise HTTPException(404, "Source not found")
        raise HTTPException(409, f"Source is {r.status.value} — stop it first" if r.status == SourceStatus.stopping else f"Source is already {r.status.value}")
    await db.commit()
    from app.tasks.source_tasks import ingest_source

    ingest_source.delay(sid)
    await log_event("INFO", "source", f"Source #{sid} started")
    r = await db.get(VideoSource, sid)
    assert r is not None
    total, pending = await _item_counts(db, sid)
    return _out(r, total, pending)


@source_router.post("/{sid}/stop", response_model=SourceOut)
@limiter.limit("20/minute")
async def stop_source(request: Request, sid: int, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    r = await db.get(VideoSource, sid)
    if not r:
        raise HTTPException(404, "Source not found")
    if r.status == SourceStatus.stopping:
        raise HTTPException(409, "Stop already requested — finishing current item")
    if r.status != SourceStatus.running:
        raise HTTPException(409, f"Source is {r.status.value}, nothing to stop")
    r.status = SourceStatus.stopping
    await db.commit()
    await db.refresh(r)
    await log_event("INFO", "source", f"Source @{r.username} stop requested")
    total, pending = await _item_counts(db, sid)
    return _out(r, total, pending)


@source_router.post("/{sid}/retry-failed")
async def retry_failed(sid: int, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    r = await db.get(VideoSource, sid)
    if not r:
        raise HTTPException(404, "Source not found")
    if r.status in (SourceStatus.running, SourceStatus.stopping):
        raise HTTPException(409, "Stop the source before retrying")
    res = await db.execute(
        update(SourceItem)
        .where(SourceItem.source_id == sid, SourceItem.status == SourceItemStatus.failed)
        .values(status=SourceItemStatus.pending, error=None)
    )
    await db.commit()
    n = res.rowcount or 0
    await log_event("INFO", "source", f"Source @{r.username}: {n} failed items re-queued")
    return {"reset": n}


@source_router.get("/{sid}/items", response_model=list[SourceItemOut])
async def list_items(
    sid: int, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db),
    status: str | None = Query(default=None), limit: int = Query(default=200, ge=1, le=1000),
):
    r = await db.get(VideoSource, sid)
    if not r:
        raise HTTPException(404, "Source not found")
    q = select(SourceItem).where(SourceItem.source_id == sid).order_by(SourceItem.id.desc()).limit(limit)
    if status:
        try:
            st = SourceItemStatus(status)
        except ValueError:
            raise HTTPException(422, f"Unknown item status '{status}'")
        q = select(SourceItem).where(SourceItem.source_id == sid, SourceItem.status == st).order_by(SourceItem.id.desc()).limit(limit)
    rows = (await db.execute(q)).scalars().all()
    return [
        SourceItemOut(
            id=i.id, media_pk=i.media_pk, shortcode=i.shortcode, media_type=i.media_type,
            status=i.status.value, video_id=i.video_id, error=i.error, created_at=i.created_at,
        )
        for i in rows
    ]
