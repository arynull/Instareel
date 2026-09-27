"""Async engine for the web API + sync engine for Celery workers.

Celery tasks are fully synchronous and must NEVER create an event loop
(via asyncio.run) — they use SyncSessionLocal below.
"""
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


def _sqlite_pragmas(dbapi_conn, _connection_record):
    """Harden every SQLite connection (M4/M5).

    - M4: foreign_keys=ON — SQLite parses FK constraints but does NOT
      enforce them by default; without this, deletes silently orphan rows.
    - M5: WAL + busy timeout — backend, worker and beat all write to the
      same file. WAL lets readers proceed during a write; the busy timeout
      makes a writer wait for the lock instead of failing instantly with
      "database is locked".
    Both PRAGMAs are idempotent, so running them per connection is safe.
    """
    cur = dbapi_conn.cursor()
    try:
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
    finally:
        cur.close()


connect_args = {}
if settings.DATABASE_URL.startswith("sqlite"):
    # timeout: sqlite3 busy-wait on connect, matching the 30s PRAGMA
    # busy_timeout below (the sync engine already had this; the async one
    # didn't — three processes share one SQLite file).
    connect_args = {"check_same_thread": False, "timeout": 30}

engine = create_async_engine(settings.DATABASE_URL, echo=False, connect_args=connect_args)
if settings.DATABASE_URL.startswith("sqlite"):
    event.listen(engine.sync_engine, "connect", _sqlite_pragmas)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

sync_connect_args: dict = {}
if settings.SYNC_DATABASE_URL.startswith("sqlite"):
    sync_connect_args = {"check_same_thread": False, "timeout": 30}

sync_engine = create_engine(settings.SYNC_DATABASE_URL, echo=False, connect_args=sync_connect_args)
if settings.SYNC_DATABASE_URL.startswith("sqlite"):
    event.listen(sync_engine, "connect", _sqlite_pragmas)
SyncSessionLocal = sessionmaker(bind=sync_engine, autocommit=False, autoflush=False)


async def get_db():
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@contextmanager
def get_sync_db():
    """Sync session helper for Celery tasks and scripts (commit/rollback included)."""
    with SyncSessionLocal() as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
