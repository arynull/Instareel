"""Video processing celery task — never crashes the worker.

Fully synchronous: no asyncio.run, no event loop. Uses SyncSessionLocal
and the sync FFmpeg pipeline.
"""
import logging

from app.tasks.celery_app import celery

log = logging.getLogger("igfunnel.tasks.video")


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
        process_video_sync(video_id, effect_filter, color_grade)
        publish_sync("video_processing_complete", {"video_id": video_id, "status": "processed"})
        log_event_sync("INFO", "video", f"Video {video_id} processed successfully")
        return {"video_id": video_id, "status": "processed"}
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
