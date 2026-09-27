"""Synchronous health checks shared by two consumers.

- The ``GET /api/v1/system/health`` endpoint (runs them in a thread so the
  event loop never blocks).
- The ``system_watchdog`` Celery task, which turns state *changes* into
  dashboard notifications.

Each check returns ``(status, message)`` where status is ``"ok"``, ``"warn"``
or ``"down"``. Checks are the critical pipeline dependencies: database,
Redis, the two Celery worker lanes (fast: scheduler ticks/watchdog;
slow: FFmpeg/uploads/ingest/analytics), Celery beat (via its per-minute
heartbeat key — a live beat process that stopped ticking still counts as
down). A check must never raise; unexpected errors are reported as
``"down"`` with the exception text so the caller always gets a usable
verdict.
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
    """Aggregate worker check (backwards-compatible).

    Ping first: any pong means at least one worker is alive and consuming.
    With no ping reply (solo pool: every worker is inside a task), fall
    back to the per-lane liveness signals, then to the legacy un-suffixed
    keys of a single pre-lane worker. The Health page prefers the per-lane
    checks below; this aggregate stays for the watchdog-era callers and
    tests.
    """
    from app.tasks import worker_signals as _ws
    from app.tasks.celery_app import celery

    pong = celery.control.inspect(timeout=5).ping() or {}
    alive = sorted(
        node
        for node, reply in pong.items()
        if isinstance(reply, dict) and reply.get("ok") == "pong"
    )
    if alive:
        return "ok", f"{len(alive)} worker(s) alive: {', '.join(alive)}"
    # No ping reply. Under the solo pool the workers can't answer while
    # they execute tasks — consult the liveness signals before crying "down".
    # A lane only counts as "busy" (warn) when its heartbeat is FRESH: a
    # stale alive key is evidence the process is dead, and must stay "down".
    warn_parts = []
    dead_parts = []
    for lane in ("fast", "slow"):
        state = _ws.read_worker_state(lane=lane)
        if not _has_signal_data(state):
            continue
        (warn_parts if state.get("alive") else dead_parts).append(
            f"{lane} lane: {_busy_line(_ws, state)}"
        )
    if not warn_parts and not dead_parts:
        state = _ws.read_worker_state(lane="")
        if _has_signal_data(state):
            (warn_parts if state.get("alive") else dead_parts).append(
                _busy_line(_ws, state)
            )
    if warn_parts:
        # Mixed state (one lane busy, another dead): still warn — the dead
        # lane is named in the message, and the Health page shows each lane
        # separately anyway.
        return "warn", "; ".join(warn_parts + dead_parts)
    if dead_parts:
        return "down", "; ".join(dead_parts)
    return "down", "No worker replied — worker down or broker URL wrong"


def _has_signal_data(state: "dict | None") -> bool:
    return bool(
        state
        and (
            state.get("alive")
            or state.get("current_task")
            or state.get("last_task")
            or state.get("alive_age_s") is not None
        )
    )


def _busy_line(_ws, state: dict) -> str:
    task = state.get("current_task") or {}
    name = task.get("task", "")
    if name:
        age = _ws.task_age_human(task.get("started_at"))
        return (
            f"busy {_ws.describe_task(name)}"
            + (f" (started {age})" if age else "")
            + " — alive, but can't answer ping while a task runs (solo pool)"
        )
    if state.get("alive"):
        return "process is alive but didn't answer ping (transient)"
    return "process looks dead (stale alive key)"


def _check_worker_lane(lane: str, label: str) -> tuple[str, str]:
    """One lane's health: ping by worker hostname, else that lane's signals.

    In compose the workers run with ``--hostname=fast`` / ``--hostname=slow``,
    so a broadcast ping reply can be attributed to a lane. A single pre-lane
    worker (local dev) has neither hostname nor suffixed keys — then this
    falls back to the legacy un-suffixed signals for either lane.
    """
    from app.tasks import worker_signals as _ws
    from app.tasks.celery_app import celery

    pong = celery.control.inspect(timeout=5).ping() or {}
    # Ping replies are keyed by full node name ("fast@container-id"), so
    # match the lane prefix — an exact key never occurs in practice.
    reply = None
    for node, payload in pong.items():
        if node == lane or node.startswith(lane + "@"):
            reply = payload
            break
    if isinstance(reply, dict) and reply.get("ok") == "pong":
        return "ok", f"{label} alive (ping)"
    state = _ws.read_worker_state(lane=lane)
    if not _has_signal_data(state):
        # Pre-lane single worker: un-suffixed keys.
        state = _ws.read_worker_state(lane="")
    if not _has_signal_data(state):
        return "down", f"{label} down — no ping reply and no liveness signals"
    if state.get("alive"):
        return "warn", f"{label}: {_busy_line(_ws, state)}"
    return "down", f"{label} down — {_busy_line(_ws, state)}"


@_verdict
def check_worker_fast_sync() -> tuple[str, str]:
    """Health of the fast lane (scheduler ticks, watchdog, heartbeats)."""
    return _check_worker_lane("fast", "Fast lane")


@_verdict
def check_worker_slow_sync() -> tuple[str, str]:
    """Health of the slow lane (FFmpeg, uploads, ingest, analytics)."""
    return _check_worker_lane("slow", "Slow lane")


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
#: The worker is split into two lanes (see celery_app task lanes): the fast
#: lane runs the scheduler ticks and the watchdog itself, the slow lane the
#: long jobs. A dead slow lane means videos silently stop processing, so it
#: is monitored separately.
CRITICAL_CHECKS = (
    ("database", "Database", check_database_sync),
    ("redis", "Redis", check_redis_sync),
    ("celery_worker_fast", "Celery worker (fast lane)", check_worker_fast_sync),
    ("celery_worker_slow", "Celery worker (slow lane)", check_worker_slow_sync),
    ("celery_beat", "Celery beat", check_beat_sync),
)
