"""Health-monitoring tasks (fully synchronous, Celery-safe, no event loop)."""
import datetime as dt
import logging

from app.tasks.celery_app import celery

log = logging.getLogger("igfunnel.tasks")

#: Redis key the beat heartbeat refreshes every minute. The /system/health
#: endpoint treats a missing/stale key as "beat down" — a live beat process
#: that stopped ticking still counts as down.
BEAT_HEARTBEAT_KEY = "health:beat_heartbeat"
BEAT_HEARTBEAT_TTL = 180


@celery.task(name="tasks.health_tasks.beat_heartbeat")
def beat_heartbeat():
    """Prove beat is alive and scheduling: refresh the heartbeat key."""
    import redis

    from app.config import settings

    try:
        client = redis.from_url(settings.REDIS_URL, socket_timeout=5)
        client.set(
            BEAT_HEARTBEAT_KEY,
            dt.datetime.now(dt.timezone.utc).isoformat(),
            ex=BEAT_HEARTBEAT_TTL,
        )
    except Exception as exc:  # noqa: BLE001 — Redis being down is reported by /health itself
        log.warning("beat heartbeat write failed: %s", exc)


#: How often beat runs the watchdog (kept in sync with celery_app beat_schedule).
WATCHDOG_INTERVAL_MINUTES = 2

#: A gap this many times the interval means the scheduler itself was down.
WATCHDOG_GAP_FACTOR = 3


def _watchdog_checks():
    """Critical checks the watchdog runs — everything except the worker.

    The watchdog executes on the worker itself, and under the solo pool the
    worker's MainProcess is blocked running this task, so it can never
    answer its own inspect().ping(). The /system/health endpoint still
    checks the worker from the backend.
    """
    from app.services.health_checks import CRITICAL_CHECKS

    return [c for c in CRITICAL_CHECKS if c[0] != "celery_worker"]


@celery.task(name="tasks.health_tasks.system_watchdog")
def system_watchdog():
    """Turn critical-component state CHANGES into dashboard notifications.

    Runs every WATCHDOG_INTERVAL_MINUTES on the worker. For each critical
    component except the Celery worker itself — database, Redis, Celery
    beat — (the worker can't ping itself under the solo pool; /system/health
    covers it from the backend):
    - down, with no unread "down" notification -> one critical notification
      (dedup_key ``watchdog:{name}:down`` fires exactly once per outage);
    - back up while an unread "down" notification exists -> the down
      notification is auto-resolved (marked read) and a one-off "recovered"
      success notification is posted.

    It also detects scheduler gaps: when the previous watchdog run is older
    than WATCHDOG_GAP_FACTOR x interval, the worker or beat was down in
    between, so it posts a warning that scheduled posts may have been
    missed — the complement of the posting grace window, which only covers
    brief outages.

    Finally it prunes notifications older than NOTIFICATION_RETENTION_DAYS.
    Never raises: monitoring must not break the worker.
    """
    import datetime as _dt

    from sqlalchemy import delete, select

    from app.database import SyncSessionLocal
    from app.models import Notification, Setting
    from app.tasks.sync_helpers import NOTIFICATION_RETENTION_DAYS, get_setting, notify_sync

    # The watchdog runs ON the worker, and the worker uses the solo pool:
    # its MainProcess is blocked executing this very task, so it can never
    # answer its own inspect().ping() — including the worker check here
    # would raise a spurious "Celery worker down" critical notification
    # every 2 minutes (and burn the 5s ping timeout each time). The
    # /system/health endpoint still pings the worker from the backend.
    checks = _watchdog_checks()

    try:
        now = _dt.datetime.now(_dt.timezone.utc)

        # --- scheduler gap detection (worker/beat were down) ---
        gap_minutes: int | None = None
        with SyncSessionLocal() as session:
            last_raw = get_setting(session, "watchdog_last_run", "")
            if last_raw:
                try:
                    last_run = _dt.datetime.fromisoformat(last_raw)
                    if last_run.tzinfo is None:
                        last_run = last_run.replace(tzinfo=_dt.timezone.utc)
                    gap = (now - last_run).total_seconds()
                    if gap > WATCHDOG_INTERVAL_MINUTES * 60 * WATCHDOG_GAP_FACTOR:
                        gap_minutes = int(gap // 60)
                except ValueError:
                    pass
            row = session.get(Setting, "watchdog_last_run")
            if row is None:
                session.add(Setting(key="watchdog_last_run", value=now.isoformat()))
            else:
                row.value = now.isoformat()
            session.commit()
        if gap_minutes is not None:
            notify_sync(
                "scheduler_gap",
                "warning",
                "Scheduler was down",
                f"No watchdog run for ~{gap_minutes} minutes — the worker or beat "
                "was down. Scheduled posts in that window may have been "
                "missed (the grace window only covers brief outages).",
                link="/dashboard/health",
            )

        # --- per-component state transitions ---
        # Each DB touch is its own short session: holding one session open
        # across checks + notify_sync (which opens its own) deadlocks
        # SQLite with "database is locked".
        for name, label, check in checks:
            status, message = check()
            dedup_key = f"watchdog:{name}:down"
            fire_down = False
            fire_recovered = False
            with SyncSessionLocal() as session:
                down_notif = (
                    session.execute(
                        select(Notification)
                        .where(
                            Notification.dedup_key == dedup_key,
                            Notification.read_at.is_(None),
                        )
                        .limit(1)
                    )
                    .scalars()
                    .first()
                )
                if status == "down":
                    fire_down = down_notif is None
                elif down_notif is not None:
                    down_notif.read_at = now
                    session.commit()
                    fire_recovered = True
            if fire_down:
                notify_sync(
                    "component_down",
                    "critical",
                    f"{label} is down",
                    f"{label} check failed: {message}. Scheduled posting may be affected.",
                    link="/dashboard/health",
                    dedup_key=dedup_key,
                )
            elif fire_recovered:
                notify_sync(
                    "component_recovered",
                    "success",
                    f"{label} recovered",
                    f"{label} is reachable again.",
                    link="/dashboard/health",
                )

        # --- retention prune ---
        with SyncSessionLocal() as session:
            cutoff = now - _dt.timedelta(days=NOTIFICATION_RETENTION_DAYS)
            session.execute(delete(Notification).where(Notification.created_at < cutoff))
            session.commit()
        log.info("system_watchdog ran (gap_minutes=%s)", gap_minutes)
    except Exception:  # noqa: BLE001 — monitoring must never break the worker
        log.exception("system_watchdog failed")
