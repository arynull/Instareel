"""Admin login — credentials come from .env (single admin, never in DB)."""
import hmac

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.api.deps import limiter
from app.config import settings
from app.core.security import (
    create_access_token,
    create_refresh_token,
    get_token_claims,
    refresh_token_ttl,
)
from app.core.token_blacklist import claim_jti
from app.schemas.auth import LoginIn, MeOut, RefreshIn, TokenOut
from app.services.log_service import log_event

router = APIRouter()


@router.post("/login", response_model=TokenOut)
@limiter.limit("5/minute")
async def login(request: Request, body: LoginIn):
    # Constant-time compares (no early-exit `==` leaking prefix length), and
    # empty credentials are rejected outright — startup already refuses empty
    # configured values, so "" can never be valid (defense in depth).
    ok_user = bool(body.username) and hmac.compare_digest(body.username, settings.ADMIN_USERNAME)
    # ADMIN_PASSWORD is stored in plaintext in .env; compare safely (allow bcrypt hash too).
    stored = settings.ADMIN_PASSWORD
    ok_pass = bool(body.password) and (
        hmac.compare_digest(body.password, stored)
        or (stored.startswith("$2") and bcrypt.checkpw(body.password.encode(), stored.encode()))
    )
    if not (ok_user and ok_pass):
        await log_event("WARNING", "auth", f"Failed login attempt for '{body.username}'")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    await log_event("INFO", "auth", f"Admin '{body.username}' logged in")
    return TokenOut(
        access_token=create_access_token(body.username),
        refresh_token=create_refresh_token(body.username),
    )


@router.post("/refresh", response_model=TokenOut)
@limiter.limit("10/minute")
async def refresh(request: Request, body: RefreshIn):
    # Single-use refresh tokens: the presented token's jti is claimed atomically
    # (Redis SET NX) before the new pair is issued, so a replayed (stolen)
    # token 401s — even when two /refresh calls race, only one wins the claim.
    try:
        claims = get_token_claims(body.refresh_token, expected_type="refresh")
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    jti = claims.get("jti")
    if not jti:
        # Token minted before jti existed — force a fresh login once.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Please log in again")
    try:
        first_use = await claim_jti(jti, refresh_token_ttl(claims))
    except Exception as exc:
        # Fail closed: without the claim we cannot guarantee single-use.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth store unavailable, try again shortly",
        ) from exc
    if not first_use:
        await log_event(
            "WARNING", "auth", "Replayed refresh token rejected — possible token theft"
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token already used"
        )
    subject = str(claims["sub"])
    await log_event("INFO", "auth", f"Token refreshed for '{subject}'")
    return TokenOut(
        access_token=create_access_token(subject),
        refresh_token=create_refresh_token(subject),
    )


@router.get("/me", response_model=MeOut)
async def me(username: str = Depends(__import__("app.api.deps", fromlist=["get_current_admin"]).get_current_admin)):
    return MeOut(username=username)
