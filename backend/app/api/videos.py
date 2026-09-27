"""Video upload / processing / preview + post history endpoints."""
import asyncio
import hashlib
import logging
import os
import uuid

import aiofiles
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin, get_db, limiter
from app.config import settings
from app.models import Post, PostStatus, Setting, Video, VideoStatus
from app.schemas.video import PostOut, SchedulePostIn, VideoOut, VideoSettingsUpdate
from app.services import realtime
from app.services.log_service import log_event
from app.services.video_processor import media_dirs

router = APIRouter()

log = logging.getLogger("igfunnel.api.videos")


async def _reject_if_live_processing(video_id: int, v) -> None:
    """409 when a run is genuinely working on the video.

    A *stale* ``processing`` status (previous run died after claiming; its
    lock TTL expired) does NOT 409 — it falls through so the user can
    retry, and the task reclaims the stale claim.
    """
    if v.status == VideoStatus.processing:
        from app.tasks.video_tasks import process_lock_held

        if process_lock_held(video_id):
            raise HTTPException(409, "Already processing")
        log.warning("Video %s: re-queueing over a stale processing claim", video_id)

ALLOWED_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
UPLOAD_CHUNK = 4 * 1024 * 1024


def _out(v: Video) -> VideoOut:
    return VideoOut(
        id=v.id, original_filename=v.original_filename, duration=v.duration, file_size=v.file_size,
        status=v.status.value, effect_preset=v.effect_preset, audio_track=v.audio_track,
        custom_filters=v.custom_filters, is_trial=v.is_trial, trial_strategy=v.trial_strategy,
        trim_start=v.trim_start, trim_end=v.trim_end, failed_reason=v.failed_reason,
        source_caption=v.source_caption,
        processed_at=v.processed_at, thumbnail_path=v.thumbnail_path,
        custom_thumbnail_path=v.custom_thumbnail_path, created_at=v.created_at,
    )


async def _audio_map(db: AsyncSession, video_ids: list[int]) -> dict[int, str | None]:
    """Batch audio_track lookup for posts (one query, no N+1)."""
    if not video_ids:
        return {}
    rows = (
        await db.execute(select(Video.id, Video.audio_track).where(Video.id.in_(video_ids)))
    ).all()
    return {vid: audio for vid, audio in rows}


def _post_out(p: Post, audio_track: str | None = None) -> PostOut:
    return PostOut(
        id=p.id, video_id=p.video_id, account_id=p.account_id, ig_media_id=p.ig_media_id,
        ig_permalink=p.ig_permalink, caption=p.caption, hashtags=p.hashtags, status=p.status.value,
        audio_track=audio_track, is_trial=p.is_trial,
        scheduled_for=p.scheduled_for, posted_at=p.posted_at, views_24h=p.views_24h,
        views_7d=p.views_7d, likes_24h=p.likes_24h, engagement_rate=p.engagement_rate,
        fail_reason=p.fail_reason, retry_count=p.retry_count, created_at=p.created_at,
    )


@router.get("", response_model=list[VideoOut])
async def list_videos(
    status: str | None = Query(default=None),
    search: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    q = select(Video)
    if status:
        try:
            q = q.where(Video.status == VideoStatus(status))
        except ValueError:
            raise HTTPException(400, "Invalid status")
    if search:
        q = q.where(Video.original_filename.ilike(f"%{search}%"))
    q = q.order_by(desc(Video.created_at)).limit(limit)
    rows = (await db.execute(q)).scalars().all()
    return [_out(v) for v in rows]


@router.post("/upload", response_model=VideoOut, status_code=201)
@limiter.limit("10/minute")
async def upload_video(
    request: Request,
    file: UploadFile = File(...),
    _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    from app.utils import ffmpeg as ff

    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, f"Unsupported file type {ext}. Allowed: {sorted(ALLOWED_EXT)}")
    dirs = media_dirs()
    tmp_name = f"{uuid.uuid4().hex}{ext}"
    raw_path = os.path.join(dirs["raw"], tmp_name)
    size = 0
    h = hashlib.md5()
    max_bytes = settings.MAX_UPLOAD_MB * 1024 * 1024
    # Fail fast when the client declares a body over the cap (the streaming
    # loop below still enforces it for chunked/lying bodies).
    from app.utils.uploads import reject_oversize_content_length

    reject_oversize_content_length(request, max_bytes, "File")
    try:
        # Stream straight to disk (constant memory, regardless of file size).
        async with aiofiles.open(raw_path, "wb") as f:
            while True:
                chunk = await file.read(UPLOAD_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(413, f"File exceeds {settings.MAX_UPLOAD_MB}MB limit")
                h.update(chunk)
                await f.write(chunk)
    except HTTPException:
        if os.path.exists(raw_path):
            os.remove(raw_path)
        await file.close()
        raise
    except Exception as exc:
        if os.path.exists(raw_path):
            os.remove(raw_path)
        await file.close()
        raise HTTPException(400, f"Upload failed: {exc}")
    finally:
        try:
            await file.close()
        except Exception:
            pass
    if size == 0:
        if os.path.exists(raw_path):
            os.remove(raw_path)
        raise HTTPException(400, "Empty file")
    digest = h.hexdigest()
    dup = (await db.execute(select(Video).where(Video.md5_hash == digest))).scalar_one_or_none()
    if dup:
        os.remove(raw_path)
        raise HTTPException(409, f"Duplicate of video #{dup.id} ({dup.original_filename})")
    # Validate the container before committing: reject corrupt/non-video uploads early.
    try:
        probe = await asyncio.wait_for(asyncio.to_thread(ff.probe_sync, raw_path), timeout=90)
    except Exception:
        os.remove(raw_path)
        raise HTTPException(422, "File is not a valid video (ffprobe validation failed)")
    # Visual dedupe (best-effort, same check as source ingest): the same
    # clip under a different filename is a 409, not a silent double.
    phash: str | None = None
    try:
        from app.services import media_hash as mh

        phash = await asyncio.wait_for(asyncio.to_thread(mh.frame_hash, raw_path), timeout=150)
        if phash:
            near_id = await asyncio.to_thread(mh.find_near_duplicate_id, phash)
            if near_id is not None:
                os.remove(raw_path)
                raise HTTPException(409, f"Near-duplicate of video #{near_id} (visual hash)")
    except HTTPException:
        raise
    except Exception:
        phash = None
    video = Video(
        original_filename=file.filename or tmp_name,
        raw_path=raw_path,
        file_size=size,
        md5_hash=digest,
        phash=phash,
        duration=probe.get("duration"),
    )
    db.add(video)
    await db.commit()
    await db.refresh(video)
    await log_event("INFO", "video", f"Uploaded {video.original_filename} (#{video.id})")
    # The Settings-page toggle is the source of truth; the env default only
    # applies before the row is ever seeded (fresh DBs seed on first /settings read).
    auto_row = await db.get(Setting, "auto_process_on_upload")
    auto_process = (
        auto_row.value.strip().lower() == "true"
        if auto_row and auto_row.value is not None
        else settings.AUTO_PROCESS_ON_UPLOAD
    )
    if auto_process:
        from app.tasks.video_tasks import process_video_task

        process_video_task.delay(video.id)
    return _out(video)


@router.get("/{video_id}", response_model=VideoOut)
async def get_video(video_id: int, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    v = await db.get(Video, video_id)
    if not v:
        raise HTTPException(404, "Video not found")
    return _out(v)


@router.delete("/{video_id}", status_code=204)
async def delete_video(video_id: int, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    from sqlalchemy import func as _func

    v = await db.get(Video, video_id)
    if not v:
        raise HTTPException(404, "Video not found")
    # Deleting would NULL the FK on referencing posts (NOT NULL -> 500) and
    # destroy analytics history — refuse with a clear message instead.
    n_posts = (await db.execute(select(_func.count(Post.id)).where(Post.video_id == video_id))).scalar() or 0
    if n_posts:
        raise HTTPException(409, f"Video has {n_posts} post(s) — delete them first to preserve history")
    # Retire active rules pinned to this video — a dangling pin would wait
    # forever at fire time (and with FK enforcement on, the video delete
    # below would 500). Queue-mode rules are untouched.
    from app.models import ScheduleRule

    pinned = (
        await db.execute(
            select(ScheduleRule).where(
                ScheduleRule.pinned_video_id == video_id, ScheduleRule.is_active.is_(True)
            )
        )
    ).scalars().all()
    for r in pinned:
        r.is_active = False
        r.pinned_video_id = None
    if pinned:
        names = ", ".join(f"'{r.name}'" for r in pinned)
        await log_event("INFO", "schedule", f"Video #{video_id} deleted — retired pinned rule(s): {names}")
    for path in (v.raw_path, v.processed_path, v.thumbnail_path, v.custom_thumbnail_path):
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError:
            pass
    await db.delete(v)
    await db.commit()
    return None


@router.post("/{video_id}/process")
@limiter.limit("20/minute")
async def trigger_process(request: Request, video_id: int, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    v = await db.get(Video, video_id)
    if not v:
        raise HTTPException(404, "Video not found")
    await _reject_if_live_processing(video_id, v)
    from app.tasks.video_tasks import process_video_task

    # Flip to processing NOW so the dashboard shows progress immediately
    # instead of sitting on "uploaded" until the worker picks the task up.
    # The effect always resolves from the video's saved effect_preset inside
    # the task — there is no per-call override (nothing ever passed one).
    prev_status = v.status
    v.status = VideoStatus.processing
    v.failed_reason = None
    await db.commit()
    try:
        process_video_task.delay(video_id, "")
    except Exception:
        # Broker unreachable: roll back so the video isn't wedged in
        # "processing" forever — the user can retry.
        v.status = prev_status
        await db.commit()
        raise HTTPException(503, "Task queue unreachable — the worker may be down, try again in a moment")
    return {"queued": True}


@router.post("/{video_id}/reprocess")
@limiter.limit("20/minute")
async def reprocess(request: Request, video_id: int, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    v = await db.get(Video, video_id)
    if not v:
        raise HTTPException(404, "Video not found")
    await _reject_if_live_processing(video_id, v)
    from app.tasks.video_tasks import process_video_task

    # Same immediate flip as trigger_process: the UI polls on status.
    prev_status = v.status
    v.status = VideoStatus.processing
    v.failed_reason = None
    await db.commit()
    try:
        process_video_task.delay(video_id, "")
    except Exception:
        v.status = prev_status
        await db.commit()
        raise HTTPException(503, "Task queue unreachable — the worker may be down, try again in a moment")
    return {"queued": True}


@router.get("/{video_id}/status")
async def processing_status(video_id: int, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    v = await db.get(Video, video_id)
    if not v:
        raise HTTPException(404, "Video not found")
    progress = await realtime.get_progress(video_id)
    return {"status": v.status.value, "progress": progress, "failed_reason": v.failed_reason}


@router.get("/{video_id}/score")
async def viral_score(video_id: int, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    """Pre-flight viral score (warn-only gate — never blocks posting)."""
    from app.services import viral_score as vs

    return await vs.score_video(db, video_id)


@router.put("/{video_id}/settings", response_model=VideoOut)
async def update_settings(video_id: int, body: VideoSettingsUpdate, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    v = await db.get(Video, video_id)
    if not v:
        raise HTTPException(404, "Video not found")
    if body.trim_start is not None and body.trim_end is not None and body.trim_end <= body.trim_start:
        raise HTTPException(400, "trim_end must be greater than trim_start")
    if body.custom_filters is not None and any(c in body.custom_filters for c in ";[]"):
        # Filtergraph metacharacters would break out of [0:v]...[outv].
        raise HTTPException(400, "custom_filters must be a plain comma chain (no ; [ ])")
    for field in ("effect_preset", "audio_track", "custom_filters", "trim_start", "trim_end",
                    "is_trial", "trial_strategy"):
        val = getattr(body, field)
        if val is not None:
            setattr(v, field, val)
    await db.commit()
    await db.refresh(v)
    return _out(v)


EXT_MIME = {
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
    ".mkv": "video/x-matroska", ".avi": "video/x-msvideo",
}


def _serve(path: str | None, media_type: str | None = None):
    if not path or not os.path.exists(path):
        raise HTTPException(404, "File not found")
    if not media_type:
        media_type = EXT_MIME.get(os.path.splitext(path)[1].lower(), "video/mp4")
    return FileResponse(path, media_type=media_type)


@router.get("/{video_id}/preview")
async def preview(
    video_id: int,
    request: Request,
    token: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    # <video> tags can't send Authorization headers, so playback uses a
    # short-lived signed token (see preview-token). Direct dashboard calls
    # still use the admin JWT. Either one must be valid — never open.
    from fastapi.security import HTTPAuthorizationCredentials
    from fastapi import status as _status

    from app.api.deps import bearer
    from app.core.security import decode_token, verify_preview_token

    authorized = token is not None and verify_preview_token(token, video_id)
    if not authorized:
        creds: HTTPAuthorizationCredentials | None = await bearer(request)
        if creds is None or not creds.credentials:
            raise HTTPException(status_code=_status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
        try:
            decode_token(creds.credentials, expected_type="access")
        except ValueError as exc:
            raise HTTPException(status_code=_status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    v = await db.get(Video, video_id)
    if not v:
        raise HTTPException(404, "Video not found")
    return _serve(v.processed_path or v.raw_path)


@router.get("/{video_id}/preview-token")
async def preview_token(video_id: int, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    from app.core.security import create_preview_token

    v = await db.get(Video, video_id)
    if not v:
        raise HTTPException(404, "Video not found")
    return {"token": create_preview_token(video_id)}


@router.get("/{video_id}/thumbnail")
async def thumbnail(video_id: int, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    v = await db.get(Video, video_id)
    if not v:
        raise HTTPException(404, "Video not found")
    # Custom cover wins; otherwise the auto-extracted frame.
    return _serve(v.custom_thumbnail_path or v.thumbnail_path, "image/jpeg")


THUMB_EXT = {".jpg", ".jpeg", ".png", ".webp"}
THUMB_MAX_BYTES = 5 * 1024 * 1024


@router.post("/{video_id}/thumbnail", response_model=VideoOut)
@limiter.limit("20/minute")
async def upload_thumbnail(
    request: Request,
    video_id: int,
    file: UploadFile = File(...),
    _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Upload a custom cover for this video. Used at post time instead of the
    auto-extracted frame. Non-JPEG input is converted via ffmpeg."""
    import subprocess

    v = await db.get(Video, video_id)
    if not v:
        raise HTTPException(404, "Video not found")
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in THUMB_EXT:
        raise HTTPException(400, f"Unsupported image type {ext}. Allowed: {sorted(THUMB_EXT)}")
    raw = await file.read()
    await file.close()
    if not raw:
        raise HTTPException(400, "Empty file")
    if len(raw) > THUMB_MAX_BYTES:
        raise HTTPException(413, "Image exceeds 5MB limit")
    dirs = media_dirs()
    tmp_in = os.path.join(dirs["thumbnails"], f"upload_{uuid.uuid4().hex}{ext}")
    dst = os.path.join(dirs["thumbnails"], f"custom_{uuid.uuid4().hex}.jpg")
    try:
        with open(tmp_in, "wb") as f:
            f.write(raw)
        if ext in (".jpg", ".jpeg"):
            os.replace(tmp_in, dst)
        else:
            r = subprocess.run(
                ["ffmpeg", "-y", "-i", tmp_in, "-q:v", "3", dst],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
            )
            if r.returncode != 0 or not os.path.exists(dst):
                raise HTTPException(422, "File is not a valid image (ffmpeg conversion failed)")
    except HTTPException:
        raise
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Thumbnail conversion timed out after 60s")
    except Exception as exc:
        raise HTTPException(400, f"Thumbnail upload failed: {exc}")
    finally:
        if os.path.exists(tmp_in):
            os.remove(tmp_in)
    # Replace: drop the previous custom file so covers don't pile up on disk.
    if v.custom_thumbnail_path and os.path.exists(v.custom_thumbnail_path):
        try:
            os.remove(v.custom_thumbnail_path)
        except OSError:
            pass
    v.custom_thumbnail_path = dst
    await db.commit()
    await db.refresh(v)
    await log_event("INFO", "video", f"Custom thumbnail set for video #{video_id}")
    return _out(v)


@router.delete("/{video_id}/thumbnail", response_model=VideoOut)
async def delete_thumbnail(video_id: int, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    """Drop the custom cover — the auto-extracted frame is used again."""
    v = await db.get(Video, video_id)
    if not v:
        raise HTTPException(404, "Video not found")
    if v.custom_thumbnail_path and os.path.exists(v.custom_thumbnail_path):
        try:
            os.remove(v.custom_thumbnail_path)
        except OSError:
            pass
    v.custom_thumbnail_path = None
    await db.commit()
    await db.refresh(v)
    return _out(v)


# ---- Posts ----

posts_router = APIRouter()


@posts_router.get("", response_model=list[PostOut])
async def list_posts(
    status: str | None = Query(default=None),
    account_id: int | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    q = select(Post)
    if status:
        try:
            q = q.where(Post.status == PostStatus(status))
        except ValueError:
            raise HTTPException(400, "Invalid status")
    if account_id:
        q = q.where(Post.account_id == account_id)
    q = q.order_by(desc(Post.created_at)).limit(limit)
    rows = (await db.execute(q)).scalars().all()
    audio = await _audio_map(db, [p.video_id for p in rows])
    return [_post_out(p, audio.get(p.video_id)) for p in rows]


@posts_router.get("/queue", response_model=list[PostOut])
async def post_queue(_: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    rows = (
        await db.execute(
            select(Post)
            .where(Post.status == PostStatus.scheduled)
            .order_by(Post.scheduled_for.asc().nulls_first())
            .limit(100)
        )
    ).scalars().all()
    audio = await _audio_map(db, [p.video_id for p in rows])
    return [_post_out(p, audio.get(p.video_id)) for p in rows]


@posts_router.get("/{post_id}", response_model=PostOut)
async def get_post(post_id: int, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    p = await db.get(Post, post_id)
    if not p:
        raise HTTPException(404, "Post not found")
    audio = await _audio_map(db, [p.video_id])
    return _post_out(p, audio.get(p.video_id))


@posts_router.post("/schedule", response_model=PostOut, status_code=201)
async def schedule_post(body: SchedulePostIn, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    import datetime as dt

    v = await db.get(Video, body.video_id)
    if not v:
        raise HTTPException(404, "Video not found")
    if v.status != VideoStatus.processed:
        raise HTTPException(400, f"Video must be processed first (now: {v.status.value})")
    from app.services import scheduler_service as sched_async

    if await sched_async.video_already_queued(db, body.video_id):
        raise HTTPException(400, "Video already has a pending post")
    account_id = body.account_id
    if not account_id:
        from app.services import scheduler_service

        acc = await scheduler_service.eligible_account(db, None)
        if not acc:
            raise HTTPException(400, "No eligible account available")
        account_id = acc.id
    # Trial-ness lives on the video (strategy included); the post carries a
    # snapshot for display/analytics. Keep both consistent here as the beat does.
    v.is_trial = body.is_trial
    if body.trial_strategy:
        v.trial_strategy = body.trial_strategy
    # Manual scheduling with empty text gets the same weighted auto-fill as
    # the beat (least-used caption first), so one-click flows post complete
    # reels instead of caption-less ones. A harvested source caption wins
    # over the template — it posts verbatim with its own hashtags.
    caption, hashtags = body.caption, body.hashtags
    caption_empty = not (caption or "").strip()
    from_source = caption_empty and bool((v.source_caption or "").strip())
    if caption_empty:
        caption = (v.source_caption or "").strip()
        if not caption:
            caption, _cap_id = await sched_async.pick_caption(db, None)
    if not (hashtags or "").strip() and not from_source:
        hashtags = await sched_async.pick_hashtags(db)
    post = Post(
        video_id=body.video_id, account_id=account_id, caption=caption,
        hashtags=hashtags, status=PostStatus.scheduled,
        scheduled_for=body.scheduled_for or dt.datetime.now(dt.timezone.utc),
        is_trial=body.is_trial,
    )
    db.add(post)
    await db.commit()
    await db.refresh(post)
    return _post_out(post, v.audio_track)


@posts_router.post("/{post_id}/retry")
async def retry_post(post_id: int, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    p = await db.get(Post, post_id)
    if not p:
        raise HTTPException(404, "Post not found")
    if p.status != PostStatus.failed:
        raise HTTPException(400, "Only failed posts can be retried")
    p.status = PostStatus.scheduled
    import datetime as dt

    p.scheduled_for = dt.datetime.now(dt.timezone.utc)
    await db.commit()
    from app.tasks.post_tasks import execute_post

    execute_post.delay(post_id)
    return {"queued": True}


@posts_router.delete("/{post_id}", status_code=204)
async def delete_post(post_id: int, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    p = await db.get(Post, post_id)
    if not p:
        raise HTTPException(404, "Post not found")
    await db.delete(p)
    await db.commit()
    return None
