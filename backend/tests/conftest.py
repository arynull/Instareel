"""Shared test bootstrap: env defaults BEFORE any app import.

Values match the historical inline setup in test_helpers.py so behavior is
identical; centralizing here keeps new test modules hermetic.
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("FERNET_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite://")
os.environ.setdefault("SYNC_DATABASE_URL", "sqlite://")
# Startup refuses default/empty auth secrets (M2/M3) — the suite runs with
# fixed test values so validate_security() never trips here.
os.environ.setdefault("SECRET_KEY", "test-suite-only-secret-key-not-for-production")
os.environ.setdefault("ADMIN_PASSWORD", "test-suite-only-admin-password")
# The global rate-limit default (200/min per IP in production) would trip on
# the suite's request volume from a single TestClient IP; per-endpoint limits
# (e.g. login brute-force) stay active and are still tested.
os.environ.setdefault("RATE_LIMIT_DEFAULT", "")


class _FakeRedis:
    """In-memory stand-in for the refresh-token jti blacklist.

    Production uses real Redis; the test suite is hermetic by design, so this
    autouse fixture swaps the client factory. Same semantics (setex TTL,
    exists), minus eviction of nothing — TTLs are honored.
    """

    def __init__(self):
        self._store: dict[str, tuple[str, float]] = {}

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self._store[key] = (value, time.time() + ttl)

    async def set(self, key: str, value: str, ex: int | None = None, nx: bool = False) -> bool:
        """SET with EX/NX — mirrors redis-py for claim_jti's atomic claim."""
        if nx:
            item = self._store.get(key)
            if item is not None:
                _, exp = item
                if exp >= time.time():
                    return False
        self._store[key] = (value, time.time() + (ex or 0))
        return True

    async def exists(self, key: str) -> int:
        item = self._store.get(key)
        if item is None:
            return 0
        _, exp = item
        if exp < time.time():
            del self._store[key]
            return 0
        return 1

    async def aclose(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _reset_rate_limit_buckets():
    """Isolate slowapi's in-memory buckets per test.

    The suite fires hundreds of requests from one TestClient IP; without a
    reset, the per-route login limit (5/min, a production brute-force guard)
    leaks across tests and makes unrelated logins flake with 429.
    Within a single test the limits still apply (test_login_brute_force_trips_429
    relies on that).
    """
    yield
    from app.api.deps import limiter

    try:
        limiter._storage.reset()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _fake_token_blacklist_redis(monkeypatch):
    from app.core import token_blacklist

    fake = _FakeRedis()
    monkeypatch.setattr(token_blacklist, "_client", lambda: fake)
    return fake

