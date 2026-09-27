"""Video source ingestion: pull reels from an IG page into the videos pipeline.

Two modes, chosen per run — anonymous FIRST, authenticated as fallback:

- ANONYMOUS (no account needed): newest posts are listed via the public
  web_profile_info endpoint and each reel is pulled with yt-dlp
  (video + cover + caption, no session). Works for public pages as long
  as IG serves the listing endpoint to our IP (datacenter ranges are
  often 429'd — then we fall through to authed mode).
- AUTHED: listing via a download account's session (cheap read-only
  calls), but every item's BYTES still go through yt-dlp anonymously
  first — the session is only spent when anonymous fetch fails.

Either way, per-item pacing, stop handling, the consecutive-failure
breaker and the md5 dedupe (which also catches the same media ingested
once per mode) are shared.
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
    """Beat/API entry: ingest one page until max reached, cursor exhausted,
    stopped, or aborted. Resumable via end_cursor + SourceItem dedupe.

    The authed listing runs in two phases per run:
      1. "what's new" — page newest-first until a whole page brings nothing
         new (everything older was seen before). end_cursor is NOT persisted
         here.
      2. "backfill" — resume from the persisted end_cursor toward older posts
         (or where phase 1 left off when the source never backfilled before).
    end_cursor advances only past fully-processed pages, so a cap/stop/crash
    mid-page re-lists that page next run and the SourceItem dedupe heals it.
    """
    from sqlalchemy import select

    from app.config import settings
    from app.core.security import decrypt_secret
    from app.database import SyncSessionLocal
    from app.models import (
        Account, AccountStatus, SourceItem, SourceItemStatus, SourceStatus, Video, VideoStatus,
        VideoSource,
    )
    from app.services import anon_ingest
    from app.services import media_hash as mh
    from app.services.instagram_service import InstagramService, _classify
    from app.services.video_processor import media_dirs
    from app.tasks import sync_helpers as sched
    from app.tasks.sync_helpers import log_event_sync
    from app.utils import ffmpeg as ff
    from app.utils.instagram_helpers import session_path_for

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
                  current_stage="resolving download account")

            # --- download account: now OPTIONAL (anonymous goes first).
            acc = None
            if source.account_id:
                acc = s.get(Account, source.account_id)
                if acc is None:
                    return fail(s, source, "Download account deleted — pick another or clear it for anonymous-only")
            else:
                cands = s.execute(
                    select(Account).where(Account.status == AccountStatus.active)
                    .order_by(Account.id)
                ).scalars().all()
                for c in cands:
                    if os.path.exists(session_path_for(c.username, settings.MEDIA_ROOT)):
                        acc = c
                        break
            # Proxy (when we have one) helps anonymous pulls too — a clean
            # proxy IP often passes the listing endpoint our datacenter IP fails.
            purl = sched.resolve_proxy_url(s, acc) if acc is not None else None

            dirs = media_dirs()
            max_bytes = settings.MAX_UPLOAD_MB * 1024 * 1024
            cap = min(source.max_items, MAX_ITEMS_HARD_CAP)

            def finalize(item, got_path, work_dirs, fname, caption, cover_src):
                """Validate/move/register one fetched file. Returns video id.

                Raises _Duplicate (→ item skipped) or ValueError (→ failed).
                """
                cover_path = _install_cover(cover_src, dirs) if source.with_covers else None
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

            # --- anonymous listing didn't deliver: figure out why, then authed.
            if anon_err is not None:
                akind = anon_err.split(":", 1)[0]
                log.warning("source %s anonymous listing failed: %s", source_id, anon_err)
                if akind == "not-found":
                    return fail(s, source, anon_err)
                # private pages CAN work with a follower account — fall through.
            if acc is None:
                hint = anon_err or "anonymous listing came back empty"
                return fail(s, source,
                            f"{hint} — and no download account is configured. "
                            "Add an account session, or retry later (listing is IP-rate-limited)")
            if acc.status != AccountStatus.active:
                return fail(s, source,
                            f"Download account @{acc.username} is not active and anonymous listing failed"
                            f" ({anon_err})")

            # --- authed mode: session lists, but BYTES still go anonymous first.
            if not sched.account_reachable(s, acc):
                return fail(s, source, "No healthy proxy route for the download account")
            username, password = acc.username, decrypt_secret(acc.password_enc)
            purl = sched.resolve_proxy_url(s, acc)
            spath = acc.session_file_path or session_path_for(username, settings.MEDIA_ROOT)
            if not os.path.exists(spath):
                return fail(s, source, f"Download account @{username} has no session file")

            svc = InstagramService(proxy_url=purl, session_path=spath)
            cl = svc._make_client(username, request_timeout=60)
            # Session-first check via the shared helper (same as profile ops).
            from app.services.instagram_service import _feed_with_retry

            feed_ok, feed_kind = _feed_with_retry(cl)
            if not feed_ok:
                try:
                    cl.login(username, password)
                    try:
                        cl.dump_settings(spath)
                    except Exception:
                        pass
                except Exception as exc:
                    return fail(s, source, f"login failed: {_classify(exc)}: {exc}")

            # --- resolve the target page.
            _bump(s, source, current_stage=f"resolving @{source.username}")
            try:
                target_id = cl.user_id_from_username(source.username)
            except Exception as exc:
                return fail(s, source, f"page @{source.username} not found: {_classify(exc)}: {exc}")
            try:
                info = cl.user_info(target_id)
            except Exception as exc:
                return fail(s, source, f"could not read page: {_classify(exc)}: {exc}")
            if getattr(info, "is_private", False):
                return fail(s, source, f"@{source.username} is private — follow it from the official app first")

            run_downloaded = 0
            consec_fail = 0

            def run_phase(start_cursor, *, stop_on_known, persist_cursor, stage):
                """Page the listing oldestward from start_cursor, processing
                each media. Returns (cursor_after_last_page, outcome).

                outcome: 'exhausted' (no older posts), 'capped' (run cap
                reached), 'stopped' (user stop) or 'caught_up' (only when
                stop_on_known: a whole page had nothing new — everything
                older was seen before). Raises _RunFailed on fatal errors.

                end_cursor is persisted only after a page is FULLY processed,
                so a cap/stop/crash mid-page re-lists that page next run and
                the SourceItem dedupe heals it — no video is ever skipped
                because an earlier run stopped halfway through its page.
                """
                nonlocal run_downloaded, consec_fail
                cursor = start_cursor
                last_cursor = None
                page = 0
                exhausted = False
                seen_cursors: set = set()
                while True:
                    if _stopped(s, source_id):
                        return last_cursor, "stopped"
                    if run_downloaded >= cap:
                        return last_cursor, "capped"
                    page += 1
                    _bump(s, source,
                          current_stage=f"{stage} @{source.username} (page {page})")
                    try:
                        medias, cursor = cl.user_medias_paginated(
                            target_id, PAGE_SIZE, end_cursor=cursor or "")
                    except Exception as exc:
                        kind = _classify(exc)
                        if kind in ("challenge", "login_required"):
                            raise _RunFailed(
                                fail(s, source, f"listing blocked ({kind}): {exc}"))
                        consec_fail += 1
                        if consec_fail >= MAX_CONSECUTIVE_FAILURES:
                            raise _RunFailed(
                                fail(s, source, f"listing failed {consec_fail}x: {exc}"))
                        _sleep_chunked(s, source_id, 30)
                        continue
                    consec_fail = 0
                    if not medias:
                        return last_cursor, "exhausted"
                    # NOTE: end_cursor is deliberately NOT persisted here (see
                    # docstring) — only `fetched` moves yet.
                    _bump(s, source, fetched=(source.fetched or 0) + len(medias))
                    if not cursor or cursor in seen_cursors:
                        # Falsy cursor = last page; repeated cursor = API loop —
                        # process this page, then stop instead of paging forever.
                        exhausted = True
                    seen_cursors.add(cursor)
                    last_cursor = cursor

                    page_had_unknown = False
                    for m in medias:
                        if _stopped(s, source_id):
                            return last_cursor, "stopped"
                        if run_downloaded >= cap:
                            return last_cursor, "capped"
                        pk = str(getattr(m, "pk", "") or "")
                        if not pk:
                            continue
                        shortcode = getattr(m, "code", None)
                        if _media_known(s, source_id, pk, shortcode):
                            continue
                        page_had_unknown = True
                        mtype = getattr(m, "media_type", 0) or 0
                        ptype = getattr(m, "product_type", "") or ""
                        item = SourceItem(source_id=source_id, media_pk=pk,
                                          shortcode=shortcode, media_type=str(mtype))
                        s.add(item)
                        s.commit()

                        take, reason = should_take_media(mtype, ptype, source.reels_only)
                        if not take:
                            skip_item(item, reason)
                            _publish(source_id)
                            continue

                        item.status = SourceItemStatus.downloading
                        s.commit()
                        _bump(s, source, current_stage=f"downloading @{source.username} #{shortcode or pk}")
                        # Bytes go anonymous first — the session only lists.
                        got_path = None
                        cover_src = None
                        cdir = None
                        dl_dir = None
                        try:
                            from pathlib import Path

                            try:
                                pk_int = int(pk)
                            except (TypeError, ValueError):
                                raise ValueError(f"unparseable media pk {pk!r}")
                            dl_dir = str(Path(dirs["raw"]) / f"src_{source_id}_{pk}")
                            os.makedirs(dl_dir, exist_ok=True)
                            caption = (getattr(m, "caption_text", "") or "")[:4000] or None
                            if shortcode:
                                res, _anon_err = anon_ingest.download_post(
                                    shortcode, dl_dir, proxy=purl,
                                    with_cover=source.with_covers)
                                if res is not None:
                                    got_path, cover_src = res["video"], res["cover"]
                                    caption = res["caption"] or caption
                            if got_path is None:
                                try:
                                    if ptype.lower() == "clips":
                                        got = cl.clip_download(pk_int, Path(dl_dir))
                                    else:
                                        got = cl.video_download(pk_int, Path(dl_dir))
                                except AttributeError:
                                    # Older instagrapi without clip_download: fall back.
                                    got = cl.video_download(pk_int, Path(dl_dir))
                                got_path = str(got)
                                # Cover = the post's own IG cover art.
                                if source.with_covers:
                                    try:
                                        from pathlib import Path as _Path

                                        cdir = str(_Path(dirs["thumbnails"]) / f"srccov_{source_id}_{pk}")
                                        os.makedirs(cdir, exist_ok=True)
                                        cgot = str(cl.photo_download(pk_int, _Path(cdir)))
                                        cover_src = cgot
                                    except Exception as exc:
                                        log.warning("source %s cover failed for %s: %s", source_id, pk, exc)
                            fname = f"{source.username}_{shortcode or pk}.mp4"[:500]
                            vid = finalize(item, got_path, [d for d in (dl_dir, cdir) if d],
                                           fname, caption, cover_src)
                            mark_downloaded(item, vid, fname)
                            run_downloaded += 1
                            consec_fail = 0
                        except _Duplicate as dup:
                            skip_item(item, str(dup))
                        except Exception as exc:
                            kind = _classify(exc)
                            if kind in ("challenge", "login_required"):
                                raise _RunFailed(
                                    fail(s, source, f"download blocked ({kind}): {exc}"))
                            consec_fail = fail_item(item, kind, str(exc), consec_fail, dl_dir, cdir)
                            if consec_fail >= MAX_CONSECUTIVE_FAILURES:
                                raise _RunFailed(fail(
                                    s, source,
                                    f"{consec_fail} consecutive failures, stopping to protect the account: {exc}"))
                        _publish(source_id)
                        if pacing():
                            return last_cursor, "stopped"

                    # The page is fully processed: only now may the backfill
                    # cursor advance past it (phase 2). A cap/stop/crash
                    # mid-page leaves end_cursor behind, so the page is
                    # re-listed next run and dedupe heals the gap.
                    if persist_cursor:
                        _bump(s, source, end_cursor=last_cursor)
                    if exhausted:
                        return last_cursor, "exhausted"
                    if stop_on_known and not page_had_unknown:
                        # Phase 1 pages newest-first: a whole page with
                        # nothing new means everything older was seen before.
                        return last_cursor, "caught_up"

            def stopped_result():
                _bump(s, source, status=SourceStatus.idle, finished_at=_now(),
                      current_stage=None)
                log_event_sync("INFO", "source", f"Source @{source.username} stopped",
                               {"source_id": source_id})
                _publish(source_id)
                return {"source_id": source_id, "status": "stopped"}

            # --- phase 1: what's new -----------------------------------
            # Page newest-first until a whole page brings nothing new (or the
            # cap/listing ends). end_cursor is untouched: the backfill
            # position belongs to phase 2.
            try:
                new_cursor, outcome = run_phase(
                    "", stop_on_known=True, persist_cursor=False,
                    stage="checking for new posts")
            except _RunFailed as rf:
                return rf.result
            if outcome == "stopped":
                return stopped_result()
            if outcome == "capped":
                _finish(s, source, SourceStatus.completed)
                return {"source_id": source_id, "status": "completed",
                        "mode": "authed", "exhausted": False,
                        "downloaded_this_run": run_downloaded}
            if outcome == "exhausted":
                # The whole listing was walked: there is nothing older.
                _finish(s, source, SourceStatus.completed)
                return {"source_id": source_id, "status": "completed",
                        "mode": "authed", "exhausted": True,
                        "downloaded_this_run": run_downloaded}

            # --- phase 2: backfill older posts --------------------------
            # Resume from the persisted cursor. When the source never ran the
            # backfill (e.g. only anonymous runs so far, which don't persist
            # end_cursor), continue where phase 1 left off instead of
            # re-listing the newest page.
            try:
                _, outcome = run_phase(
                    source.end_cursor or new_cursor, stop_on_known=False,
                    persist_cursor=True, stage="listing page")
            except _RunFailed as rf:
                return rf.result
            if outcome == "stopped":
                return stopped_result()
            _finish(s, source, SourceStatus.completed)
            return {"source_id": source_id, "status": "completed", "mode": "authed",
                    "exhausted": outcome == "exhausted",
                    "downloaded_this_run": run_downloaded}
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
