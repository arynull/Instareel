"""Bios, proxies, effects, audio tracks."""
import datetime as dt
import os
import uuid

import aiofiles
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin, get_db, limiter
from app.core.security import decrypt_secret, encrypt_secret
from app.models import Account, AudioTrack, BioConfig, EffectPreset, Proxy, ProxyProtocol, ProxySource
from app.schemas.account import ProxyCreate, ProxyOut, ProxyUpdate
from app.schemas.content import (
    AudioIn, AudioOut, AudioUpdate, BioApplyIn, BioIn, BioOut, BioUpdate, EffectIn, EffectOut,
    EffectUpdate, ProxySourceIn, ProxySourceOut, ProxySourceUpdate,
)
from app.services.log_service import log_event

bio_router = APIRouter()
proxy_router = APIRouter()
effect_router = APIRouter()
audio_router = APIRouter()

AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
MAX_AUDIO_BYTES = 50 * 1024 * 1024


def _audio_out(t: AudioTrack) -> AudioOut:
    return AudioOut(
        id=t.id, name=t.name, description=t.description, music_volume=t.music_volume,
        duck_original=t.duck_original, is_active=t.is_active, file_path=t.file_path,
        duration=t.duration, use_count=t.use_count,
    )


# ---- Bios (bio text + link + full name + picture + privacy) ----

async def _await_blocking(fn, /, *args, timeout: float, **kwargs):
    """Run a blocking callable in a worker thread and await it *without*
    blocking the event loop.

    Several endpoints used ``pool.submit(...).result(timeout)`` directly on
    the loop thread; with a single uvicorn worker that froze the whole
    dashboard for up to ``timeout`` seconds — the phone view hits
    GET /bios/{id}/current on every mount, so one slow Instagram read hung
    the entire site. ``asyncio.to_thread`` yields control while the thread
    works, so other requests keep flowing; on timeout the worker is left
    running detached and its late result is discarded.

    Raises ``concurrent.futures.TimeoutError`` on timeout so the existing
    ``except concurrent.futures.TimeoutError`` handlers keep working.
    ``timeout`` is keyword-only so it never leaks into ``fn``'s kwargs.
    """
    import asyncio
    import concurrent.futures

    try:
        return await asyncio.wait_for(asyncio.to_thread(fn, *args, **kwargs), timeout)
    except (asyncio.TimeoutError, concurrent.futures.TimeoutError) as exc:
        raise concurrent.futures.TimeoutError(f"timed out after {timeout:g}s") from exc


async def _resolve_route(acc_id: int) -> "tuple[bool, str | None]":
    """(reachable, proxy_url) for an account, callable from async endpoints.

    The sched.* helpers are written for sync sessions (celery tasks): called
    with an AsyncSession, session.get/session.execute return un-awaited
    coroutines and every proxied call 500s (direct worked only by accident
    of an early return). Re-resolve the account in a short-lived sync
    session inside a thread instead.
    """
    import asyncio

    from app.database import SyncSessionLocal
    from app.tasks import sync_helpers as sched

    def _run() -> "tuple[bool, str | None]":
        with SyncSessionLocal() as s:
            a = s.get(Account, acc_id)
            if a is None:
                return False, None
            if not sched.account_reachable(s, a):
                return False, None
            return True, sched.resolve_proxy_url(s, a)

    return await asyncio.to_thread(_run)

def _bio_out(b: BioConfig) -> BioOut:
    import os as _os

    return BioOut(
        id=b.id, account_id=b.account_id, text=b.text, link_url=b.link_url,
        full_name=b.full_name or "", make_private=b.make_private,
        profile_pic_path=b.profile_pic_path,
        has_picture=bool(b.profile_pic_path and _os.path.exists(b.profile_pic_path)),
        last_applied=b.last_applied,
    )


@bio_router.get("", response_model=list[BioOut])
async def list_bios(_: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(BioConfig))).scalars().all()
    return [_bio_out(b) for b in rows]


@bio_router.post("", response_model=BioOut, status_code=201)
async def create_bio(body: BioIn, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    acc = await db.get(Account, body.account_id)
    if not acc:
        raise HTTPException(404, "Account not found")
    b = BioConfig(**body.model_dump())
    db.add(b)
    await db.commit()
    await db.refresh(b)
    return _bio_out(b)


@bio_router.post("/ensure", response_model=BioOut)
async def ensure_bio(body: dict, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    """Get-or-create the single profile config for an account.

    No rotation anymore: one account = one config, every section optional
    and applied independently."""
    raw_aid = body.get("account_id")
    if isinstance(raw_aid, bool) or not isinstance(raw_aid, int):
        raise HTTPException(400, "account_id is required")
    acc = await db.get(Account, raw_aid)
    if not acc:
        raise HTTPException(404, "Account not found")
    row = (await db.execute(select(BioConfig).where(BioConfig.account_id == acc.id))).scalars().first()
    if row is None:
        row = BioConfig(account_id=acc.id, text="")
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return _bio_out(row)


@bio_router.put("/{bid}", response_model=BioOut)
async def update_bio(bid: int, body: BioUpdate, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    b = await db.get(BioConfig, bid)
    if not b:
        raise HTTPException(404, "Bio not found")
    if body.account_id is not None and await db.get(Account, body.account_id) is None:
        raise HTTPException(404, "Account not found")
    # Partial update: only explicitly sent fields are touched, so saving one
    # section never wipes the others.
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(b, k, v)
    await db.commit()
    await db.refresh(b)
    return _bio_out(b)


@bio_router.delete("/{bid}", status_code=204)
async def delete_bio(bid: int, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    b = await db.get(BioConfig, bid)
    if not b:
        raise HTTPException(404, "Bio not found")
    await db.delete(b)
    await db.commit()
    return None


@bio_router.post("/{bid}/apply")
@limiter.limit("10/minute")
async def apply_bio(
    request: Request, bid: int, body: BioApplyIn | None = None,
    _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db),
):
    """Apply profile sections now. Each section is independent: pass
    {"fields": ["bio"]} to touch only the bio, etc. (bio, link, full_name,
    picture, privacy). Omitted/empty = apply every non-empty section."""
    import concurrent.futures

    from app.config import settings
    from app.services.instagram_service import InstagramService
    from app.utils.instagram_helpers import session_path_for

    b = await db.get(BioConfig, bid)
    if not b:
        raise HTTPException(404, "Bio not found")
    acc = await db.get(Account, b.account_id)
    if not acc:
        raise HTTPException(404, "Account not found")
    reachable, purl = await _resolve_route(acc.id)
    if not reachable:
        raise HTTPException(409, "No healthy proxy route for this account right now")
    fields = set((body.fields if body and body.fields else []))
    unknown = fields - {"bio", "link", "full_name", "picture", "privacy"}
    if unknown:
        raise HTTPException(422, f"Unknown section(s): {sorted(unknown)}")
    want_all = not fields
    username, password = acc.username, decrypt_secret(acc.password_enc)
    bio_text: str = b.text if (want_all or "bio" in fields) else ""
    link: str = (b.link_url or "") if (want_all or "link" in fields) else ""
    full_name: str = (b.full_name or "") if (want_all or "full_name" in fields) else ""
    make_private: bool | None = b.make_private if (want_all or "privacy" in fields) else None
    picture: str | None = b.profile_pic_path if (want_all or "picture" in fields) else None
    if not want_all and not (bio_text or link or full_name or picture or make_private is not None):
        raise HTTPException(422, "Selected section is empty — nothing to apply")
    acc_id = acc.id
    from urllib.parse import urlsplit as _urlsplit

    egress = _urlsplit(purl).hostname if purl else "direct"
    svc = InstagramService(proxy_url=purl, session_path=session_path_for(username, settings.MEDIA_ROOT))
    try:
        err = await _await_blocking(
            svc.apply_profile, username, password,
            biography=bio_text, external_url=link, full_name=full_name,
            make_private=make_private, picture_path=picture,
            timeout=180,
        )
    except concurrent.futures.TimeoutError:
        await log_event("ERROR", "account", f"Profile apply timed out for account {acc_id} via {egress}")
        raise HTTPException(504, f"Profile apply timed out after 180s [via {egress}] — the proxy route stalled")
    if err:
        await log_event("ERROR", "account", f"Profile apply failed for account {acc_id} via {egress}: {err}")
        raise HTTPException(502, f"Profile apply failed: {err} [via {egress}]")
    b = await db.get(BioConfig, bid)
    if b:
        b.last_applied = dt.datetime.now(dt.timezone.utc)
        await db.commit()
    await log_event("INFO", "account", f"Profile force-applied to account {acc_id}", {"bio_id": bid})
    return {"ok": True}


PIC_EXT = {".jpg", ".jpeg", ".png", ".webp"}
MAX_PIC_BYTES = 10 * 1024 * 1024


@bio_router.post("/{bid}/picture", response_model=BioOut)
@limiter.limit("10/minute")
async def upload_bio_picture(
    request: Request,
    bid: int,
    file: UploadFile = File(...),
    _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Upload the profile picture for a bio config (validated image, ≤10MB).

    Stored under media/profile_pics/ and published with Apply picture.
    """
    from app.config import settings
    from app.services.video_processor import media_dirs

    b = await db.get(BioConfig, bid)
    if not b:
        raise HTTPException(404, "Bio not found")
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in PIC_EXT:
        raise HTTPException(400, f"Unsupported image type {ext}. Allowed: {sorted(PIC_EXT)}")
    dirs = media_dirs()
    tmp_name = f"bio_{bid}_{uuid.uuid4().hex}{ext}"
    dest = os.path.join(dirs["profile_pics"], tmp_name)
    # Stream to disk with a cap — the old `await file.read()` loaded the
    # whole body into memory with no limit, so a huge upload OOM'd the
    # backend before the 10MB check ever ran.
    from app.utils.uploads import reject_oversize_content_length

    reject_oversize_content_length(request, MAX_PIC_BYTES, "Image")
    size = 0
    try:
        async with aiofiles.open(dest, "wb") as f:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_PIC_BYTES:
                    raise HTTPException(413, "Image exceeds 10MB")
                await f.write(chunk)
    except HTTPException:
        if os.path.exists(dest):
            os.remove(dest)
        raise
    except Exception as exc:
        # Disk error mid-stream etc. — never orphan the partial file.
        if os.path.exists(dest):
            os.remove(dest)
        raise HTTPException(400, f"Upload failed: {exc}")
    finally:
        try:
            await file.close()
        except Exception:
            pass
    if size == 0:
        if os.path.exists(dest):
            os.remove(dest)
        raise HTTPException(400, "Empty file")
    try:
        from PIL import Image as PILImage

        with PILImage.open(dest) as img:
            img.load()
            if img.width < 50 or img.height < 50:
                raise HTTPException(422, "Image too small (min 50×50)")
    except HTTPException:
        os.remove(dest)
        raise
    except Exception:
        os.remove(dest)
        raise HTTPException(422, "File is not a valid image")
    old = b.profile_pic_path
    b.profile_pic_path = dest
    await db.commit()
    await db.refresh(b)
    try:
        if old and old != dest and os.path.exists(old):
            os.remove(old)
    except OSError:
        pass
    await log_event("INFO", "account", f"Profile picture uploaded for bio #{bid}")
    return _bio_out(b)


@bio_router.delete("/{bid}/picture", response_model=BioOut)
async def delete_bio_picture(bid: int, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    b = await db.get(BioConfig, bid)
    if not b:
        raise HTTPException(404, "Bio not found")
    old = b.profile_pic_path
    b.profile_pic_path = None
    await db.commit()
    await db.refresh(b)
    try:
        if old and os.path.exists(old):
            os.remove(old)
    except OSError:
        pass
    return _bio_out(b)


@bio_router.post("/{bid}/picture/remove-live")
@limiter.limit("10/minute")
async def remove_live_picture(
    request: Request, bid: int, _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Delete the CURRENT Instagram profile photo (not the staged file).

    One-way and immediate — there is no undo on IG's side, hence the
    confirm on the frontend."""
    import concurrent.futures

    from app.config import settings
    from app.services.instagram_service import InstagramService
    from app.utils.instagram_helpers import session_path_for

    b = await db.get(BioConfig, bid)
    if not b:
        raise HTTPException(404, "Bio not found")
    acc = await db.get(Account, b.account_id)
    if not acc:
        raise HTTPException(404, "Account not found")
    reachable, purl = await _resolve_route(acc.id)
    if not reachable:
        raise HTTPException(409, "No healthy proxy route for this account right now")
    username, password = acc.username, decrypt_secret(acc.password_enc)
    acc_id = acc.id
    from urllib.parse import urlsplit as _urlsplit

    egress = _urlsplit(purl).hostname if purl else "direct"
    svc = InstagramService(proxy_url=purl, session_path=session_path_for(username, settings.MEDIA_ROOT))
    try:
        err = await _await_blocking(
            svc.remove_live_picture, username, password,
            timeout=180,
        )
    except concurrent.futures.TimeoutError:
        await log_event("ERROR", "account", f"Live picture removal timed out for account {acc_id} via {egress}")
        raise HTTPException(504, f"Live picture removal timed out after 180s [via {egress}] — the proxy route stalled")
    if err:
        await log_event("ERROR", "account", f"Live picture removal failed for account {acc_id} via {egress}: {err}")
        raise HTTPException(502, f"Live picture removal failed: {err} [via {egress}]")
    b = await db.get(BioConfig, bid)
    if b:
        b.last_applied = dt.datetime.now(dt.timezone.utc)
        await db.commit()
    await log_event("INFO", "account", f"Live profile picture removed for account {acc_id}", {"bio_id": bid})
    return {"ok": True}


@bio_router.get("/{bid}/current")
async def bio_current(
    bid: int, _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Read-only IG-side profile snapshot to compare against the config."""
    from app.config import settings
    from app.services.instagram_service import InstagramService
    from app.utils.instagram_helpers import session_path_for

    b = await db.get(BioConfig, bid)
    if not b:
        raise HTTPException(404, "Bio not found")
    acc = await db.get(Account, b.account_id)
    if not acc:
        raise HTTPException(404, "Account not found")
    username = acc.username
    reachable, purl = await _resolve_route(acc.id)
    if not reachable:
        raise HTTPException(409, "No healthy proxy route for this account right now")
    try:
        data = await _await_blocking(
            InstagramService(proxy_url=purl, session_path=session_path_for(username, settings.MEDIA_ROOT)).read_profile,
            username,
            timeout=120,
        )
    except Exception as exc:
        raise HTTPException(502, f"Profile read failed: {exc}")
    return data


@bio_router.get("/{bid}/history")
async def bio_history(bid: int, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    from app.models import SystemLog

    b = await db.get(BioConfig, bid)
    if not b:
        raise HTTPException(404, "Bio not found")
    # Only this config's own events (tagged with bio_id at write time) —
    # previously this leaked the global account log into every bio card.
    rows = (
        await db.execute(
            select(SystemLog).where(SystemLog.category == "account").order_by(SystemLog.timestamp.desc()).limit(200)
        )
    ).scalars().all()
    mine = [r for r in rows if (r.details or {}).get("bio_id") == bid][:50]
    return [{"message": r.message, "timestamp": r.timestamp} for r in mine]


# ---- Proxies ----

def _proxy_out(p: Proxy) -> ProxyOut:
    return ProxyOut(
        id=p.id, url=p.url, protocol=p.protocol.value, username=p.username, country=p.country,
        is_healthy=p.is_healthy, last_checked=p.last_checked, fail_count=p.fail_count,
        latency_ms=p.latency_ms, last_error=p.last_error, source=p.source or "manual",
        is_active=p.is_active, created_at=p.created_at,
    )


@proxy_router.get("/pipeline")
async def proxy_pipeline(_: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    """Granular status of the proxy pipeline: pool snapshot, checker config,
    pool policy, last cycle results, and recent proxy activity."""
    from sqlalchemy import desc

    from app.models import Setting, SystemLog
    from app.tasks.sync_helpers import (
        MAX_PROXY_FAILS,
        POOL_MAX_AUTO,
        PROXY_CHECK_BATCH,
        PROXY_CHECK_THREADS,
        PROXY_FAIL_COOLDOWN_HOURS,
        PROXY_VERIFY_LIMIT,
        PROXY_VERIFY_THREADS,
        SWEEP_TCP_TIMEOUT,
    )

    rows = (await db.execute(select(Proxy))).scalars().all()
    settings = {r.key: (r.value or "") for r in (await db.execute(select(Setting))).scalars().all()}

    counts = {
        "total": len(rows),
        "healthy": sum(1 for p in rows if p.is_healthy and p.is_active),
        "dead": sum(1 for p in rows if not p.is_healthy and p.is_active),
        "disabled": sum(1 for p in rows if not p.is_active),
        "never_checked": sum(1 for p in rows if p.last_checked is None),
        "manual": sum(1 for p in rows if (p.source or "manual") == "manual"),
        "auto": sum(1 for p in rows if (p.source or "manual") != "manual"),
    }
    lats = [p.latency_ms for p in rows if p.latency_ms is not None]
    checked = [p.last_checked for p in rows if p.last_checked is not None]

    async def last_log(category: str, like: str):
        q = (
            select(SystemLog)
            .where(SystemLog.category == category, SystemLog.message.like(like))
            .order_by(desc(SystemLog.timestamp))
            .limit(1)
        )
        r = (await db.execute(q)).scalars().first()
        return {"at": r.timestamp, "message": r.message} if r else None

    recent = (
        await db.execute(
            select(SystemLog)
            .where(SystemLog.category == "proxy")
            .order_by(desc(SystemLog.timestamp))
            .limit(8)
        )
    ).scalars().all()

    try:
        purge_days = int(settings.get("pool_purge_after_days", "7"))
    except ValueError:
        purge_days = 7
    try:
        stillborn_hours = int(settings.get("pool_stillborn_hours", "48"))
    except ValueError:
        stillborn_hours = 48
    return {
        "counts": counts,
        "latency": {
            "avg_ms": round(sum(lats) / len(lats)) if lats else None,
            "max_ms": max(lats) if lats else None,
            "measured": len(lats),
        },
        "oldest_checked_at": min(checked) if checked else None,
        "checker": {
            "cadence": "every 30 min",
            "batch": PROXY_CHECK_BATCH,
            "threads": PROXY_CHECK_THREADS,
            "verify_limit": PROXY_VERIFY_LIMIT,
            "verify_threads": PROXY_VERIFY_THREADS,
            "sweep_timeout_s": SWEEP_TCP_TIMEOUT,
            "max_fails": MAX_PROXY_FAILS,
            "fail_cooldown_h": PROXY_FAIL_COOLDOWN_HOURS,
        },
        "pool": {
            "refresh_cadence": "every 3 h",
            "purge_after_days": purge_days,
            "stillborn_hours": stillborn_hours,
            "max_auto": POOL_MAX_AUTO,
            "country": settings.get("pool_country", ""),
            "require_country": settings.get("pool_require_country", "false").lower() == "true",
        },
        "last_runs": {
            "health_check": await last_log("system", "Proxy health check:%"),
            "pool_refresh": await last_log("proxy", "Pool refresh:%"),
            "purge": await last_log("proxy", "%purg%"),
            "auto_disabled": await last_log("proxy", "%auto-disabled%"),
        },
        "recent": [
            {"at": r.timestamp, "level": r.level.value, "message": r.message} for r in recent
        ],
    }


@proxy_router.get("", response_model=list[ProxyOut])
async def list_proxies(_: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Proxy).order_by(Proxy.id))).scalars().all()
    return [_proxy_out(p) for p in rows]


@proxy_router.post("", response_model=ProxyOut, status_code=201)
async def create_proxy(body: ProxyCreate, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    try:
        proto = ProxyProtocol(body.protocol)
    except ValueError:
        raise HTTPException(400, "Invalid protocol")
    p = Proxy(url=body.url, protocol=proto, username=body.username,
              password_enc=encrypt_secret(body.password) if body.password else None, country=body.country,
              source="manual")
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return _proxy_out(p)


@proxy_router.post("/import")
@limiter.limit("10/minute")
async def import_proxies(
    request: Request,
    file: UploadFile = File(...),
    # NOTE: plain `str = ...` params are query-only in FastAPI — multipart
    # form fields REQUIRE Form() or they are silently dropped (proven by test).
    default_protocol: str = Form("http"),
    default_country: str = Form(""),
    _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Bulk-import proxies from a text file (one per line, any shape).

    Accepted per line: host:port, scheme://host:port, user:pass@host:port,
    scheme://user:pass@host:port, host:port:user:pass — each optionally
    suffixed with " |CC" / " #CC" country tag. Lines starting with # and
    blank lines are skipped. No-auth (IP-whitelisted) lines work as-is.
    Existing host:port entries are skipped, never duplicated.
    """
    from app.services.proxy_service import MAX_IMPORT_LINES, parse_proxy_line, proxy_fingerprint

    if default_protocol not in ("http", "https", "socks4", "socks5"):
        raise HTTPException(400, "Invalid default_protocol")
    default_country = (default_country or "").strip().upper()[:2]
    raw = await file.read()
    try:
        await file.close()
    except Exception:
        pass
    if not raw:
        raise HTTPException(400, "Empty file")
    if len(raw) > 1024 * 1024:
        raise HTTPException(413, "List exceeds 1MB")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(400, "File must be UTF-8 text")
    lines = text.splitlines()
    if len(lines) > MAX_IMPORT_LINES:
        raise HTTPException(413, f"Too many lines (max {MAX_IMPORT_LINES})")

    have = set()
    for p in (await db.execute(select(Proxy))).scalars().all():
        known, _err = parse_proxy_line(p.url, "http")
        if known:
            have.add(proxy_fingerprint(known["scheme"], known["host"], known["port"]))

    added, skipped, errors = 0, [], []
    for i, line in enumerate(lines, 1):
        spec, err = parse_proxy_line(line, default_protocol)
        if spec is None:
            if err not in ("blank/comment",):
                errors.append({"line": i, "text": line.strip()[:80], "reason": err})
            continue
        key = proxy_fingerprint(spec["scheme"], spec["host"], spec["port"])
        if key in have:
            skipped.append({"line": i, "text": line.strip()[:80], "reason": "duplicate"})
            continue
        try:
            proto = ProxyProtocol(spec["scheme"] if spec["scheme"] != "https" else "http")
        except ValueError:
            errors.append({"line": i, "text": line.strip()[:80], "reason": "bad scheme"})
            continue
        country = spec["country"] or (default_country or None)
        db.add(Proxy(
            url=f"{spec['scheme']}://{spec['host']}:{spec['port']}",
            protocol=proto,
            username=spec["username"] or None,
            password_enc=encrypt_secret(spec["password"]) if spec["password"] else None,
            country=country,
            source="manual",
        ))
        have.add(key)
        added += 1
    await db.commit()
    await log_event("INFO", "proxy", f"Bulk import: {added} added, {len(skipped)} duplicates, {len(errors)} bad lines")
    return {
        "added": added,
        "duplicates_skipped": len(skipped),
        "errors": (skipped + errors)[:100],
    }


@proxy_router.put("/{pid}", response_model=ProxyOut)
async def update_proxy(pid: int, body: ProxyUpdate, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    p = await db.get(Proxy, pid)
    if not p:
        raise HTTPException(404, "Proxy not found")
    if body.url is not None:
        p.url = body.url
    if body.protocol is not None:
        try:
            p.protocol = ProxyProtocol(body.protocol)
        except ValueError:
            raise HTTPException(400, "Invalid protocol")
    if body.username is not None:
        p.username = body.username
    if body.password is not None:
        p.password_enc = encrypt_secret(body.password)
    if body.country is not None:
        p.country = body.country
    if body.is_active is not None:
        p.is_active = body.is_active
    await db.commit()
    await db.refresh(p)
    return _proxy_out(p)


@proxy_router.delete("/{pid}", status_code=204)
async def delete_proxy(pid: int, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    p = await db.get(Proxy, pid)
    if not p:
        raise HTTPException(404, "Proxy not found")
    # Detach accounts first — with FK enforcement on, deleting a referenced
    # proxy would 500 instead (same as the purge path below).
    from sqlalchemy import update as _update

    await db.execute(_update(Account).where(Account.proxy_id == pid).values(proxy_id=None))
    await db.delete(p)
    await db.commit()
    return None


@proxy_router.post("/{pid}/test")
async def test_proxy(pid: int, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    import datetime as dt2

    from app.services.proxy_service import check_proxy

    p = await db.get(Proxy, pid)
    if not p:
        raise HTTPException(404, "Proxy not found")
    ok, latency = await check_proxy(p)
    p.is_healthy = ok
    p.latency_ms = latency
    p.last_checked = dt2.datetime.now(dt2.timezone.utc)
    p.fail_count = 0 if ok else (p.fail_count or 0) + 1
    await db.commit()
    import asyncio

    from app.tasks.sync_helpers import publish_sync

    await asyncio.to_thread(publish_sync, "proxy_pool_update", {"tested": p.id})
    return {"healthy": ok, "latency_ms": latency}


@proxy_router.post("/check-all")
@limiter.limit("5/minute")
async def check_all(request: Request, _: str = Depends(get_current_admin)):
    from app.tasks.periodic_tasks import check_all_proxies

    check_all_proxies.delay()
    return {"queued": True}


def _source_out(x: ProxySource) -> ProxySourceOut:
    return ProxySourceOut(
        id=x.id, name=x.name, url=x.url, default_protocol=x.default_protocol,
        default_country=x.default_country or "", is_active=x.is_active,
        last_fetch_at=x.last_fetch_at, last_added=x.last_added, last_total=x.last_total,
    )


@proxy_router.get("/sources", response_model=list[ProxySourceOut])
async def list_sources(_: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(ProxySource).order_by(ProxySource.name))).scalars().all()
    return [_source_out(x) for x in rows]


def _checked_source_url(url: str) -> None:
    """Fail fast on unsafe proxy-list URLs (SSRF, m1).

    The periodic fetch re-validates every hop anyway; rejecting here gives
    the admin an immediate 400 instead of a source that errors forever.
    """
    from app.utils.ssrf import validate_fetch_url

    try:
        validate_fetch_url(url)
    except ValueError as exc:
        raise HTTPException(400, f"Source URL rejected: {exc}")


@proxy_router.post("/sources", response_model=ProxySourceOut, status_code=201)
async def create_source(body: ProxySourceIn, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    if body.default_protocol not in ("http", "https", "socks4", "socks5"):
        raise HTTPException(400, "Invalid default_protocol")
    if body.url and not body.url.lower().startswith(("http://", "https://")):
        raise HTTPException(400, "Source URL must be http(s)")
    _checked_source_url(body.url)
    exists = (await db.execute(select(ProxySource).where(ProxySource.name == body.name))).scalar_one_or_none()
    if exists:
        raise HTTPException(409, "Source already exists")
    x = ProxySource(
        name=body.name, url=body.url, default_protocol=body.default_protocol,
        default_country=(body.default_country or "").upper(),
    )
    db.add(x)
    await db.commit()
    await db.refresh(x)
    return _source_out(x)


@proxy_router.put("/sources/{sid}", response_model=ProxySourceOut)
async def update_source(sid: int, body: ProxySourceUpdate, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    x = await db.get(ProxySource, sid)
    if not x:
        raise HTTPException(404, "Source not found")
    data = body.model_dump(exclude_unset=True)
    if "default_protocol" in data and data["default_protocol"] not in ("http", "https", "socks4", "socks5"):
        raise HTTPException(400, "Invalid default_protocol")
    if "url" in data:
        _checked_source_url(data["url"])
    if "name" in data:
        clash = (await db.execute(
            select(ProxySource).where(ProxySource.name == data["name"], ProxySource.id != sid)
        )).scalar_one_or_none()
        if clash:
            raise HTTPException(409, "Another source already uses that name")
    # Renaming a source orphans its rows' origin label — auto rows stay
    # protected by the purge rule (source != manual), so this is display-only.
    for k, v in data.items():
        if k == "default_country":
            v = (v or "").upper()
        setattr(x, k, v)
    await db.commit()
    await db.refresh(x)
    return _source_out(x)


@proxy_router.delete("/sources/{sid}", status_code=204)
async def delete_source(sid: int, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    x = await db.get(ProxySource, sid)
    if not x:
        raise HTTPException(404, "Source not found")
    await db.delete(x)
    await db.commit()
    return None


@proxy_router.post("/pool/refresh")
@limiter.limit("5/minute")
async def refresh_pool_now(request: Request, _: str = Depends(get_current_admin)):
    from app.tasks.periodic_tasks import refresh_proxy_pool

    refresh_proxy_pool.delay()
    return {"queued": True}


@proxy_router.post("/pool/purge")
@limiter.limit("5/minute")
async def purge_pool_now(request: Request, _: str = Depends(get_current_admin)):
    from app.database import SyncSessionLocal
    from app.tasks.sync_helpers import get_setting, publish_sync, purge_stale_auto_proxies

    def _run() -> int:
        with SyncSessionLocal() as s:
            try:
                days = int(get_setting(s, "pool_purge_after_days", "7"))
            except ValueError:
                days = 7
            try:
                stillborn = int(get_setting(s, "pool_stillborn_hours", "48"))
            except ValueError:
                stillborn = 48
            return purge_stale_auto_proxies(
                s, max_age_days=min(max(days, 1), 30), stillborn_hours=max(stillborn, 1))

    # The purge helper is sync (same session style as the celery tasks).
    n = await _await_blocking(_run, timeout=120)
    await log_event("INFO", "proxy", f"Manual pool purge: {n} stale auto rows removed")
    publish_sync("proxy_pool_update", {"purged": n})
    return {"purged": n}


@proxy_router.post("/reset")
@limiter.limit("5/minute")
async def reset_proxies_now(request: Request, _: str = Depends(get_current_admin)):
    """Zero the whole proxy section: delete every auto-fetched row, reset
    manual rows to a fresh NEW state, and unlink all accounts from proxies
    so nothing routes through an unverified proxy afterwards. Sources and
    pool settings are left untouched."""
    from sqlalchemy import update

    from app.database import SyncSessionLocal
    from app.tasks.sync_helpers import publish_sync

    def _run() -> dict:
        with SyncSessionLocal() as s:
            # Unlink first so no account dangles on (or silently keeps
            # routing through) a deleted/unverified proxy.
            unlinked = (
                s.execute(
                    update(Account).where(Account.proxy_id.isnot(None)).values(proxy_id=None)
                ).rowcount
            )
            auto_rows = (
                s.execute(
                    select(Proxy).where(Proxy.source.isnot(None), Proxy.source != "manual")
                )
                .scalars()
                .all()
            )
            for p in auto_rows:
                s.delete(p)
            manuals = (
                s.execute(
                    select(Proxy).where((Proxy.source.is_(None)) | (Proxy.source == "manual"))
                )
                .scalars()
                .all()
            )
            for p in manuals:
                p.is_healthy = False
                p.fail_count = 0
                p.latency_ms = None
                p.last_checked = None
                p.last_error = None
            s.commit()
            return {
                "deleted_auto": len(auto_rows),
                "reset_manual": len(manuals),
                "unlinked_accounts": unlinked,
            }

    res = await _await_blocking(_run, timeout=120)
    await log_event(
        "INFO", "proxy",
        f"Manual proxy reset: {res['deleted_auto']} auto deleted, "
        f"{res['reset_manual']} manual zeroed, {res['unlinked_accounts']} accounts unlinked",
    )
    publish_sync("proxy_pool_update", res)
    return res


# ---- Effects ----

@effect_router.get("", response_model=list[EffectOut])
async def list_effects(_: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    from sqlalchemy import func

    from app.models import Post, PostStatus, Video

    rows = (await db.execute(select(EffectPreset).order_by(EffectPreset.name))).scalars().all()
    # Top up any missing built-ins (covers both fresh DBs and servers seeded
    # with an older, smaller set) so the admin can just pick — no manual
    # FFmpeg entry needed. Never touches rows the admin added/edited.
    from app.services.default_effects import missing_presets

    missing = missing_presets([e.name for e in rows])
    if missing:
        for preset in missing:
            db.add(EffectPreset(**preset))
        await db.commit()
        rows = (await db.execute(select(EffectPreset).order_by(EffectPreset.name))).scalars().all()
    # Engagement is computed live (one grouped query), not stored — the old
    # column was never written, so every row showed NULL.
    eng = {
        (r[0] or ""): r[1]
        for r in (
            await db.execute(
                select(Video.effect_preset, func.avg(Post.engagement_rate))
                .join(Video, Video.id == Post.video_id)
                .where(Post.status == PostStatus.posted, Video.effect_preset.is_not(None))
                .group_by(Video.effect_preset)
            )
        ).all()
    }
    return [EffectOut(id=e.id, name=e.name, description=e.description, ffmpeg_filter=e.ffmpeg_filter, is_active=e.is_active, use_count=e.use_count,
                      avg_engagement=round(float(eng.get(e.name) or 0), 2)) for e in rows]


@effect_router.post("", response_model=EffectOut, status_code=201)
async def create_effect(body: EffectIn, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    exists = (await db.execute(select(EffectPreset).where(EffectPreset.name == body.name))).scalar_one_or_none()
    if exists:
        raise HTTPException(409, "Effect preset already exists")
    e = EffectPreset(**body.model_dump())
    db.add(e)
    await db.commit()
    await db.refresh(e)
    return EffectOut(id=e.id, name=e.name, description=e.description, ffmpeg_filter=e.ffmpeg_filter, is_active=e.is_active, use_count=e.use_count, avg_engagement=None)


@effect_router.put("/{eid}", response_model=EffectOut)
async def update_effect(eid: int, body: EffectUpdate, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    e = await db.get(EffectPreset, eid)
    if not e:
        raise HTTPException(404, "Effect not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(e, k, v)
    await db.commit()
    await db.refresh(e)
    return EffectOut(id=e.id, name=e.name, description=e.description, ffmpeg_filter=e.ffmpeg_filter, is_active=e.is_active, use_count=e.use_count, avg_engagement=None)


@effect_router.delete("/{eid}", status_code=204)
async def delete_effect(eid: int, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    e = await db.get(EffectPreset, eid)
    if not e:
        raise HTTPException(404, "Effect not found")
    await db.delete(e)
    await db.commit()
    return None


# ---- Audio tracks (trending sounds mixed in at processing time) ----

@audio_router.get("", response_model=list[AudioOut])
async def list_audio(_: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(AudioTrack).order_by(AudioTrack.name))).scalars().all()
    return [_audio_out(t) for t in rows]


@audio_router.post("/upload", response_model=AudioOut, status_code=201)
@limiter.limit("10/minute")
async def upload_audio(
    request: Request,
    file: UploadFile = File(...),
    name: str = Form(""),
    description: str = Form(""),
    music_volume: float = Form(0.4),
    duck_original: bool = Form(False),
    _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Upload a trending sound (MP3/WAV/M4A/…). Validated with ffprobe, then
    eligible for automatic weighted selection at processing time."""
    import asyncio

    from app.config import settings
    from app.services.video_processor import media_dirs
    from app.utils import ffmpeg as ff

    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in AUDIO_EXT:
        raise HTTPException(400, f"Unsupported audio type {ext}. Allowed: {sorted(AUDIO_EXT)}")
    if not (0.0 <= music_volume <= 2.0):
        raise HTTPException(400, "music_volume must be between 0.0 and 2.0")
    track_name = (name or os.path.splitext(file.filename or "track")[0]).strip()[:128]
    if not track_name:
        raise HTTPException(400, "Track name is required")
    exists = (await db.execute(select(AudioTrack).where(AudioTrack.name == track_name))).scalar_one_or_none()
    if exists:
        raise HTTPException(409, "Audio track already exists")
    dirs = media_dirs()
    tmp_name = f"{uuid.uuid4().hex}{ext}"
    raw_path = os.path.join(dirs["audio"], tmp_name)
    from app.utils.uploads import reject_oversize_content_length

    reject_oversize_content_length(request, MAX_AUDIO_BYTES, "Audio")
    size = 0
    try:
        # Single streaming handle (same pattern as video upload) — one
        # open/write/close cycle instead of one per chunk.
        async with aiofiles.open(raw_path, "wb") as f:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_AUDIO_BYTES:
                    raise HTTPException(413, "Audio exceeds 50MB")
                await f.write(chunk)
    except HTTPException:
        if os.path.exists(raw_path):
            os.remove(raw_path)
        raise
    except Exception:
        # Disk error mid-stream etc. — never orphan the partial file.
        if os.path.exists(raw_path):
            os.remove(raw_path)
        raise
    finally:
        try:
            await file.close()
        except Exception:
            pass
    if size == 0:
        if os.path.exists(raw_path):
            os.remove(raw_path)
        raise HTTPException(400, "Empty file")
    try:
        probe = await asyncio.wait_for(asyncio.to_thread(ff.probe_sync, raw_path), timeout=60)
    except Exception:
        os.remove(raw_path)
        raise HTTPException(422, "File is not valid audio (ffprobe validation failed)")
    duration = probe.get("duration") or 0
    if duration < 1:
        os.remove(raw_path)
        raise HTTPException(422, "Audio is too short (< 1s)")
    t = AudioTrack(
        name=track_name, description=description[:2000], file_path=raw_path,
        duration=duration, music_volume=music_volume, duck_original=duck_original,
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)
    await log_event("INFO", "audio", f"Audio track '{track_name}' uploaded ({duration:.1f}s)")
    return _audio_out(t)


@audio_router.put("/{tid}", response_model=AudioOut)
async def update_audio(tid: int, body: AudioUpdate, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    t = await db.get(AudioTrack, tid)
    if not t:
        raise HTTPException(404, "Audio track not found")
    data = body.model_dump(exclude_unset=True)
    if "name" in data:
        clash = (await db.execute(
            select(AudioTrack).where(AudioTrack.name == data["name"], AudioTrack.id != tid)
        )).scalar_one_or_none()
        if clash:
            raise HTTPException(409, "Another track already uses that name")
    for k, v in data.items():
        setattr(t, k, v)
    await db.commit()
    await db.refresh(t)
    return _audio_out(t)


@audio_router.delete("/{tid}", status_code=204)
async def delete_audio(tid: int, _: str = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    t = await db.get(AudioTrack, tid)
    if not t:
        raise HTTPException(404, "Audio track not found")
    path = t.file_path
    await db.delete(t)
    await db.commit()
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
    await log_event("INFO", "audio", f"Audio track '{t.name}' deleted")
    return None
