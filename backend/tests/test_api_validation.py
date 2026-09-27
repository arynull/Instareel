"""P1 API validation tests: 422 on zero/negative limits, partial-PUT
non-clobbering, IG username charset enforcement."""
import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.deps import get_current_admin, get_db
from app.config import settings
from app.database import Base
from app.main import app


def _run(coro):
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = None
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def override_db():
        async with maker() as s:
            yield s

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    _run(_create())
    monkeypatch.setattr(settings, "MEDIA_ROOT", str(tmp_path / "media"))
    monkeypatch.setattr(settings, "AUTO_PROCESS_ON_UPLOAD", False)
    app.dependency_overrides[get_current_admin] = lambda: "admin"
    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# limit=0 / negative -> 422 everywhere a limit param exists
# ---------------------------------------------------------------------------

_LIMIT_URLS = [
    "/api/v1/videos?limit={v}",
    "/api/v1/posts?limit={v}",
    "/api/v1/notifications?limit={v}",
    "/api/v1/analytics/posts?limit={v}",
    "/api/v1/sources/{sid}/items?limit={v}",
]


def _make_source(c):
    r = c.post("/api/v1/sources", json={"username": "somepage"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.mark.parametrize("v", [0, -1, -100])
def test_zero_negative_limits_422(client, v):
    sid = _make_source(client)
    for tpl in _LIMIT_URLS:
        url = tpl.format(v=v, sid=sid)
        r = client.get(url)
        assert r.status_code == 422, f"{url} -> {r.status_code}"


def test_positive_limit_still_200(client):
    sid = _make_source(client)
    for tpl in _LIMIT_URLS:
        url = tpl.format(v=5, sid=sid)
        r = client.get(url)
        assert r.status_code == 200, f"{url} -> {r.status_code}: {r.text}"


# ---------------------------------------------------------------------------
# Partial PUT must not clobber untouched fields with schema defaults
# ---------------------------------------------------------------------------

def test_partial_put_source_keeps_untouched_fields(client):
    sid = _make_source(client)
    # Set non-default values first.
    r = client.put(f"/api/v1/sources/{sid}",
                   json={"reels_only": False, "max_items": 10, "delay_min_s": 5.0})
    assert r.status_code == 200, r.text
    assert r.json()["reels_only"] is False

    # Partial update: only max_items. reels_only/delays must survive.
    r = client.put(f"/api/v1/sources/{sid}", json={"max_items": 50})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["max_items"] == 50
    assert body["reels_only"] is False
    assert body["delay_min_s"] == 5.0

    # And the reverse direction.
    r = client.put(f"/api/v1/sources/{sid}", json={"reels_only": True})
    assert r.status_code == 200, r.text
    assert r.json()["max_items"] == 50


def test_partial_put_caption_keeps_untouched_fields(client):
    r = client.post("/api/v1/captions",
                    json={"name": "cap1", "content": "hello {name}", "category": "fun"})
    assert r.status_code == 201, r.text
    cid = r.json()["id"]

    r = client.put(f"/api/v1/captions/{cid}", json={"content": "changed"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["content"] == "changed"
    assert body["name"] == "cap1"
    assert body["category"] == "fun"


def test_partial_put_schedule_rule_keeps_untouched_fields(client):
    r = client.post("/api/v1/schedule",
                    json={"name": "test rule", "account_id": None,
                          "day_of_week": 1, "hour": 8, "minute": 30})
    assert r.status_code == 201, f"{r.status_code} {r.text}"
    rid = r.json()["id"]
    orig = r.json()

    r = client.put(f"/api/v1/schedule/{rid}", json={"hour": 9})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["hour"] == 9
    assert body["minute"] == orig["minute"]
    assert body["day_of_week"] == orig["day_of_week"]


# ---------------------------------------------------------------------------
# IG username charset (rename endpoint + account create schema)
# ---------------------------------------------------------------------------

def _make_account(c, username="testuser"):
    r = c.post("/api/v1/accounts", json={"username": username, "password": "x"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.mark.parametrize("bad", [
    "bad-name!", "with space", "semi;colon", "quo'te", "uniçode",
    "a" * 31, "@@@",
    # Note: "dot..dot.." matches the charset ([A-Za-z0-9._]) and is accepted;
    # the validator enforces charset+length, not Instagram's style policy.
])
def test_rename_rejects_bad_usernames(client, bad):
    aid = _make_account(client)
    r = client.post(f"/api/v1/accounts/{aid}/rename", params={"new_username": bad})
    assert r.status_code in (400, 422), f"{bad!r} -> {r.status_code}"


@pytest.mark.parametrize("good", ["valid.name_123", "UPPER", "a", "a" * 30])
def test_rename_accepts_good_usernames(client, good):
    aid = _make_account(client)
    r = client.post(f"/api/v1/accounts/{aid}/rename", params={"new_username": good})
    assert r.status_code == 200, f"{good!r} -> {r.status_code}: {r.text}"
    assert r.json()["username"] == good.lstrip("@")


def test_rename_strips_leading_at(client):
    aid = _make_account(client)
    r = client.post(f"/api/v1/accounts/{aid}/rename", params={"new_username": "@withat"})
    assert r.status_code == 200, r.text
    assert r.json()["username"] == "withat"


def test_create_account_rejects_bad_username(client):
    r = client.post("/api/v1/accounts",
                    json={"username": "not valid!", "password": "x"})
    assert r.status_code == 422, r.status_code
