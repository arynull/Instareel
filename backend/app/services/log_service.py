"""DB-backed audit log helper (also mirrors to stdlib logging)."""
import logging
from typing import Any

from app.database import SessionLocal
from app.models import LogLevel, SystemLog

log = logging.getLogger("igfunnel")


async def log_event(level: str, category: str, message: str, details: dict[str, Any] | None = None) -> None:
    try:
        lvl = LogLevel[level.upper()]
    except KeyError:
        lvl = LogLevel.INFO
    getattr(log, lvl.name.lower(), log.info)("[%s] %s", category, message)
    try:
        async with SessionLocal() as session:
            session.add(SystemLog(level=lvl, category=category, message=message, details=details))
            await session.commit()
        # Push AFTER commit so a client refetch always sees the row.
        # Best-effort: publish() swallows all errors, never breaks the API.
        from app.services.realtime import publish

        await publish("new_log", {"level": lvl.name, "category": category})
    except Exception:
        log.exception("Failed to persist system log")
