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
- ``worker_ready`` also records ``health:worker_started_at``. The last-task
  key survives a worker restart (24h TTL), so without this a gap that is
  really an outage (crash, deploy, failed container recreate) would be
  mislabeled "Worker was busy" from a stale pre-restart task.

Task lanes (see celery_app.TASK_FAST_QUEUE/TASK_SLOW_QUEUE): with two
worker processes, every key is namespaced per lane —
``health:worker_alive:fast`` vs ``health:worker_alive:slow`` — from the
``WORKER_LANE`` env var (``worker-fast`` / ``worker-slow`` in compose).
Without the env var (single local-dev worker) keys stay un-suffixed,
exactly as before. Readers take an explicit ``lane``: ``"fast"``,
``"slow"``, ``""`` (legacy un-suffixed), or ``None`` for "this process's
own lane".

Readers:

- ``check_worker_sync`` (health_checks): ping fails + process alive + a
  task recorded -> ``"warn"`` ("busy"), not ``"down"``.
- ``system_watchdog`` gap detection: a gap explained by recorded task
  activity -> "Worker was busy" instead of "Scheduler was down".
"""
import datetime as dt
import json
import logging
import os
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

#: ISO-8601 instant of the last worker (re)start, set on worker_ready.
#: Lets gap forensics tell "worker restarted mid-gap" (genuine outage)
#: apart from "worker was busy the whole time". Overwritten on every
#: start; the TTL is only cleanup.
WORKER_STARTED_AT_KEY = "health:worker_started_at"
WORKER_STARTED_AT_TTL = 7 * 86400

#: Human labels for busy messages; unknown tasks fall back to a prettified
#: version of their last dotted segment.
TASK_LABELS = {
    "tasks.post_tasks.check_and_post": "checking the posting schedule",
    "tasks.post_tasks.execute_post": "posting a video",
    "tasks.video_tasks.process_video": "processing a video",
    "tasks.analytics_tasks.fetch_all_analytics": "fetching analytics",
    "tasks.analytics_tasks.fetch_fresh_analytics": "fetching fresh analytics",
    "tasks.analytics_tasks.fetch_post_analytics": "refreshing post analytics",
    "tasks.proxy_tasks.check_all_proxies": "checking proxies",
    "tasks.proxy_tasks.refresh_proxy_pool": "refreshing the proxy pool",
    "tasks.cleanup_tasks.clean_old_media": "cleaning up old media",
    "tasks.account_tasks.reset_daily_counts": "resetting daily counters",
    "tasks.account_tasks.scan_shadowban": "scanning for shadowbans",
    "tasks.source_tasks.ingest_source": "ingesting a source",
    "tasks.health_tasks.beat_heartbeat": "sending its heartbeat",
    "tasks.health_tasks.system_watchdog": "running the watchdog",
}


def _lane() -> str | None:
    """This worker process's task lane (``fast``/``slow``) or None.

    Set via the ``WORKER_LANE`` env var (docker-compose: worker-fast /
    worker-slow). Unset for a single local-dev worker — keys stay
    un-suffixed, exactly the pre-lane behavior. Read at call time (not
    import time) so tests can monkeypatch the env.
    """
    return os.environ.get("WORKER_LANE") or None


def _key(base: str, lane: "str | None" = None) -> str:
    """Redis key for ``base``, namespaced to ``lane``.

    ``lane=None`` means "this process's own lane". Pass an explicit lane
    (``"fast"``/``"slow"``) to read another worker's signals, or ``""`` for
    the legacy un-suffixed keys written by a single worker without
    ``WORKER_LANE``.
    """
    lane = _lane() if lane is None else lane
    return f"{base}:{lane}" if lane else base


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
    started_at = _parse_iso(state.get("started_at"))
    if started_at is not None and started_at > last_run:
        # The worker (re)started after the last watchdog run: the gap holds
        # a genuine outage (crash, deploy, failed container recreate). The
        # 24h last_task key may still name a pre-restart task — it must not
        # mislabel this outage as "busy".
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


def read_worker_state(lane: "str | None" = None) -> dict | None:
    """Best-effort worker liveness snapshot; None when Redis is unreachable.

    ``lane`` selects whose signals to read: ``"fast"``/``"slow"`` for the
    lane workers, ``""`` for the legacy un-suffixed keys, ``None`` (default)
    for this process's own lane. Returns ``{"alive": bool,
    "alive_age_s": float | None, "started_at": str | None,
    "current_task": dict | None, "last_task": dict | None}``.
    """
    try:
        client = _redis_client()
        try:
            alive_age = None
            raw = client.get(_key(WORKER_ALIVE_KEY, lane))
            ts = _parse_iso(raw) if raw else None
            if ts is not None:
                alive_age = (_utcnow() - ts).total_seconds()
            return {
                "alive": alive_age is not None and alive_age < WORKER_ALIVE_STALE_AFTER_SECONDS,
                "alive_age_s": alive_age,
                "started_at": client.get(_key(WORKER_STARTED_AT_KEY, lane)),
                "current_task": _json_get(client, _key(WORKER_CURRENT_TASK_KEY, lane)),
                "last_task": _json_get(client, _key(WORKER_LAST_TASK_KEY, lane)),
            }
        finally:
            client.close()
    except Exception:  # noqa: BLE001 — liveness introspection must never raise
        return None


def own_lane_alive(max_age_s: int = WORKER_ALIVE_STALE_AFTER_SECONDS) -> bool:
    """True when this process's lane has a fresh alive key in Redis.

    Used by the container healthcheck (docker-compose.yml): unlike
    ``inspect().ping()``, the alive key is refreshed by a daemon thread
    every 15s, so it stays fresh while the solo pool is blocked inside a
    long task — no flapping to "unhealthy" mid-render.
    """
    try:
        client = _redis_client()
        try:
            raw = client.get(_key(WORKER_ALIVE_KEY))
        finally:
            client.close()
    except Exception:  # noqa: BLE001 — healthcheck helper must never raise
        return False
    ts = _parse_iso(raw) if raw else None
    if ts is None:
        return False
    return (_utcnow() - ts).total_seconds() < max_age_s


def _record_task_start(sender=None, task=None, task_id=None, **kwargs) -> None:
    name = getattr(task, "name", None) or getattr(sender, "name", "") or ""
    try:
        client = _redis_client()
        try:
            client.set(
                _key(WORKER_CURRENT_TASK_KEY),
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
            cur = _json_get(client, _key(WORKER_CURRENT_TASK_KEY))
            client.delete(_key(WORKER_CURRENT_TASK_KEY))
            client.set(
                _key(WORKER_LAST_TASK_KEY),
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
    alive_key = _key(WORKER_ALIVE_KEY)
    while not _stop_event.wait(WORKER_ALIVE_INTERVAL_SECONDS):
        try:
            if client is None:
                client = _redis_client()
            client.set(
                alive_key,
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
    # Record the (re)start instant for gap forensics (see module docstring).
    # Best-effort: monitoring must never break worker startup.
    try:
        client = _redis_client()
        try:
            client.set(_key(WORKER_STARTED_AT_KEY), _utcnow().isoformat(), ex=WORKER_STARTED_AT_TTL)
        finally:
            client.close()
    except Exception:  # noqa: BLE001
        log.warning("worker_signals: failed to record worker start", exc_info=True)
    # The worker loads IG session files: lock down whatever is on disk,
    # including files written before the 0o600-at-dump hardening (m7).
    try:
        from app.config import settings
        from app.utils.instagram_helpers import harden_session_dir

        harden_session_dir(settings.MEDIA_ROOT)
    except Exception:
        log.warning("session dir hardening failed", exc_info=True)
    # Slow lane only: execute_post runs here, and with a single slow worker
    # no upload task can survive a container recreation (e.g. a deploy
    # mid-upload). Any post that went to 'posting' before THIS boot is
    # definitely orphaned — fail it now instead of leaving it invisible
    # until the 45-minute stale reaper fires. Posts dispatched after boot
    # (updated_at >= booted_at) are untouched: no race with a just-firing
    # slot. Best-effort: monitoring must never break worker startup.
    try:
        if _lane() in ("slow", None):
            booted_at = _utcnow()
            from app.tasks.periodic_tasks import reap_orphaned_posting

            reap_orphaned_posting(booted_at)
    except Exception:  # noqa: BLE001
        log.warning("worker_signals: orphan-posting sweep failed", exc_info=True)
    threading.Thread(target=_alive_loop, name="worker-alive", daemon=True).start()


def _stop_alive_thread(**kwargs) -> None:
    _stop_event.set()


task_prerun.connect(_record_task_start, weak=False)
task_postrun.connect(_record_task_end, weak=False)
worker_ready.connect(_start_alive_thread, weak=False)
worker_shutdown.connect(_stop_alive_thread, weak=False)
