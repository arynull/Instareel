"""IG accounts CRUD + session management."""
import asyncio
import datetime as dt
import json
import os
import threading
import time

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin, limiter
from app.core.security import decrypt_secret, encrypt_secret
from app.models import Account, AccountStatus, Post, PostStatus, Proxy
from app.schemas.account import AccountCreate, AccountOut, AccountUpdate
from app.services.log_service import log_event

router = APIRouter()

MAX_SESSION_BYTES = 5 * 1024 * 1024


def _has_session_file(username: str, session_file_path: str | None) -> bool:
    from app.config import settings
    from app.utils.instagram_helpers import session_path_for

    path = session_file_path or session_path_for(username, settings.MEDIA_ROOT)
    return bool(path) and os.path.exists(path)


def _out(a: Account) -> AccountOut:
    return AccountOut(
        id=a.id, username=a.username, ig_user_id=a.ig_user_id, proxy_id=a.proxy_id, status=a.status.value,
        last_login=a.last_login, last_post=a.last_post, posts_today=a.posts_today,
        max_daily_posts=a.max_daily_posts, cooldown_until=a.cooldown_until,
        total_posts=a.total_posts, total_views=a.total_views, total_likes=a.total_likes,
        notes=a.notes, created_at=a.created_at, updated_at=a.updated_at,
        has_session=_has_session_file(a.username, a.session_file_path),
    )


def _move_session_file(old_path: str | None, new_path: str) -> bool:
    """Move an existing session file to its new canonical path. Returns True if moved."""
    if old_path and old_path != new_path and os.path.exists(old_path):
        os.replace(old_path, new_path)
        return True
    return False


async def _rename_account(db: AsyncSession, acc: Account, new_username: str) -> str:
    """Rename in place (no commit): row + session file. Returns the old username.

    Everything else (posts, stats, rules, proxy link) keys off the integer id,
    so history survives a username change.
    """
    import re

    from app.config import settings
    from app.utils.instagram_helpers import session_path_for

    new_username = (new_username or "").strip().lstrip("@")
    if not re.match(r"^[A-Za-z0-9._]{1,30}$", new_username):
        raise HTTPException(400, "Invalid Instagram username (letters, numbers, . and _ only, max 30)")
    if new_username.lower() != acc.username.lower():
        clash = (await db.execute(
            select(Account).where(func.lower(Account.username) == new_username.lower(), Account.id != acc.id)
        )).scalar_one_or_none()
        if clash:
            raise HTTPException(409, f"Another account already uses @{new_username}")
    old_username = acc.username
    acc.username = new_username
    old_path = acc.session_file_path or session_path_for(old_username, settings.MEDIA_ROOT)
    new_path = session_path_for(new_username, settings.MEDIA_ROOT)
    if _move_session_file(old_path, new_path):
        acc.session_file_path = new_path
    elif not os.path.exists(new_path):
        acc.session_file_path = None
    else:
        acc.session_file_path = new_path
    return old_username


@router.get("", response_model=list[AccountOut])
async def list_accounts(_: str = Depends(get_current_admin), db: AsyncSession = Depends(__import__("app.api.deps", fromlist=["get_db"]).get_db)):
    rows = (await db.execute(select(Account).order_by(Account.username))).scalars().all()
    return [_out(a) for a in rows]


@router.post("", response_model=AccountOut, status_code=201)
async def create_account(body: AccountCreate, _: str = Depends(get_current_admin), db: AsyncSession = Depends(__import__("app.api.deps", fromlist=["get_db"]).get_db)):
    from app.config import settings

    exists = (await db.execute(select(Account).where(Account.username == body.username))).scalar_one_or_none()
    if exists:
        raise HTTPException(409, "Account already exists")
    if body.proxy_id is not None and await db.get(Proxy, body.proxy_id) is None:
        raise HTTPException(404, "Proxy not found")
    acc = Account(
        username=body.username,
        password_enc=encrypt_secret(body.password),
        proxy_id=body.proxy_id,
        max_daily_posts=body.max_daily_posts or settings.IG_DEFAULT_MAX_DAILY_POSTS,
        notes=body.notes,
    )
    db.add(acc)
    await db.commit()
    await db.refresh(acc)
    await log_event("INFO", "account", f"Account @{body.username} added")
    return _out(acc)


@router.get("/{account_id}", response_model=AccountOut)
async def get_account(account_id: int, _: str = Depends(get_current_admin), db: AsyncSession = Depends(__import__("app.api.deps", fromlist=["get_db"]).get_db)):
    acc = await db.get(Account, account_id)
    if not acc:
        raise HTTPException(404, "Account not found")
    return _out(acc)


@router.put("/{account_id}", response_model=AccountOut)
async def update_account(account_id: int, body: AccountUpdate, _: str = Depends(get_current_admin), db: AsyncSession = Depends(__import__("app.api.deps", fromlist=["get_db"]).get_db)):
    acc = await db.get(Account, account_id)
    if not acc:
        raise HTTPException(404, "Account not found")
    # Sentinel: {"proxy_id": "none"} unlinks the proxy. Plain null = leave unchanged.
    if body.proxy_id is not None:
        if body.proxy_id == "none":
            acc.proxy_id = None
        else:
            try:
                pid = int(body.proxy_id)
            except (TypeError, ValueError):
                raise HTTPException(400, "Invalid proxy_id")
            if await db.get(Proxy, pid) is None:
                raise HTTPException(404, "Proxy not found")
            acc.proxy_id = pid
    if body.max_daily_posts is not None:
        acc.max_daily_posts = body.max_daily_posts
    if body.notes is not None:
        acc.notes = body.notes
    if body.status is not None:
        try:
            acc.status = AccountStatus(body.status)
        except ValueError:
            raise HTTPException(400, "Invalid status")
    await db.commit()
    await db.refresh(acc)
    return _out(acc)


@router.post("/{account_id}/rename", response_model=AccountOut)
async def rename_account(
    account_id: int, new_username: str = Query(min_length=1, max_length=32),
    _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(__import__("app.api.deps", fromlist=["get_db"]).get_db),
):
    """Rename after a username change on Instagram — history, stats, rules and
    the session file follow the account instead of being deleted with it."""
    acc = await db.get(Account, account_id)
    if not acc:
        raise HTTPException(404, "Account not found")
    old = await _rename_account(db, acc, new_username)
    await db.commit()
    await db.refresh(acc)
    await log_event("INFO", "account", f"Account renamed @{old} → @{acc.username}")
    return _out(acc)


@router.delete("/{account_id}", status_code=204)
async def delete_account(account_id: int, _: str = Depends(get_current_admin), db: AsyncSession = Depends(__import__("app.api.deps", fromlist=["get_db"]).get_db)):
    acc = await db.get(Account, account_id)
    if not acc:
        raise HTTPException(404, "Account not found")
    from app.config import settings
    from app.utils.instagram_helpers import session_path_for

    username = acc.username
    spath = session_path_for(username, settings.MEDIA_ROOT)
    await db.delete(acc)
    await db.commit()
    try:
        if os.path.exists(spath):
            os.remove(spath)
    except OSError:
        pass
    await log_event("INFO", "account", f"Account @{username} removed")
    return None


def _blocking_login(username: str, password: str, proxy_url: str | None, session_path: str):
    from app.services.instagram_service import InstagramService

    return InstagramService(proxy_url=proxy_url, session_path=session_path).login(username, password)


#: Background Instagram login tasks, keyed by account id. A login through a
#: slow proxy can take minutes; it must never block the event loop (a single
#: uvicorn worker serves the whole dashboard — one blocking .result() froze
#: every concurrent request and the UI showed "Load failed" on unrelated
#: pages). The dashboard polls login-status for progress instead.
_login_tasks: dict[int, dict] = {}
_login_tasks_lock = threading.Lock()
#: A "running" entry older than this is treated as dead (e.g. the server was
#: restarted mid-login): the status endpoint heals it instead of wedging the
#: account behind a permanent 409.
LOGIN_TASK_STALE_S = 600
#: Hard cap for one login attempt (mirrors the old synchronous endpoint).
LOGIN_TASK_TIMEOUT_S = 180


def _login_task_get(account_id: int) -> dict | None:
    with _login_tasks_lock:
        task = _login_tasks.get(account_id)
        if (
            task
            and task["status"] == "running"
            and time.monotonic() - task["started_at"] > LOGIN_TASK_STALE_S
        ):
            task = {
                **task,
                "status": "failed",
                "detail": "Login interrupted (server restarted?) — try again.",
                "finished_at": time.time(),
            }
            _login_tasks[account_id] = task
        return dict(task) if task else None


def _login_task_claim(account_id: int) -> bool:
    """Reserve the login slot; False when a live login is already running."""
    with _login_tasks_lock:
        task = _login_tasks.get(account_id)
        if (
            task
            and task["status"] == "running"
            and time.monotonic() - task["started_at"] <= LOGIN_TASK_STALE_S
        ):
            return False
        _login_tasks[account_id] = {
            "status": "running",
            "detail": "Login in progress…",
            "started_at": time.monotonic(),
            "finished_at": None,
        }
        return True


def _login_task_finish(account_id: int, ok: bool, detail: str) -> None:
    with _login_tasks_lock:
        prev = _login_tasks.get(account_id) or {}
        _login_tasks[account_id] = {
            "status": "ok" if ok else "failed",
            "detail": detail,
            "started_at": prev.get("started_at", time.monotonic()),
            "finished_at": time.time(),
        }


def _apply_login_result(account_id: int, username: str, session_path: str, ok: bool, detail: str) -> None:
    """Persist the login outcome (runs in the worker thread — sync DB only)."""
    from app.database import SyncSessionLocal
    from app.models import Account, AccountStatus
    from app.tasks.sync_helpers import log_event_sync, publish_sync

    new_status: str | None = None
    try:
        with SyncSessionLocal() as s:
            acc = s.get(Account, account_id)
            if acc is not None:
                if ok:
                    acc.status = AccountStatus.active
                    acc.last_login = dt.datetime.now(dt.timezone.utc)
                    acc.session_file_path = session_path
                elif str(detail).startswith("challenge"):
                    acc.status = AccountStatus.challenge_required
                s.commit()
                new_status = acc.status.value
    except Exception as exc:
        # Bookkeeping must never mask the login outcome itself.
        log_event_sync("ERROR", "account", f"Login bookkeeping failed for @{username}: {exc}")
    try:
        log_event_sync("INFO" if ok else "WARNING", "account", f"Manual login @{username}: {detail}")
        if new_status is not None:
            publish_sync("account_status_change", {"account_id": account_id, "status": new_status})
    except Exception:
        pass


def _run_login_task(
    account_id: int, username: str, password: str, proxy_url: str | None, session_path: str
) -> None:
    """Blocking Instagram login in a daemon thread; never touches the event loop."""
    import concurrent.futures
    import logging

    log = logging.getLogger("igfunnel.accounts")
    ok, detail = False, "login crashed before starting"
    try:
        # Nested executor keeps the 180s backstop without blocking the loop:
        # on timeout the attempt is marked failed instead of hanging forever.
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            try:
                ok, detail = pool.submit(
                    _blocking_login, username, password, proxy_url, session_path
                ).result(timeout=LOGIN_TASK_TIMEOUT_S)
            except concurrent.futures.TimeoutError:
                ok, detail = False, (
                    f"Login timed out after {LOGIN_TASK_TIMEOUT_S}s — the route to Instagram stalled"
                )
        _apply_login_result(account_id, username, session_path, ok, detail)
    except Exception as exc:  # never leave the task wedged on "running"
        log.exception("Login worker crashed for account %s", account_id)
        ok, detail = False, f"login crashed: {exc}"
    finally:
        _login_task_finish(account_id, ok, detail)


@router.post("/{account_id}/login", status_code=202)
@limiter.limit("5/minute")
async def force_login(
    request: Request, account_id: int, _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(__import__("app.api.deps", fromlist=["get_db"]).get_db),
):
    """Start a background Instagram login; poll login-status for the outcome.

    Returns 202 immediately — the old version blocked the event loop for up
    to 180s, freezing the whole backend (single uvicorn worker) so every
    concurrent dashboard request failed.
    """
    from app.config import settings
    from app.services.proxy_service import proxy_url_for
    from app.utils.instagram_helpers import session_path_for

    acc = await db.get(Account, account_id)
    if not acc:
        raise HTTPException(404, "Account not found")
    if not _login_task_claim(account_id):
        raise HTTPException(
            409, "A login is already in progress for this account — poll login-status."
        )
    proxy = await db.get(Proxy, acc.proxy_id) if acc.proxy_id else None
    username, password = acc.username, decrypt_secret(acc.password_enc)
    purl = proxy_url_for(proxy) if proxy else None
    spath = session_path_for(username, settings.MEDIA_ROOT)
    thread = threading.Thread(
        target=_run_login_task,
        args=(account_id, username, password, purl, spath),
        name=f"ig-login-{account_id}",
        daemon=True,
    )
    thread.start()
    return {"ok": True, "status": "running", "detail": "Login started in the background."}


@router.get("/{account_id}/login-status")
async def login_status(
    account_id: int, _: str = Depends(get_current_admin),
):
    """Poll the background login task: running | ok | failed (+ detail)."""
    task = _login_task_get(account_id)
    if task is None:
        raise HTTPException(404, "No login task for this account")
    return {"account_id": account_id, **task}


@router.post("/{account_id}/test-session")
@limiter.limit("20/minute")
async def test_session(
    request: Request, account_id: int, _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(__import__("app.api.deps", fromlist=["get_db"]).get_db),
):
    import concurrent.futures

    from app.config import settings
    from app.services.instagram_service import InstagramService
    from app.services.proxy_service import proxy_url_for
    from app.utils.instagram_helpers import session_path_for

    acc = await db.get(Account, account_id)
    if not acc:
        raise HTTPException(404, "Account not found")
    proxy = await db.get(Proxy, acc.proxy_id) if acc.proxy_id else None
    username = acc.username
    purl = proxy_url_for(proxy) if proxy else None
    spath = acc.session_file_path or session_path_for(username, settings.MEDIA_ROOT)
    # Await the check instead of a blocking .result(): with a single uvicorn
    # worker a blocking wait froze the whole backend for every concurrent
    # request (same bug as the old /login endpoint). The pool is shut down
    # without waiting so a timed-out check never stalls the loop — its
    # thread simply finishes whenever the network call returns.
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        fut = pool.submit(
            InstagramService(proxy_url=purl, session_path=spath).check_session, username
        )
        try:
            valid, reason = await asyncio.wait_for(asyncio.wrap_future(fut), timeout=120)
        except (asyncio.TimeoutError, concurrent.futures.TimeoutError):
            return {"valid": False, "detail": "Session check timed out after 120s — the route to Instagram stalled"}
        return {"valid": valid, "detail": "Session is valid" if valid else f"Session invalid — {reason}"}
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


@router.post("/{account_id}/session")
@limiter.limit("10/minute")
async def upload_session(
    request: Request,
    account_id: int,
    file: UploadFile = File(...),
    _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(__import__("app.api.deps", fromlist=["get_db"]).get_db),
):
    """Upload a session JSON (created by manual_login.py / session_from_browser.py).

    Validates the payload is a JSON object containing the fields instagrapi
    needs (at minimum a cookies/authorization section), then stores it at the
    account's canonical session path.

    The dump's stable owner id (ds_user_id) is adopted on first upload and
    verified on later ones — a foreign session is rejected instead of
    silently overwriting. When the dump's username differs from the row
    (username changed on Instagram), the account auto-renames so the old
    session keeps matching and no history is lost.
    """
    from app.config import settings
    from app.utils.instagram_helpers import session_owner_info, session_path_for

    acc = await db.get(Account, account_id)
    if not acc:
        raise HTTPException(404, "Account not found")
    raw = await file.read()
    try:
        await file.close()
    except Exception:
        pass
    if not raw:
        raise HTTPException(400, "Empty file")
    if len(raw) > MAX_SESSION_BYTES:
        raise HTTPException(413, "Session file too large (max 5MB)")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise HTTPException(400, "File is not valid JSON")
    if not isinstance(payload, dict):
        raise HTTPException(400, "Session JSON must be an object")
    keys = set(payload.keys())
    if not ({"cookies", "authorization", "authorization_data", "uuids"} & keys):
        raise HTTPException(
            400,
            "Not an instagrapi session file (missing cookies/authorization data). "
            "Create it with backend/manual_login.py or session_from_browser.py.",
        )
    owner_uid, owner_name = session_owner_info(payload)
    if owner_uid and acc.ig_user_id and owner_uid != acc.ig_user_id:
        raise HTTPException(
            400,
            f"Session belongs to a different Instagram account (id {owner_uid}); "
            f"this row is linked to id {acc.ig_user_id}. Upload it to the right account.",
        )
    if owner_uid and not acc.ig_user_id:
        acc.ig_user_id = owner_uid
    extra = ""
    if owner_name and owner_name.lower() != acc.username.lower():
        old = await _rename_account(db, acc, owner_name)
        extra = f" Account auto-renamed @{old} → @{acc.username} (username changed on Instagram)."
    spath = session_path_for(acc.username, settings.MEDIA_ROOT)
    tmp = spath + ".tmp"
    with open(tmp, "wb") as f:
        f.write(raw)
    os.replace(tmp, spath)
    acc.session_file_path = spath
    await db.commit()
    await db.refresh(acc)
    await log_event("INFO", "account", f"Session uploaded for @{acc.username}")
    return {"ok": True, "detail": f"Session saved ({len(raw)} bytes).{extra} Press 'Test session' to verify."}


@router.delete("/{account_id}/session", status_code=204)
async def delete_session(account_id: int, _: str = Depends(get_current_admin), db: AsyncSession = Depends(__import__("app.api.deps", fromlist=["get_db"]).get_db)):
    acc = await db.get(Account, account_id)
    if not acc:
        raise HTTPException(404, "Account not found")
    spath = acc.session_file_path
    try:
        if spath and os.path.exists(spath):
            os.remove(spath)
    except OSError:
        pass
    acc.session_file_path = None
    await db.commit()
    await log_event("INFO", "account", f"Session removed for @{acc.username}")
    return None


@router.post("/{account_id}/cooldown")
async def set_cooldown(
    account_id: int, hours: int = Query(default=24, ge=0, le=720),
    _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(__import__("app.api.deps", fromlist=["get_db"]).get_db),
):
    acc = await db.get(Account, account_id)
    if not acc:
        raise HTTPException(404, "Account not found")
    acc.status = AccountStatus.cooldown
    acc.cooldown_until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=hours)
    await db.commit()
    import asyncio

    from app.tasks.sync_helpers import publish_sync

    await asyncio.to_thread(
        publish_sync, "account_status_change",
        {"account_id": acc.id, "status": acc.status.value},
    )
    return {"ok": True}


@router.post("/{account_id}/activate")
async def activate(
    account_id: int, _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(__import__("app.api.deps", fromlist=["get_db"]).get_db),
):
    acc = await db.get(Account, account_id)
    if not acc:
        raise HTTPException(404, "Account not found")
    acc.status = AccountStatus.active
    acc.cooldown_until = None
    await db.commit()
    import asyncio

    from app.tasks.sync_helpers import publish_sync

    await asyncio.to_thread(
        publish_sync, "account_status_change",
        {"account_id": acc.id, "status": acc.status.value},
    )
    return {"ok": True}


@router.get("/{account_id}/health")
async def account_health(
    account_id: int, _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(__import__("app.api.deps", fromlist=["get_db"]).get_db),
):
    """0-100 health score + level + reasons (Health Guard)."""
    from app.services import account_health as health

    acc = await db.get(Account, account_id)
    if not acc:
        raise HTTPException(404, "Account not found")
    out = await health.evaluate_account(db, account_id)
    if out is None:
        raise HTTPException(404, "Account not found")
    return out


@router.get("/{account_id}/analytics")
async def account_analytics(
    account_id: int, _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(__import__("app.api.deps", fromlist=["get_db"]).get_db),
):
    acc = await db.get(Account, account_id)
    if not acc:
        raise HTTPException(404, "Account not found")
    rows = (
        await db.execute(
            select(
                func.count(Post.id),
                func.coalesce(func.sum(Post.views_7d), 0),
                func.coalesce(func.sum(Post.likes_7d), 0),
                func.avg(Post.engagement_rate),
            ).where(Post.account_id == account_id, Post.status == PostStatus.posted)
        )
    ).one()
    recent = (
        await db.execute(
            select(Post).where(Post.account_id == account_id).order_by(Post.created_at.desc()).limit(10)
        )
    ).scalars().all()
    return {
        "posts": rows[0],
        "views": int(rows[1] or 0),
        "likes": int(rows[2] or 0),
        "avg_engagement": round(float(rows[3] or 0), 2),
        "recent": [{"id": p.id, "status": p.status.value, "views_7d": p.views_7d, "posted_at": p.posted_at} for p in recent],
    }
