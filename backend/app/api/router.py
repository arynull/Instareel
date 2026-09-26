"""Main API router â€” mounts every resource under /api/v1."""
from fastapi import APIRouter

from app.api import auth
from app.api.accounts import router as accounts_router
from app.api.notifications import notifications_router
from app.api.resources import audio_router, bio_router, effect_router, proxy_router
from app.api.scheduling import caption_router, hashtag_router, schedule_router
from app.api.sources import source_router
from app.api.system import analytics_router, logs_router, settings_router, system_router, ws_router
from app.api.videos import posts_router, router as videos_router

router = APIRouter(prefix="/api/v1")
router.include_router(auth.router, prefix="/auth", tags=["auth"])
router.include_router(accounts_router, prefix="/accounts", tags=["accounts"])
router.include_router(videos_router, prefix="/videos", tags=["videos"])
router.include_router(posts_router, prefix="/posts", tags=["posts"])
router.include_router(schedule_router, prefix="/schedule", tags=["schedule"])
router.include_router(source_router, prefix="/sources", tags=["sources"])
router.include_router(caption_router, prefix="/captions", tags=["captions"])
router.include_router(hashtag_router, prefix="/hashtags", tags=["hashtags"])
router.include_router(bio_router, prefix="/bios", tags=["bios"])
router.include_router(proxy_router, prefix="/proxies", tags=["proxies"])
router.include_router(effect_router, prefix="/effects", tags=["effects"])
router.include_router(audio_router, prefix="/audio", tags=["audio"])
router.include_router(analytics_router, prefix="/analytics", tags=["analytics"])
router.include_router(logs_router, prefix="/logs", tags=["logs"])
router.include_router(settings_router, prefix="/settings", tags=["settings"])
router.include_router(system_router, prefix="/system", tags=["system"])
router.include_router(notifications_router, prefix="/notifications", tags=["notifications"])
# WebSocket is mounted on the app root (not under /api/v1) by main.py.
ws_mount = ws_router
