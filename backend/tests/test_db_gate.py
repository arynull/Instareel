"""Tests for the startup database-version gate (app/core/db_gate.py).

The gate is the Level-1 hardening against the recurring 0018/0019 incident
pattern: new code booted on an unmigrated database, dying with
``no such column`` while nothing alerted. Every scenario below uses a real
temporary SQLite file and the REAL alembic versions directory, so the tests
prove the gate against the actual migration history — not a mock of it.
"""
import pytest
from sqlalchemy import create_engine, text

from app.core import db_gate
from app.core.db_gate import (
    DatabaseVersionError,
    assert_db_at_head,
    code_heads,
    db_heads,
    db_is_empty,
    stamp_head,
)


@pytest.fixture()
def db_url(tmp_path):
    return f"sqlite:///{tmp_path}/gate.db"


@pytest.fixture()
def engine(db_url):
    eng = create_engine(db_url)
    yield eng
    eng.dispose()


def _stamp(engine, rev):
    """Record an arbitrary revision directly (simulates a DB at that state)."""
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)"))
        conn.execute(text("DELETE FROM alembic_version"))
        conn.execute(text("INSERT INTO alembic_version (version_num) VALUES (:v)"), {"v": rev})


def test_code_heads_match_latest_migration():
    heads = code_heads()
    assert heads == {"0019_post_rule_id"}


def test_empty_db_detected(engine):
    assert db_is_empty(engine) is True


def test_nonempty_db_not_empty(engine):
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE probe (id INTEGER PRIMARY KEY)"))
    assert db_is_empty(engine) is False


def test_stamp_head_then_assert_passes(engine, db_url):
    stamp_head(engine)
    assert db_heads(engine) == {"0019_post_rule_id"}
    assert_db_at_head(engine, db_url)  # must not raise


def test_behind_db_raises_with_recovery_command(engine, db_url):
    _stamp(engine, "0018_post_dispatched_at")
    with pytest.raises(DatabaseVersionError) as ei:
        assert_db_at_head(engine, db_url)
    msg = str(ei.value)
    assert "BEHIND" in msg
    assert "0018_post_dispatched_at" in msg
    assert "0019_post_rule_id" in msg
    assert "alembic upgrade head" in msg


def test_missing_version_table_raises(engine, db_url):
    # Legacy drift: tables exist (e.g. from an old create_all run) but no
    # alembic_version — the gate must refuse, not silently proceed.
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE accounts (id INTEGER PRIMARY KEY)"))
    with pytest.raises(DatabaseVersionError) as ei:
        assert_db_at_head(engine, db_url)
    assert "alembic_version" in str(ei.value)
    assert "alembic stamp head" in str(ei.value)


def test_db_ahead_of_code_raises(engine, db_url):
    # Code rolled back below the DB: also a mismatch, also refuses to boot.
    _stamp(engine, "9999_future_revision")
    with pytest.raises(DatabaseVersionError) as ei:
        assert_db_at_head(engine, db_url)
    assert "AHEAD" in str(ei.value)
    assert "9999_future_revision" in str(ei.value)


def test_password_redacted_from_error():
    url = "postgresql+psycopg2://igfunnel:s3cret@postgres:5432/igfunnel"
    redacted = db_gate._redact_url(url)
    assert "s3cret" not in redacted
    assert "***" in redacted
    assert "postgres:5432/igfunnel" in redacted
