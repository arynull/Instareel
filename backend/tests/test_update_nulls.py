"""Null semantics for the partial-update schemas.

Explicit JSON null for a NON-nullable column used to sail through
validation and 500 at commit (IntegrityError). It must 422 instead.
Null for a *nullable* column keeps its meaning: clear the field.
"""
import asyncio

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.deps import get_current_admin, get_db
from app.config import settings
from app.database import Base
from app.main import app
from app.models import Account, AudioTrack, BioConfig
from app.schemas.content import (
    AudioUpdate,
    BioUpdate,
    CaptionUpdate,
    EffectUpdate,
    HashtagSetUpdate,
    ProxySourceUpdate,
    ScheduleRuleUpdate,
)


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
    app.dependency_overrides[get_current_admin] = lambda: "admin"
    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as c:
        yield c, maker
    app.dependency_overrides.clear()


async def _make_account(maker):
    async with maker() as s:
        a = Account(username="u1", password_enc="x")
        s.add(a)
        await s.commit()
        return a.id


# ---- schema level: explicit null 422s, unset stays silent ----


def test_schema_explicit_null_rejected():
    with pytest.raises(ValidationError):
        ScheduleRuleUpdate.model_validate({"name": None})
    with pytest.raises(ValidationError):
        CaptionUpdate.model_validate({"content": None})
    with pytest.raises(ValidationError):
        HashtagSetUpdate.model_validate({"tags": None})
    with pytest.raises(ValidationError):
        BioUpdate.model_validate({"text": None})
    with pytest.raises(ValidationError):
        EffectUpdate.model_validate({"ffmpeg_filter": None})
    with pytest.raises(ValidationError):
        AudioUpdate.model_validate({"music_volume": None})
    with pytest.raises(ValidationError):
        ProxySourceUpdate.model_validate({"url": None})


def test_schema_unset_fields_not_rejected():
    # Partial body without the guarded fields validates fine.
    ScheduleRuleUpdate.model_validate({"hour": 7})
    CaptionUpdate.model_validate({"category": "x"})
    BioUpdate.model_validate({})
    # Nullable columns accept explicit null (clear semantics).
    ScheduleRuleUpdate.model_validate({"pinned_video_id": None})
    CaptionUpdate.model_validate({"category": None})


# ---- endpoint level: 422, never 500 ----


def test_rule_null_name_422(client):
    c, _ = client
    rid = c.post("/api/v1/schedule", json={"name": "r", "hour": 6}).json()["id"]
    r = c.put(f"/api/v1/schedule/{rid}", json={"name": None})
    assert r.status_code == 422


def test_rule_nullable_null_clears(client):
    c, _ = client
    rid = c.post("/api/v1/schedule", json={"name": "r", "hour": 6}).json()["id"]
    assert c.put(f"/api/v1/schedule/{rid}", json={"preferred_effect": "x"}).status_code == 200
    r = c.put(f"/api/v1/schedule/{rid}", json={"preferred_effect": None})
    assert r.status_code == 200
    assert r.json()["preferred_effect"] is None


def test_rule_partial_update_still_works(client):
    c, _ = client
    rid = c.post("/api/v1/schedule", json={"name": "r", "hour": 6}).json()["id"]
    r = c.put(f"/api/v1/schedule/{rid}", json={"hour": 7})
    assert r.status_code == 200
    body = r.json()
    assert body["hour"] == 7 and body["name"] == "r"


def test_caption_null_content_422(client):
    c, _ = client
    cid = c.post("/api/v1/captions", json={"name": "c", "content": "hi"}).json()["id"]
    assert c.put(f"/api/v1/captions/{cid}", json={"content": None}).status_code == 422


def test_caption_nullable_category_null_clears(client):
    c, _ = client
    cid = c.post(
        "/api/v1/captions", json={"name": "c", "content": "hi", "category": "x"}
    ).json()["id"]
    r = c.put(f"/api/v1/captions/{cid}", json={"category": None})
    assert r.status_code == 200
    assert r.json()["category"] is None


def test_hashtag_null_tags_422(client):
    c, _ = client
    hid = c.post("/api/v1/hashtags", json={"name": "h", "tags": "#a"}).json()["id"]
    assert c.put(f"/api/v1/hashtags/{hid}", json={"tags": None}).status_code == 422


def test_bio_null_text_422(client):
    c, maker = client
    aid = _run(_make_account(maker))

    async def _make_bio():
        async with maker() as s:
            b = BioConfig(account_id=aid, text="t")
            s.add(b)
            await s.commit()
            return b.id

    bid = _run(_make_bio())
    assert c.put(f"/api/v1/bios/{bid}", json={"text": None}).status_code == 422


def test_effect_null_filter_422(client):
    c, _ = client
    eid = c.post("/api/v1/effects", json={"name": "e"}).json()["id"]
    assert c.put(f"/api/v1/effects/{eid}", json={"ffmpeg_filter": None}).status_code == 422


def test_audio_null_volume_422(client):
    c, maker = client

    async def _make_audio():
        async with maker() as s:
            t = AudioTrack(name="t", file_path="/tmp/x.mp3")
            s.add(t)
            await s.commit()
            return t.id

    tid = _run(_make_audio())
    assert c.put(f"/api/v1/audio/{tid}", json={"music_volume": None}).status_code == 422


def test_proxy_source_null_url_422(client):
    c, maker = client

    async def _make_source():
        async with maker() as s:
            from app.models import ProxySource

            x = ProxySource(name="s", url="http://example.com/x")
            s.add(x)
            await s.commit()
            return x.id

    sid = _run(_make_source())
    assert c.put(f"/api/v1/proxies/sources/{sid}", json={"url": None}).status_code == 422
