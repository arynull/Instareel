"""Video processing celery task — never crashes the worker.

Fully synchronous: no asyncio.run, no event loop. Uses SyncSessionLocal
and the sync FFmpeg pipeline.
"""
import logging
import os
import shutil

from app.tasks.celery_app import celery

log = logging.getLogger("igfunnel.tasks.video")

#: Single-flight lock for process_video_task, per video id.
#: The upload endpoint auto-queues a task while the video is still
#: "uploaded", and the user can also hit Process manually — without a lock
#: two FFmpeg runs would transcode the same video concurrently (wasted CPU,
#: both writing the same output files). TTL bounds the lock if the worker
#: dies mid-run; the API's 409-on-processing guard covers the rest.
_PROCESS_LOCK_PREFIX = "lock:process_video:"
_PROCESS_LOCK_TTL_SECONDS = 1800

#: Minimum free disk space required before FFmpeg runs (floor; the real
#: requirement scales with the input file below).
_MIN_FREE_DISK_BYTES = 1024**3


def _redis_client():
    import redis

    from app.config import settings

    return redis.from_url(settings.REDIS_URL, decode_responses=True, socket_timeout=5)


def _acquire_process_lock(video_id: int) -> bool:
    """True when this run owns the processing lock for the video."""
    try:
        return bool(
            _redis_client().set(
                f"{_PROCESS_LOCK_PREFIX}{video_id}", "1",
                nx=True, ex=_PROCESS_LOCK_TTL_SECONDS,
            )
        )
    except Exception:
        # Fail open: a Redis outage must not wedge video processing.
        # (The worker is solo-pool; the lock matters for multi-worker
        # deploys and double-queued tasks.)
        log.warning("process lock unavailable for video %s — proceeding", video_id)
        return True


def _release_process_lock(video_id: int) -> None:
    try:
        _redis_client().delete(f"{_PROCESS_LOCK_PREFIX}{video_id}")
    except Exception:
        log.warning("process lock release failed for video %s", video_id)


def _ensure_disk_space(raw_path: str | None) -> None:
    """Fail fast when the disk can't hold input + output + temp files.

    FFmpeg dying mid-encode with 'No space left on device' leaves a corrupt
    partial output and a confusing error; check up front instead.
    """
    from app.config import settings

    need = _MIN_FREE_DISK_BYTES
    if raw_path:
        try:
            need = max(need, 2 * os.path.getsize(raw_path))
        except OSError:
            pass
    free = shutil.disk_usage(settings.MEDIA_ROOT).free
    if free < need:
        raise RuntimeError(
            f"Insufficient disk space for processing: {free / 1024**3:.1f} GiB free, "
            f"need {need / 1024**3:.1f} GiB"
        )


@celery.task(name="tasks.video_tasks.process_video", bind=True, max_retries=2)
def process_video_task(self, video_id: int, effect_filter: str = "", color_grade: str = ""):
    import datetime as dt

    from sqlalchemy import select

    from app.database import SyncSessionLocal
    from app.models import EffectPreset, Video, VideoStatus
    from app.services.video_processor import process_video_sync
    from app.tasks import sync_helpers as sched
    from app.tasks.sync_helpers import log_event_sync, publish_sync

    def mark(status: VideoStatus, reason: str | None = None):
        try:
            with SyncSessionLocal() as s:
                v = s.get(Video, video_id)
                if v:
                    v.status = status
                    if reason is not None:
                        v.failed_reason = reason[:2000]
                    s.commit()
        except Exception:
            # DB itself down: log only, so the outer handler can still
            # return a graceful {"status": "failed"} instead of crashing.
            log.exception("mark(%s) failed for video %s", status, video_id)

    try:
        # Single-flight: a second queued task for the same video (upload
        # auto-queue + manual Process click) skips instead of running a
        # concurrent FFmpeg transcode on the same files.
        if not _acquire_process_lock(video_id):
            log.info("Video %s is already being processed — skipping duplicate task", video_id)
            return {"video_id": video_id, "status": "skipped", "reason": "already processing"}
        try:
            mark(VideoStatus.processing)
            # Trending audio: respect the video's chosen track; otherwise pick
            # weighted-random (least-used first). Stored on the video so the
            # processor, scheduler analytics and re-runs all agree on it.
            with SyncSessionLocal() as s:
                video = s.get(Video, video_id)
                if video is not None:
                    chosen = sched.resolve_audio(s, video.audio_track)
                    if chosen is None and (video.audio_track or "").strip():
                        log.warning("Audio track '%s' missing/inactive — picking another", video.audio_track)
                    if chosen is None:
                        chosen = sched.pick_audio(s)
                    if chosen is not None:
                        video.audio_track = chosen.name
                        chosen.use_count = (chosen.use_count or 0) + 1
                    else:
                        video.audio_track = None
                    s.commit()
            if not effect_filter:
                with SyncSessionLocal() as s:
                    video = s.get(Video, video_id)
                    chosen_name = (video.effect_preset or "").strip() if video else ""
                    if chosen_name:
                        # The video stores the preset NAME — resolve it to the
                        # real FFmpeg filter (never feed the raw name to FFmpeg).
                        preset = s.execute(
                            select(EffectPreset).where(
                                EffectPreset.name == chosen_name,
                                EffectPreset.is_active.is_(True),
                            )
                        ).scalars().first()
                        if preset:
                            effect_filter = preset.ffmpeg_filter or ""
                    # Otherwise the filter stays empty: default is NO effect.
                    # A preset is only applied when the user explicitly chose one.
            with SyncSessionLocal() as s:
                _v = s.get(Video, video_id)
                _ensure_disk_space(_v.raw_path if _v else None)
            process_video_sync(video_id, effect_filter, color_grade)
            publish_sync("video_processing_complete", {"video_id": video_id, "status": "processed"})
            log_event_sync("INFO", "video", f"Video {video_id} processed successfully")
            return {"video_id": video_id, "status": "processed"}
        finally:
            _release_process_lock(video_id)
    except Exception as exc:  # noqa: BLE001 — must never crash worker
        log.exception("process_video_task failed for %s", video_id)
        mark(VideoStatus.failed, str(exc))
        publish_sync("video_processing_complete", {"video_id": video_id, "status": "failed"})
        log_event_sync("ERROR", "video", f"Video {video_id} failed: {exc}")
        from app.tasks.sync_helpers import notify_sync

        notify_sync(
            "video_failed",
            "warning",
            f"Video {video_id} processing failed",
            str(exc)[:500],
            link="/dashboard/videos",
            dedup_key=f"video_failed:{video_id}",
        )
        return {"video_id": video_id, "status": "failed", "error": str(exc)}
