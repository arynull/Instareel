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
