"""Celery app + beat schedule (all periodic tasks defined here)."""
import os

from celery import Celery
from celery.schedules import crontab

from app.config import settings

celery = Celery("igfunnel", broker=settings.REDIS_URL, backend=settings.REDIS_URL)
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
)
celery.autodiscover_tasks(["app.tasks"])

celery.conf.beat_schedule = {
    "check-scheduled-posts": {"task": "tasks.post_tasks.check_and_post", "schedule": crontab(minute="*")},
    # Liveness proof for the Health page: a live-but-stuck beat still counts as down.
    "beat-heartbeat": {"task": "tasks.health_tasks.beat_heartbeat", "schedule": crontab(minute="*")},
    # Turns critical-component state changes into dashboard notifications.
    "system-watchdog": {"task": "tasks.health_tasks.system_watchdog", "schedule": crontab(minute="*/2")},
    "fetch-analytics": {"task": "tasks.analytics_tasks.fetch_all_analytics", "schedule": crontab(hour="*/4")},
    "proxy-health-check": {"task": "tasks.proxy_tasks.check_all_proxies", "schedule": crontab(minute="*/30")},
    "proxy-pool-refresh": {"task": "tasks.proxy_tasks.refresh_proxy_pool", "schedule": crontab(hour="*/3", minute=17)},
    "media-cleanup": {"task": "tasks.cleanup_tasks.clean_old_media", "schedule": crontab(hour=4, minute=0)},
    "reset-daily-counts": {"task": "tasks.account_tasks.reset_daily_counts", "schedule": crontab(hour=0, minute=0)},
}

# Explicit imports so workers always register tasks (autodiscover is
# unreliable for the package that holds the Celery app itself, esp. on Windows).
import app.tasks.health_tasks  # noqa: F401
import app.tasks.periodic_tasks  # noqa: F401
import app.tasks.post_tasks  # noqa: F401
import app.tasks.source_tasks  # noqa: F401
import app.tasks.video_tasks  # noqa: F401
