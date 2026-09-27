"""Video processing celery task — never crashes the worker.

Fully synchronous: no asyncio.run, no event loop. Uses SyncSessionLocal
and the sync FFmpeg pipeline.

Concurrency contract (single-flight + idempotent claim):

- The Redis lock (``lock:process_video:<id>``) serialises *task runs*.
  It carries a random ownership token; release/refresh are Lua
  compare-and-delete scripts, so a run can never release or extend a lock
  it doesn't own (the old code used a constant value + blind DELETE).
- A daemon thread refreshes the TTL while the run is alive, so an encode
  longer than the TTL can't lose the lock mid-run; on crash the thread
  dies with the process and the TTL bounds the wedge.
- The DB claim (``_claim_for_processing``) serialises *intent*: exactly
  one run may move the video into ``processing``. A run that finds the
  video already ``processed`` skips (post-completion duplicate task);
  ``processing`` + a lock we verifiably hold means the previous holder
  died after claiming (or the API pre-flipped the status for this run) —
  safe to reclaim. ``processing`` + an unverified (fail-open) lock is
  never stolen.
- The API pre-flips the status to ``processing`` before queueing so the
  dashboard shows progress immediately; its 409 guard consults the lock
  (``process_lock_held``) so a *stale* processing claim doesn't wedge the
  video forever — the user can retry once the lock TTL expires.
"""
import logging
import os
import shutil
import threading
import uuid

from app.tasks.celery_app import celery

log = logging.getLogger("igfunnel.tasks.video")

#: Single-flight lock for process_video_task, per video id.
_PROCESS_LOCK_PREFIX = "lock:process_video:"
_PROCESS_LOCK_TTL_SECONDS = 1800
#: Refresh at 1/3 TTL — the lock survives arbitrarily long encodes while
#: the process lives, and expires ≤TTL after a crash.
_PROCESS_LOCK_REFRESH_SECONDS = 600

#: Atomic compare-and-delete: only the owner releases its lock.
_LOCK_RELEASE_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""

#: Atomic compare-and-extend: only the owner refreshes its lock.
_LOCK_REFRESH_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("expire", KEYS[1], ARGV[2])
else
    return 0
end
"""

#: Minimum free disk space required before FFmpeg runs (floor; the real
#: requirement scales with the input file below).
_MIN_FREE_DISK_BYTES = 1024**3


def _redis_client():
    import redis

    from app.config import settings

    return redis.from_url(settings.REDIS_URL, decode_responses=True, socket_timeout=5)


def _acquire_process_lock(video_id: int) -> "tuple[str | None, bool]":
    """Take the single-flight lock. Returns (token, verified).

    ``token`` is None when another run holds the lock (caller must skip).
    ``verified`` is False only when Redis was unreachable: the lock could
    not be taken, so the caller proceeds *without* verifiable ownership
    (fail-open — a Redis outage must not wedge video processing) and must
    not steal a ``processing`` claim it can't verify (see
    ``_claim_for_processing``). Refresh/release degrade to no-ops then.
    """
    key = f"{_PROCESS_LOCK_PREFIX}{video_id}"
    token = uuid.uuid4().hex
    try:
        taken = bool(
            _redis_client().set(
                key, token, nx=True, ex=_PROCESS_LOCK_TTL_SECONDS,
            )
        )
    except Exception:
        log.warning("process lock unavailable for video %s — proceeding unverified", video_id)
        return token, False
    return (token, True) if taken else (None, True)


def _release_process_lock(video_id: int, token: str | None) -> None:
    """Release the lock, but only if we still own it (atomic)."""
    if not token:
        return
    try:
        _redis_client().eval(_LOCK_RELEASE_LUA, 1, f"{_PROCESS_LOCK_PREFIX}{video_id}", token)
    except Exception:
        log.warning("process lock release failed for video %s", video_id)


def _refresh_process_lock(video_id: int, token: str) -> bool:
    """Extend the TTL, but only if we still own the lock (atomic)."""
    try:
        return bool(
            _redis_client().eval(
                _LOCK_REFRESH_LUA, 1,
                f"{_PROCESS_LOCK_PREFIX}{video_id}", token,
                _PROCESS_LOCK_TTL_SECONDS,
            )
        )
    except Exception:
        log.warning("process lock refresh failed for video %s", video_id)
        return False


def process_lock_held(video_id: int) -> bool:
    """True when a live lock exists for the video (API 409 guard).

    Fail-closed: when Redis can't be reached we can't prove the previous
    holder is gone, so report held rather than risk a double transcode.
    """
    try:
        return bool(_redis_client().exists(f"{_PROCESS_LOCK_PREFIX}{video_id}"))
    except Exception:
        return True


def _start_lock_refresher(video_id: int, token: str, stop_event: threading.Event) -> threading.Thread:
    """Keep the lock alive while this run is working (daemon thread).

    Threads survive the solo pool's blocking FFmpeg call; on crash the
    thread dies with the process and the TTL bounds the wedge.
    """

    def _loop() -> None:
        while not stop_event.wait(_PROCESS_LOCK_REFRESH_SECONDS):
            _refresh_process_lock(video_id, token)

    t = threading.Thread(
        target=_loop, daemon=True, name=f"proc-lock-refresh-{video_id}")
    t.start()
    return t


def _claim_for_processing(video_id: int, lock_verified: bool) -> str:
    """Decide whether this run should transcode the video.

    Returns one of:

    - ``"process"`` — claimed (``uploaded``/``failed`` → ``processing``),
      or reclaimed a stale ``processing`` claim while verifiably holding
      the lock (previous holder died after claiming, or the API
      pre-flipped the status for this very run).
    - ``"skip_processed"`` — already ``processed`` (duplicate task queued
      after completion); transcoding again would waste CPU and churn
      files. Reprocessing goes through the API, which flips the status
      first.
    - ``"skip_active"`` — ``posting``/``posted``/``archived`` (not our
      business), or ``processing`` with an *unverified* (fail-open) lock:
      another run may genuinely be working, never steal it.
    - ``"gone"`` — the video row no longer exists.
    """
    from sqlalchemy import update

    from app.database import SyncSessionLocal
    from app.models import Video, VideoStatus

    with SyncSessionLocal() as s:
        v = s.get(Video, video_id)
        if v is None:
            return "gone"
        if v.status == VideoStatus.processed:
            return "skip_processed"
        if v.status in (VideoStatus.posting, VideoStatus.posted, VideoStatus.archived):
            return "skip_active"
        if v.status == VideoStatus.processing and not lock_verified:
            return "skip_active"
        # Atomic claim: only a row still in a claimable state flips. The
        # read above is just for the skip reason; this conditional UPDATE
        # is the real mutual exclusion (besides the Redis lock).
        claimable = [VideoStatus.uploaded, VideoStatus.failed]
        if lock_verified:
            # Reclaim a stale processing row, or the API-pre-flipped one.
            claimable.append(VideoStatus.processing)
        n = s.execute(
            update(Video)
            .where(Video.id == video_id, Video.status.in_(claimable))
            .values(status=VideoStatus.processing, failed_reason=None)
        ).rowcount
        s.commit()
        if n:
            return "process"
        # Lost a race between the read and the claim — re-read for an
        # accurate skip reason.
        s.expire_all()
        v2 = s.get(Video, video_id)
        if v2 is None:
            return "gone"
        if v2.status == VideoStatus.processed:
            return "skip_processed"
        return "skip_active"


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
        token, lock_verified = _acquire_process_lock(video_id)
        if token is None:
            log.info("Video %s is already being processed — skipping duplicate task", video_id)
            return {"video_id": video_id, "status": "skipped", "reason": "already processing"}
        stop_refresh = threading.Event()
        if lock_verified:
            _start_lock_refresher(video_id, token, stop_refresh)
        try:
            # Idempotent claim: exactly one run may own the transcode.
            decision = _claim_for_processing(video_id, lock_verified)
            if decision == "gone":
                log.warning("Video %s no longer exists — skipping", video_id)
                return {"video_id": video_id, "status": "skipped", "reason": "video not found"}
            if decision == "skip_processed":
                log.info("Video %s already processed — skipping duplicate task", video_id)
                return {"video_id": video_id, "status": "skipped", "reason": "already processed"}
            if decision == "skip_active":
                log.info("Video %s is already being processed — skipping duplicate task", video_id)
                return {"video_id": video_id, "status": "skipped", "reason": "already processing"}
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
            stop_refresh.set()
            _release_process_lock(video_id, token)
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
