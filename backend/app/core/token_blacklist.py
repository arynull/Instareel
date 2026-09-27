"""Refresh-token replay protection.

Refresh tokens are single-use: every successful /refresh blacklists the old
token's jti in Redis until its natural expiry. A stolen refresh token can
therefore be used at most once — and the moment the legitimate client
refreshes, the stolen copy is dead (the attacker's replay attempt then 401s,
which is also a theft signal worth alerting on).
"""
import redis.asyncio as aioredis

from app.config import settings

_PREFIX = "igfunnel:refresh_blacklist:"


def _client() -> aioredis.Redis:
    return aioredis.from_url(settings.REDIS_URL, decode_responses=True)


async def blacklist_jti(jti: str, ttl_seconds: int) -> None:
    client = _client()
    try:
        await client.setex(_PREFIX + jti, max(int(ttl_seconds), 1), "1")
    finally:
        await client.aclose()


async def claim_jti(jti: str, ttl_seconds: int) -> bool:
    """Atomically claim a refresh-token jti (Redis SET NX).

    Returns True on first use, False if already claimed. This replaces the
    old check-then-set (is_blacklisted + blacklist_jti), where two concurrent
    /refresh calls could both pass the check before either wrote the key.
    """
    client = _client()
    try:
        return bool(
            await client.set(_PREFIX + jti, "1", ex=max(int(ttl_seconds), 1), nx=True)
        )
    finally:
        await client.aclose()


async def is_blacklisted(jti: str) -> bool:
    client = _client()
    try:
        return bool(await client.exists(_PREFIX + jti))
    finally:
        await client.aclose()
