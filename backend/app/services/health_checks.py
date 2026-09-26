"""Synchronous health checks shared by two consumers.

- The ``GET /api/v1/system/health`` endpoint (runs them in a thread so the
  event loop never blocks).
- The ``system_watchdog`` Celery task, which turns state *changes* into
  dashboard notifications.

Each check returns ``(status, message)`` where status is ``"ok"`` or
``"down"``. Checks are the 4 critical pipeline dependencies: database,
Redis, Celery worker, Celery beat (via its per-minute heartbeat key — a
live beat process that stopped ticking still counts as down). A check must
never raise; unexpected errors are reported as ``"down"`` with the
exception text so the caller always gets a usable verdict.
"""
import datetime as dt
import logging
from urllib.parse import urlparse

from app.config import settings

log = logging.getLogger("igfunnel.health")

#: Beat heartbeat older than this is "down" (heartbeat ticks every 60s).
BEAT_STALE_AFTER_SECONDS = 150

#: The Redis key the beat heartbeat task refreshes every minute.
BEAT_HEARTBEAT_KEY = "health:beat_heartbeat"


def _verdict(fn):
    def wrapper():
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — a check must never raise
            return "down", f"{type(exc).__name__}: {exc}"

    wrapper.__name__ = fn.__name__
    return wrapper


@_verdict
def check_database_sync() -> tuple[str, str]:
    from sqlalchemy import text as sa_text

    from app.database import SyncSessionLocal

    with SyncSessionLocal() as session:
        session.execute(sa_text("SELECT 1"))
    return "ok", "Query round-trip ok"


@_verdict
def check_redis_sync() -> tuple[str, str]:
    import redis

    host = urlparse(settings.REDIS_URL).hostname or "?"
    port = urlparse(settings.REDIS_URL).port or 6379
    client = redis.from_url(settings.REDIS_URL, decode_responses=True, socket_timeout=5)
    try:
        if client.ping():
            return "ok", f"PONG from {host}:{port}"
        return "down", f"No PONG from {host}:{port}"
    finally:
        client.close()


@_verdict
def check_worker_sync() -> tuple[str, str]:
    from app.tasks.celery_app import celery

    pong = celery.control.inspect(timeout=5).ping() or {}
    alive = sorted(
        node
        for node, reply in pong.items()
        if isinstance(reply, dict) and reply.get("ok") == "pong"
    )
    if alive:
        return "ok", f"{len(alive)} worker(s) alive: {', '.join(alive)}"
    return "down", "No worker replied — worker down or broker URL wrong"


@_verdict
def check_beat_sync() -> tuple[str, str]:
    import redis

    client = redis.from_url(settings.REDIS_URL, decode_responses=True, socket_timeout=5)
    try:
        raw = client.get(BEAT_HEARTBEAT_KEY)
        if not raw:
            return "down", "No heartbeat yet — beat hasn't ticked (or just restarted)"
        ts = dt.datetime.fromisoformat(raw)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
        age = (dt.datetime.now(dt.timezone.utc) - ts).total_seconds()
        if age < BEAT_STALE_AFTER_SECONDS:
            return "ok", f"Last tick {int(age)}s ago"
        return "down", f"Last tick {int(age)}s ago — beat is not scheduling"
    finally:
        client.close()


#: The critical components the watchdog monitors, in display order.
CRITICAL_CHECKS = (
    ("database", "Database", check_database_sync),
    ("redis", "Redis", check_redis_sync),
    ("celery_worker", "Celery worker", check_worker_sync),
    ("celery_beat", "Celery beat", check_beat_sync),
)
