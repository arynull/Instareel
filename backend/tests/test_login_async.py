"""Async (202 + poll) Instagram login.

The old POST /accounts/{id}/login blocked the event loop for up to 180s
with ThreadPoolExecutor.result() — with a single uvicorn worker that froze
the whole backend, so the dashboard showed "Load failed" on unrelated
requests while a login was in flight. /login now returns 202 immediately
and runs in a daemon thread; the dashboard polls GET /login-status.
"""
import threading
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

import app.api.accounts as accounts_api
from app.api.deps import get_current_admin, get_db
from app.config import settings
from app.database import Base
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import asyncio

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def override_db():
        async with maker() as s:
            yield s

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    def _run(coro):
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = None
        if loop is None or loop.is_closed():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)

    _run(_create())
    monkeypatch.setattr(settings, "MEDIA_ROOT", str(tmp_path / "media"))
    # The login worker thread uses the sync session factory — point it at
    # the same tmp database file so tests observe its writes.
    sync_maker = sessionmaker(create_engine(f"sqlite:///{tmp_path}/t.db"), expire_on_commit=False)
    monkeypatch.setattr("app.database.SyncSessionLocal", sync_maker)
    app.dependency_overrides[get_current_admin] = lambda: "admin"
    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _clean_login_tasks():
    accounts_api._login_tasks.clear()
    yield
    accounts_api._login_tasks.clear()


def _make_account(c, username="iguser"):
    r = c.post(
        "/api/v1/accounts",
        json={"username": username, "password": "pw", "max_daily_posts": 2},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _wait_terminal(c, aid, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = c.get(f"/api/v1/accounts/{aid}/login-status")
        assert r.status_code == 200, r.text
        body = r.json()
        if body["status"] in ("ok", "failed"):
            return body
        time.sleep(0.05)
    raise AssertionError("login task did not reach a terminal state in time")


class TestAsyncLogin:
    def test_login_accepted_and_completes(self, client, monkeypatch):
        c = client
        aid = _make_account(c)
        monkeypatch.setattr(accounts_api, "_blocking_login", lambda *a: (True, "logged_in"))

        r = c.post(f"/api/v1/accounts/{aid}/login")
        assert r.status_code == 202, r.text
        assert r.json()["status"] == "running"

        body = _wait_terminal(c, aid)
        assert body["status"] == "ok"
        assert body["detail"] == "logged_in"

        acc = c.get(f"/api/v1/accounts/{aid}").json()
        assert acc["status"] == "active"
        assert acc["last_login"] is not None

    def test_concurrent_login_second_gets_409(self, client, monkeypatch):
        c = client
        aid = _make_account(c)
        gate = threading.Event()

        def slow(*a):
            assert gate.wait(timeout=20)
            return True, "logged_in"

        monkeypatch.setattr(accounts_api, "_blocking_login", slow)
        assert c.post(f"/api/v1/accounts/{aid}/login").status_code == 202
        r2 = c.post(f"/api/v1/accounts/{aid}/login")
        assert r2.status_code == 409, r2.text
        gate.set()
        assert _wait_terminal(c, aid)["status"] == "ok"

    def test_login_failure_records_failed_and_challenge_status(self, client, monkeypatch):
        c = client
        aid = _make_account(c)
        monkeypatch.setattr(
            accounts_api, "_blocking_login", lambda *a: (False, "challenge: verification required")
        )
        assert c.post(f"/api/v1/accounts/{aid}/login").status_code == 202
        body = _wait_terminal(c, aid)
        assert body["status"] == "failed"
        assert "challenge" in body["detail"]
        assert c.get(f"/api/v1/accounts/{aid}").json()["status"] == "challenge_required"

    def test_login_unknown_account_404(self, client):
        assert client.post("/api/v1/accounts/999999/login").status_code == 404

    def test_login_status_unknown_is_404(self, client):
        c = client
        aid = _make_account(c)
        assert c.get(f"/api/v1/accounts/{aid}/login-status").status_code == 404

    def test_stale_running_heals_and_allows_retry(self, client, monkeypatch):
        c = client
        aid = _make_account(c)
        accounts_api._login_tasks[aid] = {
            "status": "running",
            "detail": "Login in progress…",
            "started_at": time.monotonic() - (accounts_api.LOGIN_TASK_STALE_S + 10),
            "finished_at": None,
        }
        # A dead "running" entry (server restarted mid-login) heals instead
        # of wedging the account behind a permanent 409.
        r = c.get(f"/api/v1/accounts/{aid}/login-status")
        assert r.status_code == 200
        assert r.json()["status"] == "failed"

        monkeypatch.setattr(accounts_api, "_blocking_login", lambda *a: (True, "logged_in"))
        assert c.post(f"/api/v1/accounts/{aid}/login").status_code == 202
        assert _wait_terminal(c, aid)["status"] == "ok"

    def test_worker_crash_still_finishes_task(self, client, monkeypatch):
        c = client
        aid = _make_account(c)

        def boom(*a):
            raise RuntimeError("nope")

        monkeypatch.setattr(accounts_api, "_blocking_login", boom)
        assert c.post(f"/api/v1/accounts/{aid}/login").status_code == 202
        body = _wait_terminal(c, aid)
        assert body["status"] == "failed"
        assert "nope" in body["detail"]


class TestSessionCheckContract:
    def test_invalid_and_valid(self, client, monkeypatch):
        from app.services.instagram_service import InstagramService

        c = client
        aid = _make_account(c)
        monkeypatch.setattr(
            InstagramService, "check_session", lambda self, u: (False, "login_required: nope")
        )
        r = c.post(f"/api/v1/accounts/{aid}/test-session")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["valid"] is False
        assert body["detail"].startswith("Session invalid —")

        monkeypatch.setattr(InstagramService, "check_session", lambda self, u: (True, "ok"))
        r = c.post(f"/api/v1/accounts/{aid}/test-session")
        assert r.status_code == 200, r.text
        assert r.json() == {"valid": True, "detail": "Session is valid"}
