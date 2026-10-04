"""Analytics, logs, global settings, WebSocket feed."""
import asyncio
import csv
import datetime as dt
import io
import json

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

import redis.asyncio as aioredis

from app.api.deps import get_current_admin, get_db
from app.config import settings
from app.core.security import decode_token
from app.models import LogLevel, Post, PostStatus, Setting, SystemLog, Video
from app.schemas.content import LogOut, SettingOut, SettingUpdate
from app.services import analytics_service, log_service

analytics_router = APIRouter()
logs_router = APIRouter()
settings_router = APIRouter()
system_router = APIRouter()


@system_router.get("/stats")
async def system_stats(_: str = Depends(get_current_admin)):
    """Live host + container resources for the Server panel.

    Runs in a thread (psutil blocks ~0.5s for a real CPU sample, the
    Docker socket can stall) so the event loop never waits on it.
    """
    import asyncio

    from app.services import host_stats

    return await asyncio.to_thread(host_stats.full_snapshot)


@system_router.get("/timezone")
async def system_timezone(_: str = Depends(get_current_admin)):
    """The timezone schedule-rule hours are interpreted in.

    The dashboard shows this next to every rule time so there is never any
    doubt which "12:00" a rule means.
    """
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(settings.SCHEDULE_TZ)
    offset = dt.datetime.now(tz).strftime("%z")  # e.g. +0330
    return {
        "tz": settings.SCHEDULE_TZ,
        "label": settings.SCHEDULE_TZ.split("/")[-1].replace("_", " "),
        "utc_offset": f"UTC{offset[:3]}:{offset[3:]}",
    }


@system_router.get("/health")
async def system_health(
    _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)
):
    """Connectivity health of every module the pipeline depends on.

    Powers the dashboard Health page: database, Redis, Celery worker,
    Celery beat (via a per-minute heartbeat key — a live beat process that
    stopped ticking still counts as down), Instagram session files,
    proxies, FFmpeg and disk space. Each check is isolated: one failing
    dependency never hides the state of the others.
    """
    import asyncio
    import os
    import shutil
    import time

    from sqlalchemy import text as sa_text

    from app.models import Account, AccountStatus, Proxy
    from app.services.health_checks import (
        check_beat_sync,
        check_database_sync,
        check_redis_sync,
        check_worker_fast_sync,
        check_worker_slow_sync,
    )
    from app.utils.instagram_helpers import session_path_for

    results: list[dict] = []

    async def run(name: str, label: str, critical: bool, coro):
        started = time.perf_counter()
        try:
            status, message = await coro
        except Exception as exc:  # noqa: BLE001 — a check must never 500 the page
            status, message = "down", f"{type(exc).__name__}: {exc}"
        results.append(
            {
                "name": name,
                "label": label,
                "status": status,
                "critical": critical,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "message": message,
                "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
        )

    async def check_instagram():
        rows = (
            (await db.execute(select(Account).where(Account.status == AccountStatus.active)))
            .scalars()
            .all()
        )
        if not rows:
            return "warn", "No active accounts"
        missing = [
            a.username
            for a in rows
            if not os.path.exists(
                a.session_file_path or session_path_for(a.username, settings.MEDIA_ROOT)
            )
        ]
        if missing:
            return "warn", f"Session file missing for: {', '.join(missing)}"
        return "ok", f"{len(rows)}/{len(rows)} session files present"

    async def check_proxies():
        total = (
            await db.execute(select(func.count(Proxy.id)).where(Proxy.is_active.is_(True)))
        ).scalar() or 0
        if not total:
            return "warn", "No proxies configured — direct connection"
        healthy = (
            await db.execute(
                select(func.count(Proxy.id)).where(
                    Proxy.is_active.is_(True), Proxy.is_healthy.is_(True)
                )
            )
        ).scalar() or 0
        if healthy == total:
            return "ok", f"{healthy}/{total} healthy"
        return "warn", f"Only {healthy}/{total} healthy"

    async def check_ffmpeg():
        path = shutil.which("ffmpeg")
        if path:
            return "ok", f"Found at {path}"
        return "down", "ffmpeg not on PATH — video processing will fail"

    async def check_disk():
        if not os.path.exists(settings.MEDIA_ROOT):
            return "warn", f"{settings.MEDIA_ROOT} does not exist yet"
        usage = shutil.disk_usage(settings.MEDIA_ROOT)
        pct = (usage.used / usage.total * 100) if usage.total else 0
        free_gb = usage.free / 1024**3
        if pct >= 95:
            return "down", f"Disk {pct:.0f}% full ({free_gb:.1f} GB free)"
        if pct >= 85:
            return "warn", f"Disk {pct:.0f}% full ({free_gb:.1f} GB free)"
        return "ok", f"Disk {pct:.0f}% used ({free_gb:.1f} GB free)"

    await run("database", "Database", True, asyncio.to_thread(check_database_sync))
    await run("redis", "Redis", True, asyncio.to_thread(check_redis_sync))
    await run("celery_worker_fast", "Celery worker (fast lane)", True, asyncio.to_thread(check_worker_fast_sync))
    await run("celery_worker_slow", "Celery worker (slow lane)", True, asyncio.to_thread(check_worker_slow_sync))
    await run("celery_beat", "Celery beat", True, asyncio.to_thread(check_beat_sync))
    await run("instagram", "Instagram sessions", False, check_instagram())
    await run("proxies", "Proxies", False, check_proxies())
    await run("ffmpeg", "FFmpeg", False, check_ffmpeg())
    await run("disk", "Disk space", False, check_disk())

    overall = "ok"
    for r in results:
        if r["status"] == "down" and r["critical"]:
            overall = "down"
            break
        if r["status"] in ("down", "warn"):
            overall = "warn"
    return {
        "overall": overall,
        "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "components": results,
    }


@analytics_router.get("/overview")
async def overview(days: int = Query(default=30, ge=1, le=365), _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    data = await analytics_service.overview(db, since)
    data["series"] = await analytics_service.views_over_time(db, days)
    return data


#: Manual analytics refresh: one sweep queued/running at a time. The TTL
#: outlives the slowest sweep (20 posts x sleeps + IG fetches) so a second
#: tap while one is in flight gets a 429 instead of hammering Instagram.
ANALYTICS_REFRESH_LOCK = "analytics:manual_refresh_lock"
ANALYTICS_REFRESH_LOCK_TTL_S = 30 * 60


@analytics_router.post("/refresh")
async def refresh_analytics(_: str = Depends(get_current_admin)):
    """Queue an immediate analytics sweep (slow lane), bypassing the 3h
    per-post minimum interval — the scheduled sweep would otherwise no-op
    on recently checked posts and the button would feel broken."""
    client = None
    try:
        client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        if not await client.set(ANALYTICS_REFRESH_LOCK, "1", nx=True, ex=ANALYTICS_REFRESH_LOCK_TTL_S):
            raise HTTPException(
                status_code=429,
                detail="A refresh is already queued or running — fresh numbers land within a few minutes.",
            )
    except HTTPException:
        raise
    except Exception:
        # Redis down: still allow the trigger; the sweep itself is idempotent
        # and the 3h interval filter on the scheduled path limits overlap.
        pass
    finally:
        if client is not None:
            await client.aclose()
    from app.tasks.celery_app import celery

    celery.send_task("tasks.analytics_tasks.fetch_all_analytics", kwargs={"force_refresh": True})
    return {"status": "queued"}


#: Per-post manual refresh: one in-flight refresh per post. Short TTL — a
#: single post takes seconds (one paced IG lookup), not minutes.
ANALYTICS_POST_REFRESH_LOCK_TTL_S = 5 * 60


@analytics_router.post("/posts/{post_id}/refresh")
async def refresh_post_analytics(post_id: int, _: str = Depends(get_current_admin)):
    """Queue an immediate analytics refresh for one post (slow lane).

    Explicit per-post action — bypasses the per-post minimum interval like
    the full manual refresh does. Per-post Redis lock so double-clicks get
    a 429 instead of stacking duplicate IG lookups.
    """
    client = None
    try:
        client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        if not await client.set(
            f"analytics:post_refresh_lock:{post_id}", "1",
            nx=True, ex=ANALYTICS_POST_REFRESH_LOCK_TTL_S,
        ):
            raise HTTPException(
                status_code=429,
                detail="A refresh for this post is already queued or running.",
            )
    except HTTPException:
        raise
    except Exception:
        # Redis down: still allow the trigger; the task itself is idempotent.
        pass
    finally:
        if client is not None:
            await client.aclose()
    from app.tasks.celery_app import celery

    celery.send_task("tasks.analytics_tasks.fetch_post_analytics", kwargs={"post_id": post_id})
    return {"status": "queued"}


@analytics_router.get("/posts")
async def posts_breakdown(limit: int = Query(default=100, ge=1, le=2000), _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Post).where(Post.status == PostStatus.posted).order_by(desc(Post.posted_at)).limit(limit))).scalars().all()
    return [
        {"id": p.id, "account_id": p.account_id, "views_7d": p.views_7d, "likes_7d": p.likes_7d,
         "comments_7d": p.comments_7d, "engagement_rate": p.engagement_rate, "posted_at": p.posted_at}
        for p in rows
    ]


@analytics_router.get("/accounts")
async def accounts_breakdown(_: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    return await analytics_service.account_comparison(db)


@analytics_router.get("/effects")
async def effects_breakdown(_: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    from app.models import EffectPreset, Video

    # One grouped query — not one aggregate per preset (N+1).
    agg = {
        (r[0] or ""): r[1:]
        for r in (
            await db.execute(
                select(
                    Video.effect_preset,
                    func.count(Post.id),
                    func.avg(Post.engagement_rate),
                    func.coalesce(func.sum(Post.views_7d), 0),
                )
                .join(Video, Video.id == Post.video_id)
                .where(Post.status == PostStatus.posted, Video.effect_preset.is_not(None))
                .group_by(Video.effect_preset)
            )
        ).all()
    }
    presets = (await db.execute(select(EffectPreset))).scalars().all()
    return [
        {"name": p.name, "posts": agg.get(p.name, (0, None, 0))[0],
         "avg_engagement": round(float(agg.get(p.name, (0, None, 0))[1] or 0), 2),
         "views": int(agg.get(p.name, (0, None, 0))[2] or 0)}
        for p in presets
    ]


@analytics_router.get("/audio")
async def audio_breakdown(_: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    """Per-track performance: which trending sound actually drives explore.

    Attribution rides on Video.audio_track (set at processing time), mirroring
    the effects breakdown — no extra columns needed.
    """
    from app.models import AudioTrack, Video

    agg = {
        (r[0] or ""): r[1:]
        for r in (
            await db.execute(
                select(
                    Video.audio_track,
                    func.count(Post.id),
                    func.avg(Post.engagement_rate),
                    func.coalesce(func.sum(Post.views_7d), 0),
                )
                .join(Video, Video.id == Post.video_id)
                .where(Post.status == PostStatus.posted, Video.audio_track.is_not(None))
                .group_by(Video.audio_track)
            )
        ).all()
    }
    tracks = (await db.execute(select(AudioTrack))).scalars().all()
    out = []
    for track in tracks:
        row = agg.get(track.name, (0, None, 0))
        out.append({
            "id": track.id, "name": track.name, "posts": row[0],
            "avg_engagement": round(float(row[1] or 0), 2), "views": int(row[2] or 0),
            "use_count": track.use_count, "is_active": track.is_active,
        })
    return out


@analytics_router.get("/captions")
async def captions_breakdown(_: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    from app.models import CaptionTemplate

    templates = (await db.execute(select(CaptionTemplate))).scalars().all()
    # Same approximation as /captions/{id}/performance: posts whose caption
    # matches the template exactly. No stored column (it was never computed).
    agg = {
        (r[0] or ""): r[1:]
        for r in (
            await db.execute(
                select(
                    Post.caption,
                    func.count(Post.id),
                    func.avg(Post.engagement_rate),
                    func.coalesce(func.sum(Post.views_7d), 0),
                )
                .where(Post.status == PostStatus.posted)
                .group_by(Post.caption)
            )
        ).all()
    }
    out = []
    for t in templates:
        row = agg.get(t.content or "", (0, None, 0))
        out.append({
            "id": t.id, "name": t.name, "use_count": t.use_count,
            "avg_engagement": round(float(row[1] or 0), 2),
        })
    return out


@analytics_router.get("/time-slots")
async def time_slots(_: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    return await analytics_service.time_slot_performance(db)


@analytics_router.get("/best-slots")
async def best_slots(
    account_id: int = Query(...),
    limit: int = Query(default=5, ge=1, le=12),
    _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Golden-hour suggestion for one account (personalized when it has
    enough posted history, global fallback otherwise). Read-only."""
    from app.services import best_slots as slots

    return await slots.best_slots_for_account(db, account_id, limit=limit)


@analytics_router.get("/engagement-heatmap")
async def engagement_heatmap(
    account_id: int | None = Query(default=None),
    days: int = Query(default=90, ge=7, le=365),
    _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """7x24 engagement heatmap (dow x local hour) for the dashboard.
    Per-account when it has enough posted history, global fallback
    otherwise. Read-only."""
    from app.services import engagement_heatmap as heat

    return await heat.heatmap_for_account(db, account_id, days=days)


@analytics_router.get("/funnel")
async def pipeline_funnel(
    _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Pipeline stage counts (sources -> library -> ready -> scheduled ->
    posted) for the dashboard command center. Read-only."""
    from app.services import engagement_heatmap as heat

    return await heat.funnel_counts(db)


@analytics_router.get("/export")
async def export_csv(_: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Post, Video).join(Video, Video.id == Post.video_id).where(Post.status == PostStatus.posted).order_by(desc(Post.posted_at)).limit(2000))).all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "account_id", "posted_at", "views_7d", "likes_7d", "comments_7d", "engagement_rate", "audio_track", "url"])
    for p, v in rows:
        w.writerow([p.id, p.account_id, p.posted_at, p.views_7d, p.likes_7d, p.comments_7d, p.engagement_rate, v.audio_track, p.ig_permalink])
    return PlainTextResponse(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=analytics.csv"})


# ---- Logs ----

@logs_router.get("", response_model=list[LogOut])
async def list_logs(
    level: str | None = Query(default=None),
    category: str | None = Query(default=None),
    search: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    q = select(SystemLog)
    if level:
        try:
            q = q.where(SystemLog.level == LogLevel[level.upper()])
        except KeyError:
            raise HTTPException(400, f"Invalid level (known: {[e.value for e in LogLevel]})")
    if category:
        q = q.where(SystemLog.category == category)
    if search:
        q = q.where(SystemLog.message.ilike(f"%{search}%"))
    q = q.order_by(desc(SystemLog.timestamp)).limit(limit)
    rows = (await db.execute(q)).scalars().all()
    return [LogOut(id=r.id, level=r.level.value, category=r.category, message=r.message, details=r.details, timestamp=r.timestamp) for r in rows]


@logs_router.get("/stats")
async def logs_stats(_: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(SystemLog.level, func.count(SystemLog.id)).group_by(SystemLog.level))).all()
    return {r[0].value: r[1] for r in rows}


@logs_router.delete("", status_code=204)
async def clear_logs(
    older_than_days: int = Query(default=30, ge=0, le=3650),
    _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import delete

    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=older_than_days)
    await db.execute(delete(SystemLog).where(SystemLog.timestamp < cutoff))
    await db.commit()
    return None


# ---- Settings ----

DEFAULT_SETTINGS = {
    "auto_process_on_upload": ("true", "processing"),
    "post_jitter_minutes": ("5", "scheduler"),
    # New-account warm-up: accounts younger than this many days post at most
    # 1/day (anti-ban protection). 0 disables it — e.g. when the Instagram
    # account is years old and was only recently connected here.
    "warmup_days": ("7", "scheduler"),
    # Shadowban / action-block protection (anti-ban). The shadowban scan
    # (tasks.account_tasks.scan_shadowban, every 6h) pauses an account when
    # its recent reels' views collapse vs. its baseline; an action block
    # (Instagram "feedback_required" on upload) cools the account down for
    # action_block_cooldown_hours with a rotated proxy.
    "shadowban_scan_enabled": ("true", "scheduler"),
    "shadowban_pause_hours": ("48", "scheduler"),
    "shadowban_min_baseline_views": ("100", "scheduler"),
    "shadowban_min_recent_posts": ("3", "scheduler"),
    "shadowban_collapse_ratio": ("0.10", "scheduler"),
    "action_block_cooldown_hours": ("24", "scheduler"),
    # Dispatch dedup: a dispatched_at stamp older than this many minutes is
    # treated as a lost dispatch (broker/queue hiccup) and the tick
    # re-dispatches the post instead of losing it. Must comfortably exceed
    # the worst-case slow-lane queue wait, or healthy queued posts get
    # (harmless but wasteful) duplicate queue entries.
    "dispatch_stale_minutes": ("30", "scheduler"),
    "pool_country": ("", "proxy"),
    "pool_require_country": ("false", "proxy"),
    "pool_purge_after_days": ("7", "proxy"),
    "pool_stillborn_hours": ("48", "proxy"),
}

MASKED = "••••••••"


@settings_router.get("", response_model=list[SettingOut])
async def list_settings(_: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    # Read-only: the seeding write that used to live here moved to startup.
    # Defaults for never-persisted keys are overlaid so the contract is
    # unchanged even if seeding didn't run.
    rows = (await db.execute(select(Setting).order_by(Setting.category, Setting.key))).scalars().all()
    have = {s.key for s in rows}
    out = [
        SettingOut(key=s.key, value=(MASKED if s.is_sensitive and s.value else s.value),
                   category=s.category, is_sensitive=s.is_sensitive)
        for s in rows
    ]
    for key, (val, cat) in DEFAULT_SETTINGS.items():
        if key not in have:
            out.append(SettingOut(key=key, value=val, category=cat, is_sensitive=False))
    out.sort(key=lambda s: (s.category, s.key))
    return out


async def seed_default_settings() -> int:
    """Insert missing DEFAULT_SETTINGS rows. Runs once at startup (not in
    GET) — idempotent, safe to call on every boot."""
    from app.database import SessionLocal

    added = 0
    async with SessionLocal() as db:
        for key, (val, cat) in DEFAULT_SETTINGS.items():
            if not await db.get(Setting, key):
                db.add(Setting(key=key, value=val, category=cat))
                added += 1
        await db.commit()
    return added


@settings_router.put("/{key}", response_model=SettingOut, status_code=200)
async def update_setting(
    key: str, body: SettingUpdate,
    _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db),
):
    """Update an existing setting (JSON body: {"value": ...}).

    Arbitrary key creation is rejected: unknown keys 404 with the known key
    list instead of silently polluting the table. (The old query-param
    contract is gone — the dashboard sends a JSON body.)
    """
    if len(key) > 128 or not key.replace("_", "").replace("-", "").replace(".", "").isalnum():
        raise HTTPException(400, "Invalid setting key")
    s = await db.get(Setting, key)
    if not s:
        if key not in DEFAULT_SETTINGS:
            known = sorted(DEFAULT_SETTINGS)
            raise HTTPException(404, f"Unknown setting. Known keys: {known}")
        s = Setting(key=key, value=body.value, category=DEFAULT_SETTINGS[key][1])
        db.add(s)
    else:
        s.value = body.value
        s.updated_at = dt.datetime.now(dt.timezone.utc)
    await db.commit()
    await db.refresh(s)
    return SettingOut(key=s.key, value=(MASKED if s.is_sensitive else s.value), category=s.category, is_sensitive=s.is_sensitive)


@settings_router.post("/test-instagram")
async def test_instagram(
    _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db),
):
    from app.models import Account

    count = (await db.execute(select(func.count(Account.id)))).scalar() or 0
    return {"ok": True, "accounts": count, "note": "Use /accounts/{id}/test-session for a live session check"}


# ---- WebSocket ----

ws_router = APIRouter()

#: App-level heartbeat. The server sends {"event": "ping"} every
#: WS_PING_INTERVAL seconds of idleness and expects any frame back within
#: WS_PONG_TIMEOUT — a missing reply means a half-open socket, which is
#: closed so the client reconnects. Each cycle also re-validates the access
#: token: tokens expire after JWT_EXPIRE_MINUTES, and a long-lived socket
#: must not keep serving a stale session — on expiry the server closes with
#: 4401 and the client reconnects with a fresh token.
WS_PING_INTERVAL = 25.0
WS_PONG_TIMEOUT = 10.0
#: How long the server waits for the auth frame after accepting the socket.
WS_AUTH_TIMEOUT = 5.0
#: Max simultaneous not-yet-authenticated sockets per client IP. Without a
#: cap, an attacker can hold the accept->auth window open on many sockets at
#: once (slowloris-style) and exhaust the server's connection budget — the
#: handshake is cheap for them and expensive for us. Single uvicorn worker
#: in production, so a process-local counter is exact (and still a bound
#: with more workers: workers x cap).
WS_MAX_PENDING_AUTH_PER_IP = 8

#: client-ip -> sockets accepted but not yet authenticated.
_pending_auth: dict[str, int] = {}


def _ws_client_ip(websocket: WebSocket) -> str:
    # Behind nginx, uvicorn's --proxy-headers middleware resolves
    # websocket.client from X-Forwarded-For, taking the rightmost
    # *untrusted* entry. TRUSTED_PROXY_CIDR names the compose subnet (not
    # '*'), so a client-supplied XFF can't spoof this — the entry nginx
    # appended is the one that counts.
    return websocket.client.host if websocket.client else "unknown"


@ws_router.websocket("/ws")
async def ws_feed(websocket: WebSocket):
    ip = _ws_client_ip(websocket)
    pending = _pending_auth.get(ip, 0)
    if pending >= WS_MAX_PENDING_AUTH_PER_IP:
        # Deny during the handshake — never accept, so no unauthenticated
        # socket (and no 5s auth window) is consumed.
        await websocket.close(code=1013)
        return
    _pending_auth[ip] = pending + 1
    try:
        await _ws_feed_inner(websocket)
    finally:
        left = _pending_auth.get(ip, 1) - 1
        if left <= 0:
            _pending_auth.pop(ip, None)
        else:
            _pending_auth[ip] = left


async def _ws_feed_inner(websocket: WebSocket):
    await websocket.accept()
    # Auth arrives as the first message frame ({ "token": ... }), never as a
    # URL query param (URLs are written to access logs).
    token = ""
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=WS_AUTH_TIMEOUT)
        token = (json.loads(raw) or {}).get("token", "")
    except Exception:
        token = ""
    try:
        decode_token(token, expected_type="access")
    except ValueError:
        await websocket.close(code=4401)
        return
    client = None
    try:
        client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        pubsub = client.pubsub()
        await pubsub.subscribe("igfunnel:events")
        await websocket.send_text(json.dumps({"event": "connected"}))
        while True:
            msg = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=WS_PING_INTERVAL
            )
            if msg and msg.get("data"):
                await websocket.send_text(msg["data"])
                continue
            # Idle cycle → heartbeat + token re-validation.
            await websocket.send_text(json.dumps({"event": "ping"}))
            try:
                decode_token(token, expected_type="access")
            except ValueError:
                # Token expired mid-session — force reconnect with a fresh one.
                await websocket.close(code=4401)
                return
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=WS_PONG_TIMEOUT)
            except asyncio.TimeoutError:
                await websocket.close(code=4408)  # heartbeat timeout
                return
    except WebSocketDisconnect:
        pass
    except Exception:
        try:
            await websocket.close()
        except Exception:
            pass
    finally:
        try:
            if client is not None:
                await client.aclose()
        except Exception:
            pass
