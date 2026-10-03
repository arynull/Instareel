"""Celery app + beat schedule (all periodic tasks defined here)."""
import os

from celery import Celery
from celery.schedules import crontab

from app.config import settings

celery = Celery("igfunnel", broker=settings.REDIS_URL, backend=settings.REDIS_URL)

#: Task lanes. Every worker runs ``--pool=solo``, i.e. one process executes a
#: single task at a time. Without lanes, a 10-minute FFmpeg render (or a slow
#: reel upload) stalls the per-minute scheduler ticks queued behind it —
#: posts go out late and the watchdog sees phantom "Scheduler was down"
#: gaps. Two queues fix that at the architecture level:
#:
#: - ``fast``: time-sensitive ticks that must never wait (scheduler,
#:   watchdog, heartbeats, proxy checks, cleanup).
#: - ``slow``: long-running jobs (video processing, uploads, ingest,
#:   analytics sweeps).
#:
#: Each queue is consumed by its own worker process (docker-compose.yml:
#: ``worker-fast`` / ``worker-slow``). On a 1-vCPU box the two processes
#: still time-share the CPU, but the OS scheduler keeps the fast lane
#: responsive while the slow lane renders — a stuck FFmpeg job can no
#: longer silence the scheduler.
TASK_FAST_QUEUE = "fast"
TASK_SLOW_QUEUE = "slow"

#: Tasks routed to the slow lane. Everything else (including every beat
#: tick) stays on the fast lane via ``task_default_queue``. Beat-published
#: and ``.delay()``-published tasks both go through this router, so call
#: sites need no changes.
SLOW_TASKS = frozenset(
    {
        "tasks.video_tasks.process_video",  # FFmpeg renders: minutes each
        "tasks.post_tasks.execute_post",  # reel upload: ~6 min blocking
        "tasks.source_tasks.ingest_source",  # paginated IG listing
        "tasks.analytics_tasks.fetch_all_analytics",  # ~75s per post sweep
        "tasks.analytics_tasks.fetch_fresh_analytics",  # hourly young-post lane
    }
)

celery.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    # Beat crontabs (daily reset, cleanup, …) tick in the schedule timezone
    # too, so "midnight" means the user's midnight, like rule hours do.
    timezone=settings.SCHEDULE_TZ,
    enable_utc=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    # Lane routing (see above). The default queue must exist as a worker
    # target: worker-fast consumes it, and a lone local-dev worker should
    # run with ``-Q fast,slow`` to drain both. (No explicit task_queues:
    # the Redis broker auto-creates queues on first publish.)
    task_default_queue=TASK_FAST_QUEUE,
    task_routes={name: {"queue": TASK_SLOW_QUEUE} for name in SLOW_TASKS},
)
celery.autodiscover_tasks(["app.tasks"])

celery.conf.beat_schedule = {
    "check-scheduled-posts": {"task": "tasks.post_tasks.check_and_post", "schedule": crontab(minute="*")},
    # Liveness proof for the Health page: a live-but-stuck beat still counts as down.
    "beat-heartbeat": {"task": "tasks.health_tasks.beat_heartbeat", "schedule": crontab(minute="*")},
    # Turns critical-component state changes into dashboard notifications.
    "system-watchdog": {"task": "tasks.health_tasks.system_watchdog", "schedule": crontab(minute="*/2")},
    # NOTE: crontab() defaults minute to "*", so an hour-only crontab
    # fires EVERY MINUTE of those hours (this exact bug ran the sweep
    # 60x per 4h window on 2026-10-03). Always pin the minute.
    "fetch-analytics": {
        "task": "tasks.analytics_tasks.fetch_all_analytics",
        "schedule": crontab(minute=0, hour="*/4"),
    },
    # Hourly fast lane for young reels: re-check posts <24h old so the
    # panel tracks fast-moving view counts within ~1h instead of ~4h.
    # minute=12 keeps it clear of the :00 sweep and other hourly tasks.
    "fetch-fresh-analytics": {
        "task": "tasks.analytics_tasks.fetch_fresh_analytics",
        "schedule": crontab(minute=12),
    },
    "proxy-health-check": {"task": "tasks.proxy_tasks.check_all_proxies", "schedule": crontab(minute="*/30")},
    "proxy-pool-refresh": {"task": "tasks.proxy_tasks.refresh_proxy_pool", "schedule": crontab(hour="*/3", minute=17)},
    "media-cleanup": {"task": "tasks.cleanup_tasks.clean_old_media", "schedule": crontab(hour=4, minute=0)},
    "reset-daily-counts": {"task": "tasks.account_tasks.reset_daily_counts", "schedule": crontab(hour=0, minute=0)},
    # Fail posts wedged in 'posting' (worker died mid-upload) before they can
    # silently wedge or double-post via the stale-sibling window.
    "reap-stale-posting": {"task": "tasks.post_tasks.reap_stale_posting", "schedule": crontab(minute="*/10")},
    # Shadowban / action-block watch: views-collapse heuristic per account
    # (fast lane — a short DB scan, no IG calls).
    "shadowban-scan": {"task": "tasks.account_tasks.scan_shadowban", "schedule": crontab(hour="*/6", minute=23)},
}

# Explicit imports so workers always register tasks (autodiscover is
# unreliable for the package that holds the Celery app itself, esp. on Windows).
import app.tasks.health_tasks  # noqa: F401
import app.tasks.periodic_tasks  # noqa: F401
import app.tasks.post_tasks  # noqa: F401
import app.tasks.source_tasks  # noqa: F401
import app.tasks.video_tasks  # noqa: F401
import app.tasks.worker_signals  # noqa: F401 — task_prerun/postrun liveness signals
