"""Startup database-version gate: fail fast when the DB schema lags the code.

Recurring incident pattern (migrations 0018, 0019): the stack was rebuilt
with new code but ``alembic upgrade head`` was skipped — or ran inside the
OLD backend container, where the new migration file did not exist yet, so it
silently no-op'd. The new code then hit ``no such column: ...`` on every
scheduler tick while the watchdog saw nothing wrong (it only watches gaps
between runs, never in-tick errors). The admin learned about it from missing
posts, hours later.

This gate makes that state impossible to run in: during API startup it
compares the alembic revisions recorded in the database against the heads
known to the deployed code and refuses to boot when they differ, logging an
actionable recovery command. A loudly-down backend at deploy time beats
silently-lost posts every time.

Fresh databases (no tables at all) are stamped ``head`` after ``create_all``
— the schema then equals the code's metadata by construction, and stamping
avoids re-running 19 migrations on every fresh install. (Alembic's own
``command.upgrade`` can't be used here: ``env.py`` calls ``asyncio.run()``,
which raises inside the already-running lifespan event loop.)

Bypass (emergencies ONLY — you are opting back into silent schema drift):
    SKIP_DB_VERSION_CHECK=1
"""
from __future__ import annotations

import logging
from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

log = logging.getLogger("igfunnel.db_gate")

ALEMBIC_DIR = Path(__file__).resolve().parent.parent.parent / "alembic"


class DatabaseVersionError(RuntimeError):
    """Raised at startup when the DB schema does not match the code."""


def _alembic_config(sync_db_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", sync_db_url)
    return cfg


def _script() -> ScriptDirectory:
    return ScriptDirectory.from_config(_alembic_config(""))


def code_heads() -> set[str]:
    """Revision ids the deployed code considers current (alembic heads)."""
    return set(_script().get_heads())


def _known_revisions() -> set[str]:
    """Every revision id the code knows: heads plus all their ancestors."""
    script = _script()
    known: set[str] = set()
    for head in script.get_heads():
        known.add(head)
        for rev in script.walk_revisions(base="base", head=head):
            known.add(rev.revision)
    return known


def db_heads(engine: Engine) -> set[str]:
    """Revision ids recorded in the database's alembic_version table."""
    with engine.connect() as conn:
        if not inspect(conn).has_table("alembic_version"):
            return set()
        ctx = MigrationContext.configure(conn)
        return set(ctx.get_current_heads())


def db_is_empty(engine: Engine) -> bool:
    """True when the database has no user tables at all (fresh install)."""
    with engine.connect() as conn:
        tables = inspect(conn).get_table_names()
    return not [t for t in tables if not t.startswith("sqlite_")]


def stamp_head(engine: Engine) -> None:
    """Record ``head`` in a fresh database whose schema was just created
    from the code's metadata (so schema == head by construction).

    Done with plain SQL on the caller's engine — deliberately NOT via
    ``alembic.command.stamp``: that path runs ``env.py``, which overwrites
    the URL with ``settings.DATABASE_URL`` (the async URL) and would stamp
    the wrong database (or fail outside the app's configured environment).
    """
    from sqlalchemy import text

    heads = sorted(code_heads())
    with engine.begin() as conn:
        conn.execute(
            text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        conn.execute(text("DELETE FROM alembic_version"))
        for rev in heads:
            conn.execute(
                text("INSERT INTO alembic_version (version_num) VALUES (:v)"), {"v": rev}
            )
    log.info("db_gate: stamped fresh database at head %s", heads)


def assert_db_at_head(engine: Engine, sync_db_url: str) -> None:
    """Fail fast when the existing database is not at the code's head.

    Raises:
        DatabaseVersionError: with the exact recovery command to run.
    """
    heads = code_heads()
    current = db_heads(engine)

    db_label = _redact_url(sync_db_url)
    if not current:
        raise DatabaseVersionError(
            "Database schema version unknown — refusing to start.\n"
            f"  Database: {db_label}\n"
            "  The database has tables but no alembic_version table, so its\n"
            "  schema cannot be verified against the code. This usually means\n"
            "  it was created by an old create_all run (pre-migration era).\n"
            "  Verify the schema matches this code, then record its version:\n"
            "    docker compose run --rm backend alembic stamp head\n"
            "  (Bypass ONLY for emergencies: SKIP_DB_VERSION_CHECK=1)"
        )
    # Revisions the code has never heard of (neither heads nor ancestors):
    # the code was rolled back below the DB. Also refuses to boot.
    if unknown := sorted(set(current) - _known_revisions()):
        raise DatabaseVersionError(
            "Database schema is AHEAD of the code — refusing to start.\n"
            f"  Database: {db_label}\n"
            f"  Database is at:  {sorted(current)}\n"
            f"  Code expects:    {sorted(heads)}\n"
            f"  Unknown revisions: {unknown}\n"
            "  Either deploy the matching code or downgrade the database:\n"
            "    docker compose run --rm backend alembic downgrade <rev>\n"
            "  (Bypass ONLY for emergencies: SKIP_DB_VERSION_CHECK=1)"
        )
    if missing := sorted(heads - current):
        raise DatabaseVersionError(
            "Database schema is BEHIND the code — refusing to start.\n"
            f"  Database: {db_label}\n"
            f"  Database is at:  {sorted(current)}\n"
            f"  Code expects:    {sorted(heads)}\n"
            f"  Missing revisions: {missing}\n"
            "  The code was deployed without its migration — this is exactly\n"
            "  how scheduler ticks died with 'no such column' in the past.\n"
            "  Back up and migrate in a one-off backend container, then restart:\n"
            "    ./scripts/migrate.sh\n"
            "  (Raw form — only if you already have a fresh backup:\n"
            "    docker compose run --rm backend alembic upgrade head)\n"
            "  (Bypass ONLY for emergencies: SKIP_DB_VERSION_CHECK=1)"
        )
    log.info("db_gate: database at head %s", sorted(current))


def _redact_url(url: str) -> str:
    """Strip any password from a DB URL before it lands in logs/errors."""
    try:
        from sqlalchemy.engine import make_url

        u = make_url(url)
        if u.password:
            u = u.set(password="***")
        return str(u)
    except Exception:
        return "<unparseable url>"
