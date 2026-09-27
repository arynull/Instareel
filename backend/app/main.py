"""FastAPI entrypoint: middleware, error envelope, health, routers, startup init."""
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.api.deps import limiter
from app.api.router import router, ws_mount
from app.config import settings
from app.core.exceptions import AppError
from app.core.middleware import setup_middleware
from app.database import Base, engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("igfunnel")


def _safe_db_label(url: str) -> str:
    """DB URL with credentials stripped — safe for logs."""
    from urllib.parse import urlparse

    try:
        p = urlparse(url)
    except ValueError:
        return "<unparseable>"
    if p.scheme.startswith("sqlite"):
        return f"sqlite:{p.path or ':memory:'}"
    host = p.hostname or "?"
    return f"{p.scheme}://{host}:{p.port or '?'}/{p.path.lstrip('/')}"


def _safe_redis_label(url: str) -> str:
    from urllib.parse import urlparse

    try:
        p = urlparse(url)
    except ValueError:
        return "<unparseable>"
    return f"{p.hostname or '?'}:{p.port or 6379}/{p.path.lstrip('/') or '0'}"


def log_effective_config() -> None:
    """Log the effective (non-secret) configuration at startup.

    Misconfigurations (wrong REDIS_URL host, relative sqlite path, wrong
    SCHEDULE_TZ) are the classic silent killers here — surfacing them in
    the boot log makes them visible without leaking any secret.
    """
    log.info(
        "Effective config: env=%s tz=%s db=%s redis=%s media_root=%s grace_min=%s",
        settings.ENV,
        settings.SCHEDULE_TZ,
        _safe_db_label(settings.DATABASE_URL),
        _safe_redis_label(settings.REDIS_URL),
        settings.MEDIA_ROOT,
        settings.SCHEDULE_GRACE_MINUTES,
    )


@asynccontextmanager
async def lifespan(application: FastAPI):
    # Fail closed: default/empty SECRET_KEY lets anyone forge admin JWTs,
    # and default/empty admin credentials let anyone log in.
    settings.validate_security()
    os.makedirs(settings.MEDIA_ROOT, exist_ok=True)
    for sub in ("raw", "processed", "thumbnails", "sessions", "audio", "profile_pics"):
        os.makedirs(os.path.join(settings.MEDIA_ROOT, sub), exist_ok=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    from app.api.system import seed_default_settings

    n = await seed_default_settings()
    if n:
        log.info("Seeded %d default settings", n)
    log_effective_config()
    log.info("IG Funnel API ready (env=%s)", settings.ENV)
    yield


def create_app() -> FastAPI:
    """App factory — lets tests build variants (e.g. docs on/off) without
    re-importing the module."""
    docs = settings.DOCS_ENABLED
    application = FastAPI(
        title="IG Funnel API",
        version="1.0.0",
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
        lifespan=lifespan,
    )
    application.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    setup_middleware(application, limiter)

    @application.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.code, "message": exc.message, "details": exc.details},
        )

    @application.get("/health")
    async def health():
        return {"ok": True, "env": settings.ENV}

    application.include_router(router)
    application.include_router(ws_mount)

    return application


app = create_app()
