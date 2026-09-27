"""Periodic tasks â€” all fully synchronous (Celery-safe, no event loop)."""
import datetime as dt
import logging
import os

from app.tasks.celery_app import celery

log = logging.getLogger("igfunnel.tasks")


# Max posts refreshed per analytics run — the rest wait for the next 4h
# tick. Bounds IG API calls and spreads them instead of bursting.
MAX_ANALYTICS_PER_RUN = 20
# Skip posts checked more recently than this (hours).
ANALYTICS_MIN_INTERVAL_HOURS = 3


def _post_age_hours(posted_at: "dt.datetime | None", now: "dt.datetime") -> float:
    """Hours since ``posted_at`` (floored at 1).

    Naive DB timestamps (SQLite) are UTC — compare them against an aware
    ``now`` directly and Python raises ``TypeError: can't subtract
    offset-naive and offset-aware datetimes``.
    """
    from app.tasks.sync_helpers import as_aware_utc

    ref = as_aware_utc(posted_at) or now
    return max(1.0, (now - ref).total_seconds() / 3600)


@celery.task(name="tasks.analytics_tasks.fetch_all_analytics")
def fetch_all_analytics():
    import random
    import time

    from sqlalchemy import select

    from app.config import settings
    from app.core.security import decrypt_secret
    from app.database import SyncSessionLocal
    from app.models import Account, Post, PostStatus
    from app.services.instagram_service import InstagramService
    from app.tasks import sync_helpers as sched
    from app.tasks.sync_helpers import log_event_sync
    from app.utils.instagram_helpers import session_path_for

    try:
        # Jitter the start so runs don't hit IG at the exact same minute daily.
        time.sleep(random.uniform(0, 90))
        now = dt.datetime.now(dt.timezone.utc)
        cutoff = now - dt.timedelta(days=7)
        recent = now - dt.timedelta(hours=ANALYTICS_MIN_INTERVAL_HOURS)
        with SyncSessionLocal() as s:
            posts = (
                s.execute(
                    select(Post)
                    .where(Post.status == PostStatus.posted, Post.posted_at >= cutoff)
                    .order_by(Post.last_analytics_check.asc().nulls_first())
                    .limit(MAX_ANALYTICS_PER_RUN * 2)
                )
            ).scalars().all()
            items = [
                (p.id, p.account_id, p.ig_media_id, p.posted_at)
                for p in posts
                if (last := sched.as_aware_utc(p.last_analytics_check)) is None or last <= recent
            ][:MAX_ANALYTICS_PER_RUN]
        updated = 0
        for pid, acc_id, media_id, posted_at in items:
            # Human-like pacing between API calls instead of a tight loop.
            time.sleep(random.uniform(3, 10))
            if not media_id:
                continue
            try:
                with SyncSessionLocal() as s:
                    acc = s.get(Account, acc_id)
                    if not acc:
                        continue
                    if sched.account_age_days(acc.created_at) < sched.ANALYTICS_MIN_AGE_DAYS:
                        continue
                    if not sched.account_reachable(s, acc):
                        continue
                    username, password = acc.username, decrypt_secret(acc.password_enc)
                    purl = sched.resolve_proxy_url(s, acc)
                svc = InstagramService(
                    proxy_url=purl, session_path=session_path_for(username, settings.MEDIA_ROOT)
                )
                info = svc.media_info(username, media_id)
                if not info:
                    # Failed lookup returns {} — never let it zero out good stats.
                    log.warning("analytics: no data for post %s, keeping previous values", pid)
                    continue
                likes = info.get("like_count", 0)
                comments = info.get("comment_count", 0)
                views = info.get("view_count", 0)
                age_h = _post_age_hours(posted_at, dt.datetime.now(dt.timezone.utc))
                eng = round((likes + comments) / max(1, views) * 100, 2) if views else 0.0
                with SyncSessionLocal() as s:
                    p = s.get(Post, pid)
                    if p:
                        if age_h <= 30:
                            p.views_24h, p.likes_24h, p.comments_7d = views, likes, comments
                        p.views_7d, p.likes_7d, p.comments_7d = views, likes, comments
                        p.engagement_rate = eng
                        p.last_analytics_check = dt.datetime.now(dt.timezone.utc)
                        s.commit()
                        updated += 1
            except Exception:
                log.exception("analytics fetch failed for post %s", pid)
        log_event_sync("INFO", "system", f"Analytics refresh: {updated} posts updated")
        return {"updated": updated}
    except Exception:  # noqa: BLE001
        log.exception("fetch_all_analytics failed")
        return {"error": "failed"}


@celery.task(name="tasks.proxy_tasks.check_all_proxies")
def check_all_proxies():
    """Health-check cycle: oldest-first batch, threaded sweep + verify.

    Network runs on thread pools (a 60-row batch drains in about a minute
    instead of a quarter hour); every DB write stays sequential in this
    thread so SQLite never sees concurrent writers. Disabled rows are
    included so recoveries surface for re-enable.
    """
    import concurrent.futures
    from types import SimpleNamespace

    from sqlalchemy import select

    from app.database import SyncSessionLocal
    from app.models import Proxy
    from app.services.proxy_service import check_proxy_sync
    from app.tasks.sync_helpers import log_event_sync, record_proxy_check
    from app.tasks.sync_helpers import (
        PROXY_CHECK_BATCH,
        PROXY_CHECK_THREADS,
        PROXY_VERIFY_LIMIT,
        PROXY_VERIFY_THREADS,
        SWEEP_TCP_TIMEOUT,
        due_for_check,
    )

    def _run_pool(snaps, workers, **check_kw):
        """Network-only fan-out: returns {pid: (ok, latency)}; a proxy whose
        probe itself explodes counts as failed, never kills the cycle."""
        out = {}

        def _probe(snap):
            try:
                probe = SimpleNamespace(id=snap[0], url=snap[1], username=snap[2], password_enc=snap[3])
                return snap[0], check_proxy_sync(probe, **check_kw)
            except Exception as exc:  # noqa: BLE001 — one bad row must not kill the cycle
                log.exception("proxy probe #%s failed", snap[0])
                return snap[0], (False, None)

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            for pid, res in pool.map(_probe, snaps):
                out[pid] = res
        return out

    try:
        with SyncSessionLocal() as s:
            # Oldest-checked first (never-checked lead), capped per cycle.
            ids = due_for_check(s, PROXY_CHECK_BATCH)
            # Snapshot credentials once, then check WITHOUT holding the DB
            # transaction open — network checks must never pin a connection
            # (SQLite lock contention / PG idle-in-transaction).
            snaps = []
            for pid in ids:
                p = s.get(Proxy, pid)
                if p is not None:
                    snaps.append((p.id, p.url, p.username, p.password_enc))
        # Tier 1 — fast TCP sweep (seconds, not tens of seconds).
        swept, verified = 0, 0
        survivors = []
        for pid, (ok, _lat) in _run_pool(snaps, PROXY_CHECK_THREADS,
                                         tcp_timeout=SWEEP_TCP_TIMEOUT, sweep_only=True).items():
            swept += 1
            if ok:
                survivors.append(pid)
                continue
            with SyncSessionLocal() as s:
                p = s.get(Proxy, pid)
                if p is not None:
                    record_proxy_check(s, p, False, error="tcp sweep failed")
        # Tier 2 — full end-to-end, but only for the first survivors each
        # cycle; the rest wait for the next tick (their sweep already
        # proved TCP liveness, so streaks stay honest).
        verify_ids = set(survivors[:PROXY_VERIFY_LIMIT])
        verify_snaps = [sn for sn in snaps if sn[0] in verify_ids]
        for pid, (ok, latency) in _run_pool(verify_snaps, PROXY_VERIFY_THREADS).items():
            verified += 1
            # One shared recorder for checker AND live-traffic observations —
            # same streak, same threshold, same parking behavior.
            with SyncSessionLocal() as s:
                p = s.get(Proxy, pid)
                if p is None:
                    continue
                record_proxy_check(s, p, ok, latency_ms=latency,
                                   error="" if ok else "periodic check failed")
        from app.tasks.sync_helpers import publish_sync

        log_event_sync("INFO", "system", f"Proxy health check: {swept} swept, {verified} verified")
        # Live UI: dashboards refetch the pool the moment a cycle lands
        # instead of waiting for the next poll or a remount.
        publish_sync("proxy_pool_update", {"swept": swept, "verified": verified})
        return {"swept": swept, "verified": verified}
    except Exception:  # noqa: BLE001
        log.exception("check_all_proxies failed")
        return {"error": "failed"}


@celery.task(name="tasks.proxy_tasks.refresh_proxy_pool")
def refresh_proxy_pool():
    """Auto-pool refresh: fetch enabled sources, insert healthy-candidate rows.

    Insert is cheap and untrusted input goes through the strict line parser;
    actual health is decided later by the 30-min checker. Single-location
    policy (pool_country + pool_require_country Settings) gates inserts.
    Stale auto rows are reaped; manual rows are never touched.
    """
    import random
    import time

    from sqlalchemy import select

    from app.core.security import encrypt_secret
    from app.database import SyncSessionLocal
    from app.models import Proxy, ProxyProtocol, ProxySource
    from app.services.proxy_service import (
        MAX_IMPORT_LINES,
        parse_proxy_line,
        proxy_fingerprint,
    )
    from app.tasks import sync_helpers as sched
    from app.tasks.sync_helpers import (
        POOL_MAX_NEW_PER_SOURCE,
        POOL_REQUIRE_COUNTRY_KEY,
        POOL_COUNTRY_KEY,
        log_event_sync,
        purge_stale_auto_proxies,
    )

    try:
        time.sleep(random.uniform(0, 120))
        with SyncSessionLocal() as s:
            sources = s.execute(
                select(ProxySource).where(ProxySource.is_active.is_(True))
            ).scalars().all()
            snap = [(x.id, x.name, x.url, x.default_protocol, x.default_country) for x in sources]
            pool_country = sched.get_setting(s, POOL_COUNTRY_KEY, "")
            require = sched.get_setting(s, POOL_REQUIRE_COUNTRY_KEY, "false").lower() == "true"
            have = set()
            for p in s.execute(select(Proxy)).scalars().all():
                known, _e = parse_proxy_line(p.url, "http")
                if known:
                    have.add(proxy_fingerprint(known["scheme"], known["host"], known["port"]))
        if not snap:
            return {"sources": 0}
        from app.utils.ssrf import fetch_url_guarded

        total_added = 0
        per_source = []
        for sid, name, url, proto, country in snap:
            added = total = 0
            err = ""
            try:
                # SSRF-guarded: scheme + public-IP validation on the URL and
                # every redirect hop (m1).
                _final_url, text = fetch_url_guarded(url, timeout=30, max_bytes=1024 * 1024)
                lines = text.splitlines()[:MAX_IMPORT_LINES]
                total = len(lines)
                with SyncSessionLocal() as s:
                    made = 0
                    for line in lines:
                        spec, _e = parse_proxy_line(line, proto or "http")
                        if spec is None:
                            continue
                        if not sched.pool_allows_country(
                            spec["country"], country or "", pool_country, require
                        ):
                            continue
                        key = proxy_fingerprint(spec["scheme"], spec["host"], spec["port"])
                        if key in have:
                            continue
                        try:
                            penum = ProxyProtocol(spec["scheme"] if spec["scheme"] != "https" else "http")
                        except ValueError:
                            continue
                        s.add(Proxy(
                            url=f"{spec['scheme']}://{spec['host']}:{spec['port']}",
                            protocol=penum,
                            username=spec["username"] or None,
                            password_enc=encrypt_secret(spec["password"]) if spec["password"] else None,
                            country=spec["country"] or (country or None),
                            source=name,
                            # NEW and unproven: unhealthy until the first
                            # successful check, so traffic never rides it
                            # blind and the UI shows it as new, not dead.
                            is_healthy=False,
                        ))
                        have.add(key)
                        made += 1
                        if made >= POOL_MAX_NEW_PER_SOURCE:
                            break
                    src = s.get(ProxySource, sid)
                    if src is not None:
                        src.last_fetch_at = dt.datetime.now(dt.timezone.utc)
                        src.last_added = made
                        src.last_total = total
                    s.commit()
                    added = made
            except Exception as exc:  # noqa: BLE001 — one bad source must not kill the cycle
                err = str(exc)[:300]
                log.exception("proxy source %s fetch failed", name)
            per_source.append({"source": name, "added": added, "lines": total, "error": err})
            total_added += added
        with SyncSessionLocal() as s:
            try:
                purge_after = int(sched.get_setting(s, "pool_purge_after_days", "7"))
            except ValueError:
                purge_after = 7
            try:
                stillborn = int(sched.get_setting(s, "pool_stillborn_hours", "48"))
            except ValueError:
                stillborn = 48
            purged = purge_stale_auto_proxies(
                s, max_age_days=min(max(purge_after, 1), 30), stillborn_hours=max(stillborn, 1))
            capped = sched.cap_auto_pool(s)
        from app.tasks.sync_helpers import publish_sync

        log_event_sync("INFO", "proxy", f"Pool refresh: {total_added} added, {purged} stale purged, {capped} over cap")
        publish_sync("proxy_pool_update", {"added": total_added, "purged": purged, "capped": capped})
        return {"added": total_added, "purged": purged, "capped": capped, "sources": per_source}
    except Exception:  # noqa: BLE001
        log.exception("refresh_proxy_pool failed")
        return {"error": "failed"}


@celery.task(name="tasks.cleanup_tasks.clean_old_media")
def clean_old_media(days: int = 30):
    from sqlalchemy import select

    from app.database import SyncSessionLocal
    from app.models import Video, VideoStatus
    from app.tasks.sync_helpers import log_event_sync

    try:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
        with SyncSessionLocal() as s:
            olds = (
                s.execute(
                    select(Video).where(Video.status == VideoStatus.posted, Video.processed_at < cutoff)
                )
            ).scalars().all()
            removed = 0
            for v in olds:
                for path in (v.raw_path, v.processed_path):
                    try:
                        if path and os.path.exists(path):
                            os.remove(path)
                            removed += 1
                    except OSError:
                        pass
                v.status = VideoStatus.archived
            s.commit()
        log_event_sync("INFO", "system", f"Media cleanup: {removed} files removed")
        return {"removed": removed}
    except Exception:  # noqa: BLE001
        log.exception("clean_old_media failed")
        return {"error": "failed"}


@celery.task(name="tasks.account_tasks.reset_daily_counts")
def reset_daily_counts():
    from sqlalchemy import update

    from app.database import SyncSessionLocal
    from app.models import Account
    from app.tasks.sync_helpers import log_event_sync

    try:
        with SyncSessionLocal() as s:
            s.execute(update(Account).values(posts_today=0))
            s.commit()
        log_event_sync("INFO", "system", "Daily post counts reset")
        return {"ok": True}
    except Exception:  # noqa: BLE001
        log.exception("reset_daily_counts failed")
        return {"error": "failed"}


# A post row sits in 'posting' only while a worker is actively executing it
# (claim -> pre-post sleep <= 120s -> upload, a few minutes). Retryable
# failures park the row back to 'scheduled' before the celery retry, so
# anything still in 'posting' past this age means the worker died or lost
# the task mid-upload. Well under SIBLING_STALE_HOURS (2h).
REAP_STALE_POSTING_MINUTES = 45


@celery.task(name="tasks.post_tasks.reap_stale_posting")
def reap_stale_posting():
    """Fail posts wedged in 'posting' (worker died mid-upload).

    Without this, a crash between claim_post() and the final set_status()
    leaves the row invisible forever — the due query only sees 'scheduled'
    and the slot is already marked fired (silent loss). Worse, after
    SIBLING_STALE_HOURS find_blocking_sibling() ignores the stale row and a
    later slot can upload the same video twice. The reaper marks such rows
    failed promptly and notifies. The video is quarantined as failed too —
    the upload may or may not have reached Instagram, so the scheduler must
    NOT auto-retry it; it only becomes postable again after the user
    verifies Instagram and reprocesses/retries manually. Never raises.
    """
    from sqlalchemy import select

    from app.database import SyncSessionLocal
    from app.models import Account, Post, PostStatus, Video, VideoStatus
    from app.tasks.sync_helpers import (
        as_aware_utc,
        log_event_sync,
        notify_sync,
        publish_sync,
    )

    try:
        now = dt.datetime.now(dt.timezone.utc)
        cutoff = now - dt.timedelta(minutes=REAP_STALE_POSTING_MINUTES)
        # Filter in Python: updated_at comes back naive from SQLite and
        # string-comparing it against an aware cutoff in SQL is fragile.
        # 'posting' rows are bounded by worker concurrency (a handful).
        reaped = []
        with SyncSessionLocal() as s:
            rows = (
                s.execute(
                    select(Post, Account.username)
                    .join(Account, Post.account_id == Account.id, isouter=True)
                    .where(Post.status == PostStatus.posting)
                )
            ).all()
            for post, username in rows:
                ts = as_aware_utc(post.updated_at)
                if ts is None or ts >= cutoff:
                    continue
                post.status = PostStatus.failed
                post.fail_reason = (
                    f"Stale 'posting' for {REAP_STALE_POSTING_MINUTES}+ min — the worker "
                    "died or lost the task mid-upload. The upload may or may not "
                    "have reached Instagram: verify there before re-posting."
                )
                # Quarantine the video in the same transaction: with only the
                # post failed, the scheduler would auto-pick this processed
                # video at the next slot and risk a double upload.
                video = s.get(Video, post.video_id)
                if video is not None and video.status != VideoStatus.posted:
                    video.status = VideoStatus.failed
                    video.failed_reason = (
                        f"Quarantined by the stale-posting reaper: post #{post.id} "
                        f"sat in 'posting' for {REAP_STALE_POSTING_MINUTES}+ min and "
                        "may have been uploaded. Verify on Instagram, then "
                        "reprocess or retry manually."
                    )
                reaped.append((post.id, username or "?"))
            s.commit()
        for post_id, username in reaped:
            log_event_sync(
                "WARNING", "post",
                f"Post {post_id} to @{username} reaped from stale 'posting'",
                {"post_id": post_id},
            )
            notify_sync(
                "post_failed",
                "critical",
                f"Post to @{username} stalled mid-upload",
                f"Post #{post_id} sat in 'posting' for over {REAP_STALE_POSTING_MINUTES} minutes — "
                "the worker died or lost the task. It was marked failed and its video "
                "quarantined (it will NOT be retried automatically — the upload may "
                "already have reached Instagram). Check Instagram, then reprocess or "
                "retry manually if the video never went up.",
                link="/dashboard/posts",
                dedup_key=f"stale-posting:{post_id}",
            )
            publish_sync("post_status_update", {"post_id": post_id, "status": "failed"})
        return {"reaped": len(reaped)}
    except Exception:  # noqa: BLE001
        log.exception("reap_stale_posting failed")
        return {"error": "failed"}
