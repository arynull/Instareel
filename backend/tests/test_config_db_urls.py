"""Fail-fast check: relative SQLite paths are rejected inside containers.

A relative DATABASE_URL (e.g. sqlite+aiosqlite:///./data/app.db) resolves
against the image WORKDIR (/code) instead of the /data volume, which used to
crash the backend on startup with a cryptic sqlalchemy "unable to open
database file". Settings validation now raises an actionable error instead.
"""
import pytest
from pydantic import ValidationError

import app.config as config_module
from app.config import Settings


@pytest.fixture()
def in_container(monkeypatch):
    monkeypatch.setattr(config_module, "_running_in_container", lambda: True)


@pytest.fixture()
def not_in_container(monkeypatch):
    monkeypatch.setattr(config_module, "_running_in_container", lambda: False)


def test_relative_sqlite_url_rejected_in_container(in_container):
    with pytest.raises(ValidationError, match="relative SQLite path"):
        Settings(DATABASE_URL="sqlite+aiosqlite:///./data/app.db")


def test_relative_sync_url_rejected_in_container(in_container):
    # NB: must differ from DEFAULT_SYNC_DATABASE_URL, otherwise the validator
    # derives the sync URL from DATABASE_URL (pre-existing behavior).
    with pytest.raises(ValidationError, match="SYNC_DATABASE_URL"):
        Settings(
            DATABASE_URL="sqlite+aiosqlite:////data/app.db",
            SYNC_DATABASE_URL="sqlite:///./custom.db",
        )


def test_absolute_sqlite_url_ok_in_container(in_container):
    s = Settings(DATABASE_URL="sqlite+aiosqlite:////data/app.db")
    assert s.DATABASE_URL == "sqlite+aiosqlite:////data/app.db"


def test_relative_sqlite_url_ok_outside_container(not_in_container):
    # Local dev intentionally uses relative paths.
    s = Settings(DATABASE_URL="sqlite+aiosqlite:///./data/app.db")
    assert s.DATABASE_URL == "sqlite+aiosqlite:///./data/app.db"


def test_memory_db_ok_in_container(in_container):
    s = Settings(DATABASE_URL="sqlite+aiosqlite:///:memory:")
    assert s.DATABASE_URL == "sqlite+aiosqlite:///:memory:"


def test_postgres_url_untouched_in_container(in_container):
    s = Settings(
        DATABASE_URL="postgresql+asyncpg://igfunnel:changeme@postgres:5432/igfunnel",
        SYNC_DATABASE_URL="postgresql+psycopg2://igfunnel:changeme@postgres:5432/igfunnel",
    )
    assert "+asyncpg" in s.DATABASE_URL


def test_sync_url_still_derived_from_absolute_async_url(in_container, monkeypatch):
    # conftest sets SYNC_DATABASE_URL in the env — drop it so the default
    # (and therefore the derivation branch) applies.
    monkeypatch.delenv("SYNC_DATABASE_URL", raising=False)
    s = Settings(DATABASE_URL="sqlite+aiosqlite:////data/app.db")
    assert s.SYNC_DATABASE_URL == "sqlite:////data/app.db"


def test_effective_config_labels_strip_secrets():
    from app.main import _safe_db_label, _safe_redis_label

    assert _safe_db_label("postgresql+asyncpg://user:s3cret@dbhost:5432/igfunnel") == \
        "postgresql+asyncpg://dbhost:5432/igfunnel"
    assert _safe_db_label("sqlite+aiosqlite:////data/app.db") == "sqlite://data/app.db"
    assert "s3cret" not in _safe_db_label("postgresql://u:s3cret@h/db")
    assert _safe_redis_label("redis://:redispass@redis:6379/0") == "redis:6379/0"
    assert "redispass" not in _safe_redis_label("redis://:redispass@localhost:6379/0")
