"""Video source ingestion: pull reels from an IG page into the videos pipeline.

Strictly ANONYMOUS — no IG account or session file is ever touched:

- Listing via the public web_profile_info endpoint and each reel pulled
  with yt-dlp (video + cover + caption, no session). Works for public
  pages as long as IG serves the listing endpoint to our route: direct
  server IP first, otherwise the healthiest spare proxy from the pool
  (datacenter ranges are often 429'd).
- There is deliberately NO authenticated fallback. Bulk listing and
  media downloads through an account session is exactly the pattern
  Instagram flags as scraping — it cost us killed sessions — while the
  user only ever sources public pages, so a session buys nothing.

Per-item pacing, the consecutive-failure breaker and the md5 / visual /
caption dedupe are shared. When anonymous listing fails, the run fails
with an actionable error (retry later / check the proxy pool) instead
of burning a session.
"""
import datetime as dt
import hashlib
import logging
import os
import random
import shutil
import time
import uuid

log = logging.getLogger("igfunnel.sources")

PAGE_SIZE = 20
MAX_CONSECUTIVE_FAILURES = 5
MAX_ITEMS_HARD_CAP = 200
ALLOWED_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi"}

from sqlalchemy import or_, select

from app.tasks.celery_app import celery


class _RunFailed(Exception):
    """Fatal run error. Carries fail()'s result dict up to the phase driver."""

    def __init__(self, result: dict):
        super().__init__("source run failed")
        self.result = result


def _media_known(s, source_id: int, pk: str, shortcode: "str | None") -> bool:
    """True when this media was listed by this source before (any mode).

    media_pk holds the numeric pk in authed mode but the shortcode in
    anonymous mode — match either, so a re-run never re-downloads a video
    no matter which mode listed it first. (Same bytes arriving anyway are
    still caught later by the md5 dedupe in finalize.)
    """
    # Imported lazily: this module is imported by celery_app at worker boot,
    # and models import celery-flavoured modules elsewhere.
    from app.models import SourceItem

    conds = [SourceItem.media_pk == pk]
    if shortcode:
        conds.append(SourceItem.media_pk == shortcode)
    return (
        s.execute(
            select(SourceItem.id).where(
                SourceItem.source_id == source_id, or_(*conds)
            )
        ).first()
        is not None
    )


def should_take_media(media_type: int, product_type: str, reels_only: bool) -> "tuple[bool, str]":
    """Pure gate: which IG media become Video rows. Unit-tested.

    Only single-file videos are ingestible (photos can't be processed,
    albums are multi-file). reels_only narrows further to clips.
    """
    product = (product_type or "").lower()
    if media_type == 1:
        return False, "photo — video pipeline only"
    if media_type == 8:
        return False, "album — multi-file, unsupported"
    if media_type != 2:
        return False, f"unsupported media_type={media_type}"
    if reels_only and product != "clips":
        return False, "not a reel (feed video)"
    return True, ""


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _publish(source_id: int):
    try:
        from app.tasks.sync_helpers import publish_sync

        publish_sync("video_source_update", {"source_id": source_id})
    except Exception:
        pass


def _bump(s, source, **kw):
    for k, v in kw.items():
        setattr(source, k, v)
    s.commit()


def _stopped(s, source_id: int) -> bool:
    from app.models import SourceStatus, VideoSource

    row = s.get(VideoSource, source_id)
    return row is None or row.status == SourceStatus.stopping


def _sleep_chunked(s, source_id: int, seconds: float) -> bool:
    """Sleep in 2s slices so Stop lands fast. Returns True if stop requested."""
    end = time.time() + max(seconds, 0)
    while time.time() < end:
        if _stopped(s, source_id):
            return True
        time.sleep(min(2.0, end - time.time()))
    return _stopped(s, source_id)


def _md5_of(path: str) -> "tuple[int, str]":
    h = hashlib.md5()
    size = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
            size += len(chunk)
            h.update(chunk)
    return size, h.hexdigest()


def _convert_cover(src: str, dst: str) -> bool:
    import subprocess

    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", src, "-q:v", "3", dst],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
        )
        return r.returncode == 0 and os.path.exists(dst)
    except Exception:
        return False


class _Duplicate(Exception):
    """Already in the library: same bytes (md5), same look (pHash) or same
    caption. Carries the existing video id; note names the match kind."""

    def __init__(self, video_id: int, note: str = ""):
        msg = f"duplicate of video #{video_id}" + (f" ({note})" if note else "")
        super().__init__(msg)
        self.video_id = video_id


def _install_cover(cover_src: "str | None", dirs) -> "str | None":
    """Move/convert a fetched cover into thumbnails/. Never raises —
    a bad cover must not fail the whole video (logged, video kept)."""
    if not cover_src or not os.path.exists(cover_src):
        return None
    try:
        cext = os.path.splitext(cover_src)[1].lower()
        cdst = os.path.join(dirs["thumbnails"], f"custom_{uuid.uuid4().hex}.jpg")
        if cext in (".jpg", ".jpeg"):
            shutil.move(cover_src, cdst)
        elif not _convert_cover(cover_src, cdst):
            return None
        return cdst
    except Exception as exc:
        log.warning("cover install failed for %s: %s", cover_src, exc)
        return None


def _finish(s, source, status, error: "str | None" = None):
    from app.models import SourceStatus
    from app.tasks.sync_helpers import log_event_sync

    _bump(s, source, status=status, finished_at=_now(),
          last_error=(error[:1000] if error else None), current_stage=None)
    msg = f"Source @{source.username}: {status.value}" + (f" — {error}" if error else "")
    log_event_sync("INFO" if status == SourceStatus.completed else "ERROR", "source", msg,
                   {"source_id": source.id})
    _publish(source.id)


@celery.task(name="tasks.source_tasks.ingest_source", bind=True)
def ingest_source(self, source_id: int):
    """Beat/API entry: list a public page anonymously and pull new reels.

    Anonymous-only, by design: no account, no session file, no login —
    ever. Bulk listing/downloads through a session is the pattern
    Instagram flags as scraping. A failed anonymous listing fails the run
    (retry later / check the proxy pool) instead of burning a session.

    Resumable via SourceItem dedupe: a re-run never re-downloads a video
    no matter what an earlier run already listed.
    """
    from sqlalchemy import select

    from app.config import settings
    from app.database import SyncSessionLocal
    from app.models import (
        SourceItem, SourceItemStatus, SourceStatus, Video, VideoSource,
    )
    from app.services import anon_ingest
    from app.services import media_hash as mh
    from app.services.instagram_service import _classify
    from app.services.video_processor import media_dirs
    from app.tasks import sync_helpers as sched
    from app.tasks.sync_helpers import log_event_sync
    from app.utils import ffmpeg as ff

    def fail(s, source, error: str):
        _finish(s, source, SourceStatus.failed, error)
        return {"source_id": source_id, "status": "failed", "error": error}

    try:
        with SyncSessionLocal() as s:
            source = s.get(VideoSource, source_id)
            if source is None:
                return {"error": "source not found"}
            if source.status != SourceStatus.running:
                return {"status": source.status.value, "note": "not claimed"}
            _bump(s, source, started_at=_now(), finished_at=None, last_error=None,
                  current_stage="preparing anonymous pull")

            # --- proxy for anonymous pulls (no account involved): a clean
            # proxy IP often passes the listing endpoint our datacenter IP
            # fails. Best healthy spare from the pool; None = direct.
            from app.services.proxy_service import proxy_url_for

            purl = proxy_url_for(sched.pick_spare_proxy(s))

            dirs = media_dirs()
            max_bytes = settings.MAX_UPLOAD_MB * 1024 * 1024
            cap = min(source.max_items, MAX_ITEMS_HARD_CAP)

            def finalize(item, got_path, work_dirs, fname, caption, cover_src):
                """Validate/move/register one fetched file. Returns video id.

                Raises _Duplicate (→ item skipped) or ValueError (→ failed).
                """
                cover_path = _install_cover(cover_src, dirs) if source.with_covers else None
                try:
                    ext = os.path.splitext(got_path)[1].lower() or ".mp4"
                    if ext not in ALLOWED_EXT:
                        raise ValueError(f"unsupported container {ext}")
                    size = os.path.getsize(got_path)
                    if size == 0:
                        raise ValueError("empty download")
                    if size > max_bytes:
                        raise ValueError(f"file exceeds {settings.MAX_UPLOAD_MB}MB limit")
                    dest = os.path.join(dirs["raw"], f"{uuid.uuid4().hex}{ext}")
                    shutil.move(got_path, dest)
                    for wd in work_dirs:
                        try:
                            shutil.rmtree(wd, ignore_errors=True)
                        except Exception:
                            pass
                    size, digest = _md5_of(dest)
                    dup = s.execute(
                        select(Video.id, Video.original_filename).where(Video.md5_hash == digest)
                    ).first()
                    if dup:
                        os.remove(dest)
                        raise _Duplicate(dup[0])
                    try:
                        probe = ff.probe_sync(dest)
                    except Exception:
                        os.remove(dest)
                        raise ValueError("ffprobe validation failed")
                    # Visual + caption dedupe (best-effort): md5 above only
                    # catches byte-identical files; the same clip under a new
                    # media id / re-encode is caught here and skipped, never failed.
                    # A computed phash is kept even when the similarity lookup
                    # itself errors — the hash is still valid for future rows.
                    phash = None
                    try:
                        phash = mh.frame_hash(dest)
                    except Exception:
                        phash = None
                    if phash:
                        try:
                            near = mh.find_near_duplicate(s, phash)
                            if near is not None:
                                os.remove(dest)
                                raise _Duplicate(near.id, "visual hash")
                            cap = mh.find_caption_duplicate(s, caption)
                            if cap is not None:
                                os.remove(dest)
                                raise _Duplicate(cap.id, "same caption")
                        except _Duplicate:
                            raise
                        except Exception:
                            pass
                    video = Video(
                        original_filename=fname, raw_path=dest,
                        file_size=size, md5_hash=digest, phash=phash,
                        duration=(probe or {}).get("duration"),
                        custom_thumbnail_path=cover_path,
                        source_caption=caption,
                    )
                    s.add(video)
                    s.flush()
                    return video.id
                except Exception:
                    # The cover was already moved into thumbnails/ — a later
                    # dup/validation failure must not orphan it.
                    if cover_path:
                        try:
                            if os.path.exists(cover_path):
                                os.remove(cover_path)
                        except OSError:
                            pass
                    raise

            def mark_downloaded(item, video_id, fname):
                item.status = SourceItemStatus.downloaded
                item.video_id = video_id
                item.error = None
                s.commit()
                _bump(s, source, downloaded=(source.downloaded or 0) + 1)
                log_event_sync("INFO", "video",
                               f"Sourced {fname} (#{video_id}) from @{source.username}",
                               {"source_id": source_id, "video_id": video_id})
                if source.auto_process:
                    from app.tasks.video_tasks import process_video_task

                    process_video_task.delay(video_id)

            def fail_item(item, kind, detail, consec_fail, *work_dirs):
                item.status = SourceItemStatus.failed
                item.error = f"{kind}: {detail}"[:1000]
                s.commit()
                for wd in work_dirs:
                    if not wd:
                        continue
                    try:
                        shutil.rmtree(wd, ignore_errors=True)
                    except Exception:
                        pass
                _bump(s, source, failed_count=(source.failed_count or 0) + 1)
                return consec_fail + 1

            def skip_item(item, reason):
                item.status = SourceItemStatus.skipped
                item.error = reason
                s.commit()
                _bump(s, source, skipped=(source.skipped or 0) + 1)

            def pacing():
                return _sleep_chunked(s, source_id, random.uniform(source.delay_min_s, source.delay_max_s))

            # --- anonymous listing first: one cheap request, zero session burn.
            done_total = (source.downloaded or 0) + (source.skipped or 0) + (source.failed_count or 0)
            want = min(cap + done_total + 10, 250)
            _bump(s, source, current_stage=f"anonymous listing @{source.username}")
            anon_items, anon_err = anon_ingest.list_public_posts(
                source.username, limit=want, proxy=purl)
            if anon_err is None and anon_items:
                _bump(s, source, current_stage=f"anonymous pull @{source.username} (no account)")
                run_downloaded = 0
                consec_fail = 0
                stopped_early = False
                for entry in anon_items:
                    if _stopped(s, source_id):
                        stopped_early = True
                        break
                    if run_downloaded >= cap:
                        break
                    sc = entry["shortcode"]
                    exists = s.execute(
                        select(SourceItem.id).where(
                            SourceItem.source_id == source_id, SourceItem.media_pk == sc)
                    ).first()
                    if exists:
                        continue
                    item = SourceItem(source_id=source_id, media_pk=sc,
                                      shortcode=sc, media_type="2")
                    s.add(item)
                    s.commit()
                    if not entry["is_video"]:
                        skip_item(item, "photo — video pipeline only")
                        _publish(source_id)
                        continue
                    if source.reels_only and entry["product_type"] and entry["product_type"] != "clips":
                        skip_item(item, "not a reel (feed video)")
                        _publish(source_id)
                        continue
                    item.status = SourceItemStatus.downloading
                    s.commit()
                    _bump(s, source, current_stage=f"downloading @{source.username} #{sc} (anonymous)")
                    from pathlib import Path

                    dl_dir = str(Path(dirs["raw"]) / f"src_{source_id}_{sc}")
                    os.makedirs(dl_dir, exist_ok=True)
                    try:
                        res, err = anon_ingest.download_post(
                            sc, dl_dir, proxy=purl, with_cover=source.with_covers)
                        if err is not None or res is None:
                            raise ValueError(err or "empty result")
                        caption = res["caption"] or entry["caption"]
                        fname = f"{source.username}_{sc}.mp4"[:500]
                        vid = finalize(item, res["video"], [dl_dir], fname, caption, res["cover"])
                        mark_downloaded(item, vid, fname)
                        run_downloaded += 1
                        consec_fail = 0
                    except _Duplicate as dup:
                        skip_item(item, str(dup))
                    except Exception as exc:
                        emsg = str(exc)
                        kind = emsg.split(":", 1)[0] if ":" in emsg[:32] else _classify(exc)
                        if kind == "auth":
                            return fail(s, source, f"page went private mid-run: {exc}")
                        consec_fail = fail_item(item, kind, emsg, consec_fail, dl_dir)
                        if consec_fail >= MAX_CONSECUTIVE_FAILURES:
                            return fail(s, source,
                                        f"{consec_fail} consecutive failures, stopping: {exc}")
                    _publish(source_id)
                    if pacing():
                        # pacing() True == stop requested mid-loop. Without
                        # this flag the run falls through to 'completed'
                        # even though the user stopped it.
                        stopped_early = True
                        break
                if stopped_early:
                    _bump(s, source, status=SourceStatus.idle, finished_at=_now(),
                          current_stage=None)
                    log_event_sync("INFO", "source", f"Source @{source.username} stopped",
                                   {"source_id": source_id})
                    _publish(source_id)
                    return {"source_id": source_id, "status": "stopped"}
                _finish(s, source, SourceStatus.completed)
                mode = "anonymous (no account)"
                log_event_sync("INFO", "source",
                               f"Source @{source.username} completed {mode}: +{run_downloaded} videos",
                               {"source_id": source_id})
                return {"source_id": source_id, "status": "completed",
                        "mode": "anonymous", "downloaded_this_run": run_downloaded}

            # --- anonymous listing didn't deliver: fail cleanly. Source
            # ingest is anonymous-only by design and NEVER falls back to
            # an account session — bulk listing/downloads through a session
            # is exactly the pattern Instagram flags as scraping, and it
            # already cost us killed sessions.
            if anon_err is not None:
                akind = anon_err.split(":", 1)[0]
                log.warning("source %s anonymous listing failed: %s", source_id, anon_err)
                if akind == "not-found":
                    return fail(s, source, anon_err)
                return fail(
                    s, source,
                    f"{anon_err} — source ingest never uses an account session; "
                    "retry later or check the proxy pool health")
            # No error and nothing listed: the page simply has nothing new.
            _finish(s, source, SourceStatus.completed)
            return {"source_id": source_id, "status": "completed",
                    "mode": "anonymous", "downloaded_this_run": 0}
    except Exception as exc:  # noqa: BLE001
        log.exception("ingest_source %s failed", source_id)
        try:
            with SyncSessionLocal() as s2:
                source2 = s2.get(VideoSource, source_id)
                if source2 is not None:
                    _finish(s2, source2, SourceStatus.failed, str(exc))
        except Exception:
            pass
        return {"source_id": source_id, "status": "failed", "error": str(exc)}
