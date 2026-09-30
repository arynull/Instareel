"""Lifespan wiring tests for the startup schema gate (level 1).

test_db_gate.py covers the gate logic hermetically; these tests prove the
real `lifespan` in app.main calls it correctly:
  * a boot on an empty DB creates the schema and stamps head (no raise),
  * a subsequent boot passes the gate (no raise),
  * a boot on a behind DB fails LOUDLY instead of serving requests.

TestClient runs the lifespan in a portal thread, and in-memory SQLite is
per-thread — so the gate's sync engine is pointed at a tmp FILE database
for these tests (production uses a file/PG URL too; only the async engine
stays in-memory, which the gate never inspects).
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

import app.main as main_module
from app.config import settings
from app.core.db_gate import assert_db_at_head, code_heads
from app.main import app


@pytest.fixture()
def file_sync_engine(tmp_path, monkeypatch):
    eng = create_engine(f"sqlite:///{tmp_path}/gate.db")
    monkeypatch.setattr(main_module, "sync_engine", eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def media_root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "MEDIA_ROOT", str(tmp_path / "media"))
    return tmp_path


def _boot():
    with TestClient(app):
        pass


def test_lifespan_boots_and_leaves_db_at_head(media_root, file_sync_engine):
    _boot()  # fresh DB: create_all + stamp head, must not raise
    _boot()  # second boot: gate must pass, must not raise
    assert_db_at_head(file_sync_engine, settings.SYNC_DATABASE_URL)


def test_lifespan_refuses_behind_db_loudly(media_root, file_sync_engine):
    _boot()  # ensure the stamp row exists
    behind = "0018_post_dispatched_at"  # parent of the current head
    heads = code_heads()
    assert behind not in heads and len(heads) == 1
    with file_sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE alembic_version SET version_num=:v"), {"v": behind}
        )
    with pytest.raises(Exception, match="BEHIND"):
        _boot()


def test_lifespan_bypass_boots_despite_behind_db(media_root, file_sync_engine, monkeypatch):
    """SKIP_DB_VERSION_CHECK=1 is the emergency escape hatch: the app boots
    (legacy behavior) even on a behind DB. Must stay an explicit opt-in."""
    _boot()
    with file_sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE alembic_version SET version_num=:v"),
            {"v": "0018_post_dispatched_at"},
        )
    monkeypatch.setattr(settings, "SKIP_DB_VERSION_CHECK", True)
    _boot()  # must not raise
