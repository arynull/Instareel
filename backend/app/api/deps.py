"""Shared dependencies: DB session, JWT auth guard, rate limiting."""
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.security import decode_token
from app.database import get_db

import os

# Default ceiling for every route (per-endpoint @limiter.limit decorators
# stack on top of this and stay the binding constraint where stricter).
# Without it, only the ~20 decorated endpoints were limited at all.
# RATE_LIMIT_DEFAULT="" disables the global default (the test suite does
# this — it fires hundreds of requests from one TestClient IP).
_default_limit = os.getenv("RATE_LIMIT_DEFAULT", "200/minute")
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[_default_limit] if _default_limit else [],
)
bearer = HTTPBearer(auto_error=False)


async def get_current_admin(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> str:
    # RFC 6750: 401s from a Bearer <redacted> carry WWW-Authenticate.
    headers = {"WWW-Authenticate": "Bearer"}
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers=headers,
        )
    try:
        return decode_token(credentials.credentials, expected_type="access")
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc), headers=headers
        ) from exc


def admin_username() -> str:
    return settings.ADMIN_USERNAME

__all__ = ["get_db", "get_current_admin", "limiter", "admin_username"]
