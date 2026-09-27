"""Worker liveness signals: tell "busy" apart from "down".

The Celery worker runs with ``--pool=solo``: while it executes a long task
(uploading a reel, FFmpeg processing, analytics), its MainProcess cannot
answer ``inspect().ping()``, and periodic tasks (watchdog, check_and_post)
queue up behind it. Without extra signals both look exactly like a dead
worker — the Health page flashes red "Celery worker Down" and the watchdog
fires a spurious "Scheduler was down" after every long task.

Two signals close that gap (all state lives in Redis, best-effort, never
raised):

- ``task_prerun`` / ``task_postrun`` record the currently-executing task
  (``health:worker_current_task``) and the most recently finished one
  (``health:worker_last_task``, kept 24h for gap forensics).
- a daemon thread started on ``worker_ready`` refreshes
  ``health:worker_alive`` every ``WORKER_ALIVE_INTERVAL_SECONDS``. Threads
  keep running under the solo pool even while the main thread is blocked
  inside a task, so a fresh key proves the *process* is alive even when
  ping can't be answered.

Readers:

- ``check_worker_sync`` (health_checks): ping fails + process alive + a
  task recorded -> ``"warn"`` ("busy"), not ``"down"``.
- ``system_watchdog`` gap detection: a gap explained by recorded task
  activity -> "Worker was busy" instead of "Scheduler was down".
"""
import datetime as dt
import json
import logging
import threading

from celery.signals import task_postrun, task_prerun, worker_ready, worker_shutdown

log = logging.getLogger("igfunnel.worker_signals")

#: Liveness key the worker_ready thread refreshes; readers treat a key older
#: than WORKER_ALIVE_STALE_AFTER_SECONDS as "process dead".
WORKER_ALIVE_KEY = "health:worker_alive"
WORKER_ALIVE_INTERVAL_SECONDS = 15
WORKER_ALIVE_STALE_AFTER_SECONDS = 60

#: {task, id, started_at} while a task executes; removed on postrun.
#: The TTL is only cleanup — readers judge staleness from started_at.
WORKER_CURRENT_TASK_KEY = "health:worker_current_task"
WORKER_CURRENT_TASK_TTL = 7200

#: {task, started_at, finished_at} of the most recently finished task.
WORKER_LAST_TASK_KEY = "health:worker_last_task"
WORKER_LAST_TASK_TTL = 86400

#: Human labels for busy messages; unknown tasks fall back to a prettified
#: version of their last dotted segment.
TASK_LABELS = {
    "tasks.post_tasks.check_and_post": "checking the posting schedule",
    "tasks.post_tasks.execute_post": "posting a video",
    "tasks.video_tasks.process_video": "processing a video",
    "tasks.analytics_tasks.fetch_all_analytics": "fetching analytics",
    "tasks.proxy_tasks.check_all_proxies": "checking proxies",
    "tasks.proxy_tasks.refresh_proxy_pool": "refreshing the proxy pool",
    "tasks.cleanup_tasks.clean_old_media": "cleaning up old media",
    "tasks.account_tasks.reset_daily_counts": "resetting daily counters",
    "tasks.source_tasks.ingest_source": "ingesting a source",
    "tasks.health_tasks.beat_heartbeat": "sending its heartbeat",
    "tasks.health_tasks.system_watchdog": "running the watchdog",
}


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _redis_client():
    import redis

    from app.config import settings

    return redis.from_url(settings.REDIS_URL, decode_responses=True, socket_timeout=5)


def _parse_iso(raw: str | None) -> dt.datetime | None:
    if not raw:
        return None
    try:
        ts = dt.datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    return ts


def _json_get(client, key: str) -> dict | None:
    raw = client.get(key)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def describe_task(task_name: str) -> str:
    """Human label for a Celery task name, e.g. 'posting a video'."""
    if not task_name:
        return "working"
    if task_name in TASK_LABELS:
        return TASK_LABELS[task_name]
    return task_name.rsplit(".", 1)[-1].replace("_", " ")


def task_age_human(started_at: str | None) -> str | None:
    """'started 3m ago' suffix for busy messages; None when unparseable."""
    ts = _parse_iso(started_at)
    if ts is None:
        return None
    secs = max(0, int((_utcnow() - ts).total_seconds()))
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    return f"{secs // 3600}h ago"


#: The watchdog task's own name — its execution must never "explain" a gap.
#: task_prerun records the watchdog itself as the current task before its
#: body runs; the gap is precisely the watchdog NOT having run, so seeing
#: itself must fall through to the last-task/outage branches.
_WATCHDOG_TASK_NAME = "tasks.health_tasks.system_watchdog"


def busy_task_during_gap(last_run: dt.datetime) -> str | None:
    """Task name that kept the worker busy through a watchdog gap.

    A gap means no watchdog run between ``last_run`` and now. Under the solo
    pool that also happens when the worker was simply executing a long task
    (reel upload, FFmpeg, analytics) — the watchdog itself queued behind it.
    Returns the task name when recorded task activity explains the gap,
    else None (genuine outage: worker or beat really was down).

    Note: no alive-key check here — the caller IS the watchdog running on
    the worker, which already proves the process is alive. (A crashed
    worker's stale current_task can't survive anyway: task_prerun
    overwrites the key the moment the new process runs anything.)
    """
    state = read_worker_state()
    if not state:
        return None
    cur = state.get("current_task") or {}
    task_name = cur.get("task", "")
    if task_name and task_name != _WATCHDOG_TASK_NAME:
        # A task is executing RIGHT NOW. Under the solo pool the watchdog
        # itself queues behind it, so the gap is explained no matter when
        # the task started. (The old `started <= last_run` check missed the
        # common case — a task that began after the last watchdog run — and
        # produced a spurious "Scheduler was down".)
        return task_name
    last = state.get("last_task") or {}
    finished = _parse_iso(last.get("finished_at"))
    if finished is not None and finished > last_run:
        return last.get("task", "")
    return None


def read_worker_state() -> dict | None:
    """Best-effort worker liveness snapshot; None when Redis is unreachable.

    Returns ``{"alive": bool, "alive_age_s": float | None,
    "current_task": dict | None, "last_task": dict | None}``.
    """
    try:
        client = _redis_client()
        try:
            alive_age = None
            raw = client.get(WORKER_ALIVE_KEY)
            ts = _parse_iso(raw) if raw else None
            if ts is not None:
                alive_age = (_utcnow() - ts).total_seconds()
            return {
                "alive": alive_age is not None and alive_age < WORKER_ALIVE_STALE_AFTER_SECONDS,
                "alive_age_s": alive_age,
                "current_task": _json_get(client, WORKER_CURRENT_TASK_KEY),
                "last_task": _json_get(client, WORKER_LAST_TASK_KEY),
            }
        finally:
            client.close()
    except Exception:  # noqa: BLE001 — liveness introspection must never raise
        return None


def _record_task_start(sender=None, task=None, task_id=None, **kwargs) -> None:
    name = getattr(task, "name", None) or getattr(sender, "name", "") or ""
    try:
        client = _redis_client()
        try:
            client.set(
                WORKER_CURRENT_TASK_KEY,
                json.dumps(
                    {
                        "task": name,
                        "id": task_id,
                        "started_at": _utcnow().isoformat(),
                    }
                ),
                ex=WORKER_CURRENT_TASK_TTL,
            )
        finally:
            client.close()
    except Exception:  # noqa: BLE001 — monitoring must never break tasks
        log.warning("worker_signals: failed to record task start", exc_info=True)


def _record_task_end(sender=None, task=None, task_id=None, **kwargs) -> None:
    name = getattr(task, "name", None) or getattr(sender, "name", "") or ""
    now = _utcnow()
    try:
        client = _redis_client()
        try:
            cur = _json_get(client, WORKER_CURRENT_TASK_KEY)
            client.delete(WORKER_CURRENT_TASK_KEY)
            client.set(
                WORKER_LAST_TASK_KEY,
                json.dumps(
                    {
                        "task": name,
                        "started_at": (cur or {}).get("started_at"),
                        "finished_at": now.isoformat(),
                    }
                ),
                ex=WORKER_LAST_TASK_TTL,
            )
        finally:
            client.close()
    except Exception:  # noqa: BLE001 — monitoring must never break tasks
        log.warning("worker_signals: failed to record task end", exc_info=True)


_stop_event = threading.Event()


def _alive_loop() -> None:
    client = None
    while not _stop_event.wait(WORKER_ALIVE_INTERVAL_SECONDS):
        try:
            if client is None:
                client = _redis_client()
            client.set(
                WORKER_ALIVE_KEY,
                _utcnow().isoformat(),
                ex=WORKER_ALIVE_STALE_AFTER_SECONDS,
            )
        except Exception:  # noqa: BLE001 — a failed heartbeat is not fatal
            log.warning("worker_signals: alive heartbeat failed", exc_info=True)
            try:
                if client is not None:
                    client.close()
            except Exception:  # noqa: BLE001
                pass
            client = None


def _start_alive_thread(**kwargs) -> None:
    # worker_ready can fire more than once across restarts in one process.
    if getattr(_start_alive_thread, "_started", False):
        return
    _start_alive_thread._started = True
    # The worker loads IG session files: lock down whatever is on disk,
    # including files written before the 0o600-at-dump hardening (m7).
    try:
        from app.config import settings
        from app.utils.instagram_helpers import harden_session_dir

        harden_session_dir(settings.MEDIA_ROOT)
    except Exception:
        log.warning("session dir hardening failed", exc_info=True)
    threading.Thread(target=_alive_loop, name="worker-alive", daemon=True).start()


def _stop_alive_thread(**kwargs) -> None:
    _stop_event.set()


task_prerun.connect(_record_task_start, weak=False)
task_postrun.connect(_record_task_end, weak=False)
worker_ready.connect(_start_alive_thread, weak=False)
worker_shutdown.connect(_stop_alive_thread, weak=False)
