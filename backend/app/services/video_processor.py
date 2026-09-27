"""FFmpeg processing pipeline orchestrator.

Async variant (web API) + fully synchronous variant (Celery workers).
Celery must ONLY use process_video_sync — it never touches an event loop.
"""
import hashlib
import logging
import os
import subprocess
import uuid

from app.config import settings
from app.utils import ffmpeg as ff

log = logging.getLogger("igfunnel.video")


def media_dirs() -> dict[str, str]:
    root = settings.MEDIA_ROOT
    dirs = {
        "raw": os.path.join(root, "raw"),
        "processed": os.path.join(root, "processed"),
        "thumbnails": os.path.join(root, "thumbnails"),
        "audio": os.path.join(root, "audio"),
        "profile_pics": os.path.join(root, "profile_pics"),
    }
    for d in dirs.values():
        os.makedirs(d, exist_ok=True)
    return dirs


def effective_output_duration(probe_duration: float, trim_start: float | None, trim_end: float | None) -> float:
    """Output length after trimming (pure — unit tested).

    The trending track must be looped/cut to THIS, not the raw probe length,
    or a trimmed video ends with a frozen frame under the ducked track.
    """
    total = probe_duration or 0.0
    start = trim_start or 0.0
    if trim_end and trim_end > start:
        return max(0.0, trim_end - start)
    if start:
        return max(0.0, total - start)
    return total


def validate_encode_inputs(info: dict, trim_start: float | None, trim_end: float | None) -> float:
    """Pre-encode sanity checks that fail fast with actionable errors.

    Without these, a trim past EOF (or a file with no video stream) dies
    inside FFmpeg as "Could not open encoder before EOF / frame= 0",
    which says nothing about the actual cause. Returns output length.
    """
    duration = info.get("duration") or 0.0
    if duration < 3:
        raise ValueError(f"Video too short ({duration:.1f}s < 3s)")
    if not info.get("width") or not info.get("height"):
        raise ValueError("File has no video stream — re-upload or re-ingest the source")
    if info.get("video_packets") == 0:
        raise ValueError(
            "File lists a video stream but contains zero video packets (broken download) — "
            "delete it and re-ingest/re-upload the source")
    if trim_start and trim_start >= duration:
        raise ValueError(
            f"Trim start ({trim_start:.1f}s) is past the end of the video ({duration:.1f}s) — "
            "clear the trim range and process again")
    out_len = effective_output_duration(duration, trim_start, trim_end)
    if out_len <= 0:
        raise ValueError("Trim range is empty — trim_end must be greater than trim_start")
    return out_len


def md5_of_file(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve_post_thumbnail_sync(video_id: int) -> str | None:
    """Cover file for instagrapi (Celery-safe, sync).

    Priority: admin custom cover -> auto-extracted frame -> freshly
    extracted frame from the processed video. Returns None only when no
    video file exists to extract from — the caller then uploads without a
    thumbnail (instagrapi's MoviePy fallback, may fail in this image).
    """
    from app.database import SyncSessionLocal
    from app.models import Video

    with SyncSessionLocal() as s:
        video = s.get(Video, video_id)
        if not video:
            return None
        for candidate in (video.custom_thumbnail_path, video.thumbnail_path):
            if candidate and os.path.exists(candidate):
                return candidate
        src = video.processed_path or video.raw_path
        duration = video.duration or 0.0
    if not src or not os.path.exists(src):
        return None
    try:
        dst = os.path.join(media_dirs()["thumbnails"], f"post_{uuid.uuid4().hex}.jpg")
        extract_thumbnail_sync(src, duration or 10.0, dst)
        if os.path.exists(dst):
            with SyncSessionLocal() as s:
                video = s.get(Video, video_id)
                # Don't clobber a custom cover uploaded concurrently.
                if video and not video.custom_thumbnail_path:
                    video.thumbnail_path = dst
                    s.commit()
            return dst
    except Exception:
        log.warning("Thumbnail extraction failed for video %s", video_id, exc_info=True)
    return None


async def extract_thumbnail(src: str, duration: float, dst: str) -> None:
    import asyncio

    at = max(0.1, duration * 0.25)
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-ss", str(at), "-i", src, "-frames:v", "1", "-q:v", "3", dst,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(proc.communicate(), timeout=120)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except OSError:
            pass
        await proc.wait()
        raise RuntimeError("ffmpeg thumbnail timed out after 120s")


def extract_thumbnail_sync(src: str, duration: float, dst: str) -> None:
    """Blocking thumbnail extraction (Celery-safe)."""
    at = max(0.1, duration * 0.25)
    subprocess.run(
        ["ffmpeg", "-y", "-ss", str(at), "-i", src, "-frames:v", "1", "-q:v", "3", dst],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=120,
    )


def process_video_sync(video_id: int, effect_filter: str = "", color_grade: str = "") -> str:
    """Run the full pipeline synchronously. Returns processed_path. Raises on failure."""
    import datetime as dt

    from app.database import SyncSessionLocal
    from app.models import Video, VideoStatus
    from app.tasks.sync_helpers import log_event_sync, set_progress_sync

    with SyncSessionLocal() as s:
        from sqlalchemy import select

        from app.models import EffectPreset

        video = s.get(Video, video_id)
        if not video:
            raise ValueError(f"Video {video_id} not found")
        raw_path = video.raw_path
        trim_start, trim_end = video.trim_start, video.trim_end
        effect_preset = video.effect_preset
        if not effect_filter and effect_preset:
            # effect_preset stores the preset NAME — resolve to its filter.
            preset = s.execute(
                select(EffectPreset).where(
                    EffectPreset.name == effect_preset,
                    EffectPreset.is_active.is_(True),
                )
            ).scalars().first()
            if preset:
                effect_preset = preset.ffmpeg_filter or ""
            else:
                # Unknown name (e.g. deleted preset) — never feed it to FFmpeg.
                effect_preset = ""
        custom_filters = video.custom_filters
        audio_choice = video.audio_track
        video.status = VideoStatus.processing
        s.commit()

    dirs = media_dirs()
    set_progress_sync(video_id, 2, "probing")
    info = ff.probe_sync(raw_path)
    out_len = validate_encode_inputs(info, trim_start, trim_end)
    set_progress_sync(video_id, 8, "validated")

    # Resolve the chosen trending track (name -> file/volume/mode). Unknown
    # names or missing files quietly mean "no music" — never fail the encode.
    from app.tasks.sync_helpers import resolve_audio

    audio_path: str | None = None
    audio_vol, audio_duck = 0.4, False
    if audio_choice:
        with SyncSessionLocal() as s:
            track = resolve_audio(s, audio_choice)
        if track is not None:
            audio_path, audio_vol, audio_duck = track.file_path, track.music_volume, track.duck_original
        else:
            log.warning("Audio track '%s' unresolvable at encode time — proceeding silent", audio_choice)

    # Post-trim output length drives music looping, progress parsing,
    # thumbnail seek and the stored duration — never the raw probe length.
    # (Already validated > 0 above.)
    out_name = f"{uuid.uuid4().hex}.mp4"
    dst = os.path.join(dirs["processed"], out_name)
    cmd = ff.build_command(
        raw_path, dst,
        trim_start=trim_start, trim_end=trim_end,
        effect_filter=effect_filter or (effect_preset or ""),
        custom_filters=custom_filters or "",
        color_grade=color_grade,
        has_audio=info["has_audio"],
        trending_audio=audio_path,
        music_volume=audio_vol,
        duck_original=audio_duck,
        loop_audio_to=out_len,
    )

    def on_progress(pct: float, stage: str):
        set_progress_sync(video_id, 8 + pct * 0.85, stage)

    try:
        ff.run_sync_with_progress(cmd, out_len or info["duration"], on_progress)
    except Exception:
        # A failed encode leaves a partial output — never orphan it.
        try:
            if os.path.exists(dst):
                os.remove(dst)
        except OSError:
            pass
        raise

    thumb_name = f"{uuid.uuid4().hex}.jpg"
    thumb_path = os.path.join(dirs["thumbnails"], thumb_name)
    try:
        extract_thumbnail_sync(dst, out_len, thumb_path)
    except Exception:
        log.warning("Thumbnail extraction failed for video %s", video_id)
        thumb_path = None

    digest = md5_of_file(dst)
    with SyncSessionLocal() as s:
        video = s.get(Video, video_id)
        if video:
            video.processed_path = dst
            video.thumbnail_path = thumb_path
            video.duration = out_len
            video.file_size = os.path.getsize(dst)
            video.processed_at = dt.datetime.now(dt.timezone.utc)
            video.status = VideoStatus.processed
            s.commit()
    log_event_sync("INFO", "video", f"Video {video_id} processed", {"md5": digest})
    set_progress_sync(video_id, 100, "done")
    return dst


async def process_video(
    video_id: int,
    get_video,   # async callable returning a DB-attached Video
    save_video,  # async callable persisting changes
    effect_filter: str = "",
    color_grade: str = "",
    trending_audio: str | None = None,
    music_volume: float = 0.4,
    duck_original: bool = False,
) -> str:
    """Async pipeline (web API use). Celery workers must use process_video_sync.

    Trending audio is passed pre-resolved (path/volume/mode) by the caller —
    this path has no DB session for a name lookup. Currently caller-less;
    kept at parity so a future caller gets music instead of silence.
    """
    from app.services import realtime

    video = await get_video(video_id)
    dirs = media_dirs()
    await realtime.set_progress(video_id, 2, "probing")
    info = await ff.probe(video.raw_path)
    if info["duration"] < 3:
        raise ValueError(f"Video too short ({info['duration']:.1f}s < 3s)")
    await realtime.set_progress(video_id, 8, "validated")

    out_name = f"{uuid.uuid4().hex}.mp4"
    dst = os.path.join(dirs["processed"], out_name)
    cmd = ff.build_command(
        video.raw_path, dst,
        trim_start=video.trim_start, trim_end=video.trim_end,
        effect_filter=effect_filter or (video.effect_preset or ""),
        custom_filters=video.custom_filters or "",
        color_grade=color_grade,
        has_audio=info["has_audio"],
        trending_audio=trending_audio,
        music_volume=music_volume,
        duck_original=duck_original,
        loop_audio_to=effective_output_duration(info["duration"], video.trim_start, video.trim_end),
    )

    async def on_progress(pct: float, stage: str):
        await realtime.set_progress(video_id, 8 + pct * 0.85, stage)

    try:
        await ff.run_with_progress(cmd, info["duration"], on_progress)
    except Exception:
        # A failed encode leaves a partial output — never orphan it.
        try:
            if os.path.exists(dst):
                os.remove(dst)
        except OSError:
            pass
        raise

    thumb_name = f"{uuid.uuid4().hex}.jpg"
    thumb_path = os.path.join(dirs["thumbnails"], thumb_name)
    try:
        await extract_thumbnail(dst, info["duration"], thumb_path)
    except Exception:
        log.warning("Thumbnail extraction failed for video %s", video_id)
        thumb_path = None

    digest = md5_of_file(dst)
    await save_video(video_id, {
        "processed_path": dst,
        "thumbnail_path": thumb_path,
        "duration": info["duration"],
        "file_size": os.path.getsize(dst),
        "md5_hash_processed": digest,
        "processed_at": True,
    })
    await realtime.set_progress(video_id, 100, "done")
    return dst
