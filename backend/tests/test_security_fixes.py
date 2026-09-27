"""Regression tests for the hardening fixes (audit items 3, 4, 5, 6, 7, 8, 10).

- #8  refresh-token replay protection (rotation + Redis jti blacklist)
- #5  self-docs (/docs, /redoc, /openapi.json) gated by DOCS_ENABLED
- #4  rate limiting sees the real client IP behind nginx
- #7  websocket: heartbeat, stale-token close, no legacy ?token=
- #6  proxy purge parks the account instead of unlinking to the server IP
- #10 docker-compose healthchecks
- #3  nginx allows 500MB uploads
"""
import os
import asyncio
import datetime as dt
import json
import re
from pathlib import Path
from types import SimpleNamespace

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from starlette.websockets import WebSocketDisconnect

import app.database as database
from app.config import settings
from app.core.security import create_access_token
from app.database import Base
from app.models import Account, AccountStatus, Proxy, ProxyProtocol, SystemLog

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture()
def sync_factory(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/s.db")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SyncSessionLocal", maker)
    return maker


# --------------------------------------------------------------------------
# #8 — refresh-token replay protection
# --------------------------------------------------------------------------

@pytest.fixture()
def anon_client(tmp_path, monkeypatch):
    from app.main import create_app

    monkeypatch.setattr(settings, "MEDIA_ROOT", str(tmp_path / "media"))
    app = create_app()
    with TestClient(app) as c:
        yield c


def _login(c):
    r = c.post("/api/v1/auth/login",
               json={"username": "admin", "password": os.environ["ADMIN_PASSWORD"]})
    assert r.status_code == 200, r.text
    return r.json()


def test_refresh_tokens_carry_jti(anon_client):
    tokens = _login(anon_client)
    for kind in ("access_token", "refresh_token"):
        claims = jwt.decode(tokens[kind], options={"verify_signature": False})
        assert claims.get("jti"), f"{kind} has no jti"


def test_refresh_rotation_rejects_replay(anon_client):
    c = anon_client
    t1 = _login(c)

    r2 = c.post("/api/v1/auth/refresh", json={"refresh_token": t1["refresh_token"]})
    assert r2.status_code == 200, r2.text
    t2 = r2.json()
    assert t2["refresh_token"] != t1["refresh_token"]
    assert t2["access_token"] != t1["access_token"]

    # Replaying the old refresh token must fail (single-use).
    r3 = c.post("/api/v1/auth/refresh", json={"refresh_token": t1["refresh_token"]})
    assert r3.status_code == 401, r3.text

    # The rotated token still works.
    r4 = c.post("/api/v1/auth/refresh", json={"refresh_token": t2["refresh_token"]})
    assert r4.status_code == 200, r4.text


# --------------------------------------------------------------------------
# #5 — self-docs gated
# --------------------------------------------------------------------------

def test_docs_disabled_by_default(monkeypatch):
    from app.main import create_app

    monkeypatch.setattr(settings, "DOCS_ENABLED", False)
    app = create_app()
    assert app.docs_url is None
    assert app.redoc_url is None
    assert app.openapi_url is None
    c = TestClient(app)
    assert c.get("/docs").status_code == 404
    assert c.get("/redoc").status_code == 404
    assert c.get("/openapi.json").status_code == 404


def test_docs_enabled_flag(monkeypatch):
    from app.main import create_app

    monkeypatch.setattr(settings, "DOCS_ENABLED", True)
    app = create_app()
    assert app.docs_url == "/docs"
    c = TestClient(app)
    assert c.get("/openapi.json").status_code == 200


# --------------------------------------------------------------------------
# #4 — rate limit sees the real client IP
# --------------------------------------------------------------------------

def _limited_app():
    from fastapi import FastAPI, Request
    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded
    from slowapi.middleware import SlowAPIMiddleware
    from slowapi.util import get_remote_address

    limiter = Limiter(key_func=get_remote_address)
    inner = FastAPI()
    inner.state.limiter = limiter
    inner.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    inner.add_middleware(SlowAPIMiddleware)

    @inner.get("/t")
    @limiter.limit("1/minute")
    async def t(request: Request):
        return {"ok": True}

    return inner


def test_rate_limit_distinguishes_forwarded_ips():
    """With trusted proxy headers (uvicorn --proxy-headers), slowapi buckets
    by the real client IP — the mechanism fix #4 relies on."""
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    app = ProxyHeadersMiddleware(_limited_app(), trusted_hosts="*")
    c = TestClient(app, raise_server_exceptions=False)
    h1 = {"X-Forwarded-For": "1.2.3.4"}
    h2 = {"X-Forwarded-For": "5.6.7.8"}
    assert c.get("/t", headers=h1).status_code == 200
    assert c.get("/t", headers=h1).status_code == 429  # same IP → limited
    assert c.get("/t", headers=h2).status_code == 200  # other IP → own bucket


def test_rate_limit_broken_without_proxy_headers():
    """Documents the original bug: untrusted X-Forwarded-For is ignored, so
    every client shares one bucket."""
    c = TestClient(_limited_app(), raise_server_exceptions=False)
    assert c.get("/t", headers={"X-Forwarded-For": "1.2.3.4"}).status_code == 200
    assert c.get("/t", headers={"X-Forwarded-For": "9.9.9.9"}).status_code == 429


_NGINX_TEMPLATE = REPO / "nginx" / "nginx.conf.template"


def test_nginx_forwards_client_ip():
    text = _NGINX_TEMPLATE.read_text()
    block = re.search(r"location /api/ \{(.*?)\n    \}", text, re.S).group(1)
    assert "proxy_set_header X-Forwarded-For" in block
    assert "proxy_set_header X-Forwarded-Proto" in block


def test_compose_backend_trusts_proxy_headers():
    text = (REPO / "docker-compose.yml").read_text()
    assert "--proxy-headers" in text
    assert "--forwarded-allow-ips" in text


# --------------------------------------------------------------------------
# #7 — websocket heartbeat + stale-token handling
# --------------------------------------------------------------------------

class _FakePubSub:
    async def subscribe(self, channel):
        pass

    async def get_message(self, ignore_subscribe_messages=False, timeout=0.0):
        await asyncio.sleep(timeout or 0)
        return None


class _FakeAIORedis:
    def pubsub(self):
        return _FakePubSub()

    async def aclose(self):
        pass


@pytest.fixture()
def ws_env(monkeypatch, tmp_path):
    import app.api.system as system

    monkeypatch.setattr(system, "WS_PING_INTERVAL", 0.2)
    monkeypatch.setattr(system, "WS_PONG_TIMEOUT", 2.0)
    monkeypatch.setattr(
        system, "aioredis",
        SimpleNamespace(from_url=lambda *a, **k: _FakeAIORedis()),
    )
    monkeypatch.setattr(settings, "MEDIA_ROOT", str(tmp_path / "media"))
    return system


def _ws_app():
    from app.main import create_app

    return create_app()


def test_ws_heartbeat_ping_pong_real(ws_env):
    token = create_access_token("admin")
    with TestClient(_ws_app()) as c, c.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"token": token}))
        assert json.loads(ws.receive_text())["event"] == "connected"
        assert json.loads(ws.receive_text())["event"] == "ping"
        ws.send_text(json.dumps({"event": "pong"}))
        # Heartbeat loop keeps going after a pong.
        assert json.loads(ws.receive_text())["event"] == "ping"
        ws.close()


def test_ws_rejects_bad_token(ws_env):
    with TestClient(_ws_app()) as c, c.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"token": "bogus"}))
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_text()
        assert e.value.code == 4401


def test_ws_ignores_legacy_query_token(ws_env):
    """?token= must NOT authenticate — it used to be honored."""
    token = create_access_token("admin")
    with TestClient(_ws_app()) as c, c.websocket_connect(f"/ws?token={token}") as ws:
        ws.send_text(json.dumps({"token": ""}))  # empty first frame
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_text()
        assert e.value.code == 4401


def test_ws_closes_on_expired_token(ws_env, monkeypatch):
    """Token expiring mid-session forces a 4401 so the client reconnects
    with a fresh token (no more stale sessions)."""
    real_decode = ws_env.decode_token
    calls = {"n": 0}

    def fake_decode(tok, expected_type="access"):
        calls["n"] += 1
        if calls["n"] > 1:
            raise ValueError("Token expired")
        return real_decode(tok, expected_type=expected_type)

    monkeypatch.setattr(ws_env, "decode_token", fake_decode)
    token = create_access_token("admin")
    with TestClient(_ws_app()) as c, c.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"token": token}))
        assert json.loads(ws.receive_text())["event"] == "connected"
        assert json.loads(ws.receive_text())["event"] == "ping"
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_text()
        assert e.value.code == 4401


# --------------------------------------------------------------------------
# #6 — proxy purge parks the account
# --------------------------------------------------------------------------

def test_purge_stale_auto_proxies_parks_account(sync_factory):
    from app.tasks import sync_helpers

    now = dt.datetime.now(dt.timezone.utc)
    with sync_factory() as s:
        p = Proxy(
            url="http://9.9.9.9:8080", protocol=ProxyProtocol.http, source="pool-x",
            is_healthy=False, is_active=False,
            created_at=now - dt.timedelta(days=10),
            last_checked=now - dt.timedelta(days=10),
        )
        s.add(p)
        s.flush()
        acc = Account(username="parkme", password_enc="x", proxy_id=p.id,
                      status=AccountStatus.active)
        s.add(acc)
        s.commit()
        pid, aid = p.id, acc.id

    with sync_factory() as s:
        assert sync_helpers.purge_stale_auto_proxies(s) == 1

    with sync_factory() as s:
        acc = s.get(Account, aid)
        # Parked (disabled) — never silently posting from the server IP.
        assert acc.status == AccountStatus.disabled
        assert acc.proxy_id is None
        assert "Auto-parked" in (acc.notes or "")
        assert s.get(Proxy, pid) is None
        logs = s.execute(
            select(SystemLog).where(SystemLog.category == "proxy")
        ).scalars().all()
        assert any("auto-parked" in (l.message or "") for l in logs)


def test_cap_auto_pool_parks_affected_accounts(sync_factory):
    from app.tasks import sync_helpers

    now = dt.datetime.now(dt.timezone.utc)
    with sync_factory() as s:
        proxies = []
        for i, fails in enumerate((9, 1, 0)):
            p = Proxy(
                url=f"http://10.0.0.{i}:8080", protocol=ProxyProtocol.http,
                source="pool-x", is_healthy=False, is_active=False,
                fail_count=fails, created_at=now - dt.timedelta(days=10),
                last_checked=now - dt.timedelta(days=10),
            )
            s.add(p)
            proxies.append(p)
        s.flush()
        acc = Account(username="capme", password_enc="x",
                      proxy_id=proxies[0].id, status=AccountStatus.active)
        s.add(acc)
        s.commit()
        aid = acc.id

    with sync_factory() as s:
        assert sync_helpers.cap_auto_pool(s, max_auto=2) == 1

    with sync_factory() as s:
        acc = s.get(Account, aid)
        assert acc.status == AccountStatus.disabled
        assert acc.proxy_id is None


# --------------------------------------------------------------------------
# #10 — compose healthchecks
# --------------------------------------------------------------------------

def _compose_service_blocks():
    text = (REPO / "docker-compose.yml").read_text()
    blocks, current = {}, None
    for line in text.splitlines():
        m = re.match(r"^  ([a-z_][a-z0-9_-]*):\s*$", line)
        if m:
            current = m.group(1)
            blocks[current] = []
        elif current is not None:
            blocks[current].append(line)
    return {k: "\n".join(v) for k, v in blocks.items()}


def test_compose_healthchecks_present():
    blocks = _compose_service_blocks()
    for svc in ("backend", "worker-fast", "worker-slow", "beat", "frontend"):
        assert "healthcheck:" in blocks[svc], f"{svc} has no healthcheck"
    assert "/health" in blocks["backend"]
    # Each lane proves its own consumer is alive via its per-lane alive key —
    # inspect ping flaps under the solo pool, so the healthcheck no longer
    # uses it.
    assert "own_lane_alive" in blocks["worker-fast"]
    assert "own_lane_alive" in blocks["worker-slow"]
    assert "service_healthy" in blocks["nginx"]  # waits for healthy upstreams


# --------------------------------------------------------------------------
# #3 — nginx allows 500MB uploads
# --------------------------------------------------------------------------

def test_nginx_allows_large_uploads():
    # The limit is rendered from the environment (NGINX_ENVSUBST_FILTER), so
    # the template holds a variable — assert the variable AND its compose
    # default are both sane (>= 500m for the 500MB backend upload cap).
    text = _NGINX_TEMPLATE.read_text()
    assert re.search(r"client_max_body_size\s+\$\{CLIENT_MAX_BODY_SIZE\}", text), \
        "client_max_body_size must come from CLIENT_MAX_BODY_SIZE"
    compose = (REPO / "docker-compose.yml").read_text()
    m = re.search(r"CLIENT_MAX_BODY_SIZE:\s*\$\{CLIENT_MAX_BODY_SIZE:-(\d+)m\}", compose)
    assert m is not None, "CLIENT_MAX_BODY_SIZE default not set in compose"
    assert int(m.group(1)) >= 500


# --------------------------------------------------------------------------
# M2/M3 — startup refuses default/empty auth secrets (fail closed)
# --------------------------------------------------------------------------

def test_validate_security_rejects_placeholders():
    from app.config import Settings

    bad = Settings()
    bad.SECRET_KEY = "change-me"
    bad.ADMIN_PASSWORD = "changeme-please"
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        bad.validate_security()

    bad2 = Settings()
    bad2.SECRET_KEY = "a" * 64
    bad2.ADMIN_PASSWORD = ""
    with pytest.raises(RuntimeError, match="ADMIN_PASSWORD"):
        bad2.validate_security()

    good = Settings()
    good.SECRET_KEY = "x" * 64
    good.ADMIN_PASSWORD = "a-real-password"
    good.ADMIN_USERNAME = "root"
    good.validate_security()  # must not raise


def test_startup_refuses_default_secret_key(monkeypatch):
    import app.main as main_module
    from app.config import settings

    monkeypatch.setattr(settings, "SECRET_KEY", "change-me")
    app = main_module.create_app()
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        with TestClient(app):
            pass  # pragma: no cover


def test_empty_credentials_rejected(anon_client):
    assert anon_client.post(
        "/api/v1/auth/login", json={"username": "", "password": ""}
    ).status_code == 401
    assert anon_client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": ""},
    ).status_code == 401


# --------------------------------------------------------------------------
# Refresh-token claim is atomic (SET NX) — no check-then-set race
# --------------------------------------------------------------------------

def test_claim_jti_is_single_use():
    import asyncio

    from app.core import token_blacklist

    async def go():
        first = await token_blacklist.claim_jti("race-jti-1", 60)
        second = await token_blacklist.claim_jti("race-jti-1", 60)
        return first, second

    first, second = asyncio.run(go())
    assert first is True
    assert second is False


def test_refresh_replay_rejected(anon_client):
    tokens = _login(anon_client)
    r1 = anon_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert r1.status_code == 200
    # Replaying the same refresh token must 401 (it was claimed on first use).
    r2 = anon_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert r2.status_code == 401
