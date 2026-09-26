"""Central application settings (pydantic-settings, loaded from .env)."""
import os
from functools import lru_cache
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///./data/app.db"
DEFAULT_SYNC_DATABASE_URL = "sqlite:///./data/app.db"


def _sqlite_file_path(db_url: str) -> str | None:
    """Filesystem path SQLAlchemy will open for a sqlite URL.

    Returns None for non-sqlite URLs, empty databases, or URLs SQLAlchemy
    itself cannot parse (it will raise its own error at engine creation).
    """
    try:
        from sqlalchemy.engine import make_url

        url = make_url(db_url)
    except Exception:
        return None
    if url.get_backend_name() != "sqlite":
        return None
    return url.database or None


def _running_in_container() -> bool:
    """True inside a Docker container (/.dockerenv is created by the runtime)."""
    return os.path.exists("/.dockerenv")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    APP_NAME: str = "IG Funnel"
    ENV: str = "production"
    SECRET_KEY: str = "change-me"
    FERNET_KEY: str = ""
    APP_BASE_URL: str = "http://localhost:8000"

    DATABASE_URL: str = DEFAULT_DATABASE_URL
    # Sync URL for Celery workers (sync SQLAlchemy engine — never asyncio in tasks).
    SYNC_DATABASE_URL: str = DEFAULT_SYNC_DATABASE_URL
    REDIS_URL: str = "redis://localhost:6379/0"

    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "changeme-please"
    JWT_EXPIRE_MINUTES: int = 15
    JWT_REFRESH_DAYS: int = 7
    # Self-docs (/docs, /redoc, /openapi.json) are OFF by default — they
    # enumerate every route. Enable only on trusted networks / local dev.
    DOCS_ENABLED: bool = False

    # IANA timezone whose wall-clock the schedule-rule hours follow.
    # A rule set for "12:00" fires at 12:00 in THIS zone — set it to your own
    # (e.g. Asia/Tehran) so the dashboard times match reality. Beat crontabs
    # (daily reset, cleanup, …) follow it too.
    SCHEDULE_TZ: str = "UTC"
    # Grace window (minutes) for schedule-rule firing: a rule's slot stays
    # fireable this long AFTER its minute passes. If the worker/beat is down
    # at the exact scheduled minute, the slot is still posted (late) instead
    # of silently lost. 0 = legacy exact-minute behavior.
    SCHEDULE_GRACE_MINUTES: int = 5

    MEDIA_ROOT: str = "./media"
    MAX_UPLOAD_MB: int = 500
    AUTO_PROCESS_ON_UPLOAD: bool = True

    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:8080"

    IG_DEFAULT_MAX_DAILY_POSTS: int = 3
    IG_PRE_POST_DELAY_MIN: int = 30
    IG_PRE_POST_DELAY_MAX: int = 120

    @model_validator(mode="after")
    def _derive_and_validate_db_urls(self):
        """If only the async URL was customized, derive the sync one from it.

        Inside a container, a *relative* sqlite path is almost always a
        misconfiguration: it resolves against the image WORKDIR (/code), not
        the /data volume, so the app crash-loops on startup with a cryptic
        sqlalchemy "unable to open database file". Fail fast here with an
        actionable message instead. (Relative paths stay legal outside
        containers — local dev uses them on purpose.)
        """
        if self.SYNC_DATABASE_URL == DEFAULT_SYNC_DATABASE_URL and self.DATABASE_URL != DEFAULT_DATABASE_URL:
            if "+aiosqlite" in self.DATABASE_URL:
                self.SYNC_DATABASE_URL = self.DATABASE_URL.replace("+aiosqlite", "")
            elif "+asyncpg" in self.DATABASE_URL:
                self.SYNC_DATABASE_URL = self.DATABASE_URL.replace("+asyncpg", "+psycopg2")

        if _running_in_container():
            for label in ("DATABASE_URL", "SYNC_DATABASE_URL"):
                db_url = getattr(self, label)
                path = _sqlite_file_path(db_url)
                if path and path != ":memory:" and not os.path.isabs(path):
                    raise ValueError(
                        f"{label} points at a relative SQLite path {path!r}. "
                        "Inside the container this resolves against the image "
                        "WORKDIR (/code) — not the /data volume — so the database "
                        "file won't be found. Use an absolute container path "
                        "instead, e.g. "
                        "DATABASE_URL=sqlite+aiosqlite:////data/app.db "
                        "(note the 4 slashes). See .env.example."
                    )
        return self

    @model_validator(mode="after")
    def _validate_schedule_tz(self):
        """SCHEDULE_TZ must be a real IANA zone — a typo must not silently
        shift every post by hours. Fall back to UTC with a loud warning."""
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(self.SCHEDULE_TZ)
        except (ZoneInfoNotFoundError, ValueError):
            import logging

            logging.getLogger("igfunnel").warning(
                "Invalid SCHEDULE_TZ=%r — falling back to UTC", self.SCHEDULE_TZ
            )
            self.SCHEDULE_TZ = "UTC"
        return self

    @model_validator(mode="after")
    def _validate_schedule_grace(self):
        """A negative grace window is meaningless — clamp to 0 (exact-minute)."""
        if self.SCHEDULE_GRACE_MINUTES < 0:
            import logging

            logging.getLogger("igfunnel").warning(
                "SCHEDULE_GRACE_MINUTES=%r is negative — clamped to 0",
                self.SCHEDULE_GRACE_MINUTES,
            )
            self.SCHEDULE_GRACE_MINUTES = 0
        return self


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if not settings.FERNET_KEY:
        from cryptography.fernet import Fernet

        settings.FERNET_KEY = Fernet.generate_key().decode()
        import logging

        logging.getLogger(__name__).warning(
            "FERNET_KEY not set — generated an ephemeral key. "
            "Encrypted credentials will be unreadable after restart. "
            "Set FERNET_KEY in .env (see .env.example)."
        )
    return settings


settings = get_settings()
