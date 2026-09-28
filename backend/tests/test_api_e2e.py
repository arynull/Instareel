"""End-to-end API tests: real HTTP through TestClient, isolated async DB.

Skipped by design (need live infra): celery .delay paths (process/reprocess/
retry/check-all/pool ops), IG-network endpoints (test-session, bio apply,
proxy test), websocket/redis paths.
"""
import io
import os
import wave
import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.deps import get_current_admin, get_db
from app.config import settings
from app.database import Base
from app.main import app
from app.models import (
    Account,
    AccountStatus,
    AudioTrack,
    BioConfig,
    CaptionTemplate,
    EffectPreset,
    HashtagSet,
    Post,
    PostStatus,
    Proxy,
    ProxyProtocol,
    ScheduleRule,
    Video,
    VideoStatus,
)



def _run(coro):
    """run_until_complete robust to pytest-asyncio unsetting/closing the
    main-thread loop after async tests in earlier modules. Reuses the
    existing loop when usable (same semantics as the old
    asyncio.get_event_loop().run_until_complete), else makes a fresh one."""
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

    import asyncio

    _run(_create())
    monkeypatch.setattr(settings, "MEDIA_ROOT", str(tmp_path / "media"))
    monkeypatch.setattr(settings, "AUTO_PROCESS_ON_UPLOAD", False)
    app.dependency_overrides[get_current_admin] = lambda: "admin"
    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as c:
        yield c, maker, engine
    app.dependency_overrides.clear()


def _wav(path, seconds=3):
    import math
    import struct

    with wave.open(str(path), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(44100)
        frames = b"".join(
            struct.pack("<h", int(10000 * math.sin(2 * math.pi * 440 * i / 44100)))
            for i in range(44100 * seconds)
        )
        w.writeframes(frames)


def _png(path):
    from PIL import Image

    Image.new("RGB", (200, 200), (200, 30, 30)).save(path)


class TestAuthFlow:
    def test_login_me_refresh(self, client):
        c, _, _ = client
        r = c.post("/api/v1/auth/login", json={"username": "admin", "password": os.environ["ADMIN_PASSWORD"]})
        assert r.status_code == 200, r.text
        tokens = r.json()
        me = c.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"})
        assert me.status_code == 200 and me.json()["username"] == "admin"
        r2 = c.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
        assert r2.status_code == 200
        me2 = c.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {r2.json()['access_token']}"})
        assert me2.status_code == 200

    def test_bad_login_401(self, client):
        c, _, _ = client
        assert c.post("/api/v1/auth/login", json={"username": "admin", "password": "wrong"}).status_code == 401

    def test_no_token_401(self, client):
        # The shared fixture bypasses auth via dependency_overrides — drop
        # them temporarily to prove the real guard rejects anonymous calls.
        from app.main import app as _app

        saved = dict(_app.dependency_overrides)
        _app.dependency_overrides.clear()
        try:
            with TestClient(_app) as raw:
                assert raw.get("/api/v1/auth/me").status_code in (401, 403)
        finally:
            _app.dependency_overrides.update(saved)


class TestAccounts:
    def test_crud_cooldown_activate(self, client):
        c, _, _ = client
        r = c.post("/api/v1/accounts", json={"username": "e2e1", "password": "pw", "max_daily_posts": 2})
        assert r.status_code == 201, r.text
        aid = r.json()["id"]
        assert c.get("/api/v1/accounts").json()[0]["username"] == "e2e1"
        assert c.get(f"/api/v1/accounts/{aid}").status_code == 200
        assert c.post(f"/api/v1/accounts/{aid}/cooldown?hours=5").status_code == 200
        assert c.get(f"/api/v1/accounts/{aid}").json()["status"] == "cooldown"
        assert c.post(f"/api/v1/accounts/{aid}/activate").status_code == 200
        assert c.get(f"/api/v1/accounts/{aid}").json()["status"] == "active"
        assert c.put(f"/api/v1/accounts/{aid}", json={"notes": "hi"}).status_code == 200
        assert c.delete(f"/api/v1/accounts/{aid}").status_code == 204
        assert c.get(f"/api/v1/accounts/{aid}").status_code == 404

    def test_delete_account_removes_session_file(self, client, tmp_path):
        import glob
        import os

        sessions = os.path.join(tmp_path, "media", "sessions")
        c, _, _ = client
        aid = c.post("/api/v1/accounts", json={"username": "sdel", "password": "pw"}).json()["id"]
        c.post(f"/api/v1/accounts/{aid}/session",
               files={"file": ("s.json", b'{"cookies": {"sessionid": "1:x"}}', "application/json")})
        assert len(glob.glob(os.path.join(sessions, "*.json"))) == 1
        assert c.delete(f"/api/v1/accounts/{aid}").status_code == 204
        assert glob.glob(os.path.join(sessions, "*.json")) == []

    def test_duplicate_409(self, client):
        c, _, _ = client
        c.post("/api/v1/accounts", json={"username": "dup", "password": "pw"})
        assert c.post("/api/v1/accounts", json={"username": "dup", "password": "pw"}).status_code == 409

    def test_proxy_id_guards(self, client):
        c, _, _ = client
        assert c.post("/api/v1/accounts", json={"username": "px", "password": "pw", "proxy_id": 9999}).status_code == 404
        aid = c.post("/api/v1/accounts", json={"username": "px", "password": "pw"}).json()["id"]
        assert c.put(f"/api/v1/accounts/{aid}", json={"proxy_id": "abc"}).status_code == 400
        assert c.put(f"/api/v1/accounts/{aid}", json={"proxy_id": 9999}).status_code == 404

    def test_session_upload_roundtrip(self, client):
        c, _, _ = client
        aid = c.post("/api/v1/accounts", json={"username": "sess", "password": "pw"}).json()["id"]
        bad = c.post(f"/api/v1/accounts/{aid}/session", files={"file": ("s.json", b"nope", "application/json")})
        assert bad.status_code == 400
        good = c.post(
            f"/api/v1/accounts/{aid}/session",
            files={"file": ("s.json", b'{"cookies": {"sessionid": "1:x"}, "uuids": {}}', "application/json")},
        )
        assert good.status_code == 200, good.text
        assert c.get(f"/api/v1/accounts/{aid}").json()["has_session"] is True
        assert c.delete(f"/api/v1/accounts/{aid}/session").status_code == 204

    def test_rename_moves_session_file(self, client, tmp_path):
        import glob
        import os

        sessions = os.path.join(tmp_path, "media", "sessions")
        c, _, _ = client
        aid = c.post("/api/v1/accounts", json={"username": "oldname", "password": "pw"}).json()["id"]
        c.post(f"/api/v1/accounts/{aid}/session",
               files={"file": ("s.json", b'{"cookies": {"sessionid": "9:x"}, "uuids": {}}', "application/json")})
        assert os.path.exists(os.path.join(sessions, "oldname.json"))
        r = c.post(f"/api/v1/accounts/{aid}/rename?new_username=newname")
        assert r.status_code == 200, r.text
        assert r.json()["username"] == "newname"
        assert not os.path.exists(os.path.join(sessions, "oldname.json"))
        assert os.path.exists(os.path.join(sessions, "newname.json"))
        assert c.get(f"/api/v1/accounts/{aid}").json()["has_session"] is True
        # Invalid + clash rejected.
        assert c.post(f"/api/v1/accounts/{aid}/rename?new_username=no!!bad").status_code == 400
        other = c.post("/api/v1/accounts", json={"username": "taken", "password": "pw"}).json()["id"]
        assert c.post(f"/api/v1/accounts/{aid}/rename?new_username=TAKEN").status_code == 409
        assert c.delete(f"/api/v1/accounts/{other}").status_code == 204

    def test_upload_adopts_owner_and_auto_renames(self, client):
        import json as _json

        c, _, _ = client
        aid = c.post("/api/v1/accounts", json={"username": "before", "password": "pw"}).json()["id"]
        dump = _json.dumps({"cookies": {"ds_user_id": "4242", "ds_user": "after", "sessionid": "4242:tok"},
                            "authorization_data": {"ds_user_id": "4242"}, "uuids": {}})
        r = c.post(f"/api/v1/accounts/{aid}/session",
                   files={"file": ("s.json", dump.encode(), "application/json")})
        assert r.status_code == 200, r.text
        assert "auto-renamed @before → @after" in r.json()["detail"]
        body = c.get(f"/api/v1/accounts/{aid}").json()
        assert body["username"] == "after" and body["ig_user_id"] == "4242"
        # A foreign session for another numeric id is rejected, not overwriting.
        foreign = _json.dumps({"cookies": {"ds_user_id": "777", "ds_user": "after", "sessionid": "777:tok"},
                               "uuids": {}})
        bad = c.post(f"/api/v1/accounts/{aid}/session",
                     files={"file": ("s.json", foreign.encode(), "application/json")})
        assert bad.status_code == 400
        assert c.get(f"/api/v1/accounts/{aid}").json()["ig_user_id"] == "4242"


class TestVideos:
    def _mp4(self, tmp_path):
        import subprocess

        # conftest MEDIA_ROOT is per-test tmp; module tmp_path fixture differs —
        # generate into the OS temp dir instead.
        import tempfile

        out = os.path.join(tempfile.gettempdir(), "e2e_src.mp4")
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=4:size=320x240:rate=10",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", out],
            check=True,
        )
        return out

    def test_upload_list_get_settings_preview_delete(self, client, tmp_path):
        c, _, _ = client
        src = self._mp4(tmp_path)
        with open(src, "rb") as f:
            r = c.post("/api/v1/videos/upload", files={"file": ("v.mp4", f, "video/mp4")}, timeout=120)
        assert r.status_code == 201, r.text
        vid = r.json()["id"]
        assert r.json()["status"] == "uploaded"
        assert len(c.get("/api/v1/videos").json()) == 1
        assert c.get(f"/api/v1/videos/{vid}").status_code == 200
        # Invalid trim pair rejected.
        bad = c.put(f"/api/v1/videos/{vid}/settings", json={"trim_start": 5, "trim_end": 2})
        assert bad.status_code == 400
        # Negative trims rejected (schema ge=0 → 422).
        assert c.put(f"/api/v1/videos/{vid}/settings", json={"trim_start": -1}).status_code == 422
        # Dangerous filter metachars rejected.
        bad2 = c.put(f"/api/v1/videos/{vid}/settings", json={"custom_filters": "eq=1;rm -rf"})
        assert bad2.status_code == 400
        ok = c.put(f"/api/v1/videos/{vid}/settings",
                   json={"effect_preset": "clean_natural", "audio_track": None,
                         "trim_start": 0.5, "trim_end": 3.0})
        assert ok.status_code == 200, ok.text
        body = ok.json()
        assert body["effect_preset"] == "clean_natural" and body["trim_start"] == 0.5
        assert c.get(f"/api/v1/videos/{vid}/status").json()["status"] == "uploaded"
        # Preview streams via short-lived token (no auth header, like <video>).
        assert c.get(f"/api/v1/videos/{vid}/preview").status_code == 401
        tok = c.get(f"/api/v1/videos/{vid}/preview-token").json()["token"]
        pv = c.get(f"/api/v1/videos/{vid}/preview?token={tok}")
        assert pv.status_code == 200 and pv.headers["content-type"] == "video/mp4"
        assert c.get(f"/api/v1/videos/{vid}/preview?token=garbage").status_code == 401
        assert c.get(f"/api/v1/videos/999999/preview?token={tok}").status_code == 401
        # Browsers range-request the stream (progressive playback, seeking).
        rng = c.get(f"/api/v1/videos/{vid}/preview?token={tok}", headers={"Range": "bytes=0-99"})
        assert rng.status_code == 206 and "content-range" in rng.headers
        # Duplicate content rejected.
        with open(src, "rb") as f:
            assert c.post("/api/v1/videos/upload", files={"file": ("v.mp4", f, "video/mp4")}, timeout=120).status_code == 409
        # Bad extension rejected.
        assert c.post("/api/v1/videos/upload", files={"file": ("x.txt", b"hi", "text/plain")}).status_code == 400
        assert c.delete(f"/api/v1/videos/{vid}").status_code == 204

    def test_custom_thumbnail_roundtrip(self, client, tmp_path):
        import subprocess
        import tempfile

        c, _, _ = client
        src = self._mp4(tmp_path)
        with open(src, "rb") as f:
            vid = c.post("/api/v1/videos/upload", files={"file": ("v.mp4", f, "video/mp4")}, timeout=120).json()["id"]
        # Real JPEG cover via ffmpeg.
        jpg = os.path.join(tempfile.gettempdir(), "e2e_cover.jpg")
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=red:size=320x240:duration=1",
             "-frames:v", "1", jpg], check=True,
        )
        with open(jpg, "rb") as f:
            r = c.post(f"/api/v1/videos/{vid}/thumbnail", files={"file": ("cover.jpg", f, "image/jpeg")})
        assert r.status_code == 200, r.text
        assert r.json()["custom_thumbnail_path"]
        got = c.get(f"/api/v1/videos/{vid}/thumbnail")
        assert got.status_code == 200 and got.headers["content-type"] == "image/jpeg"
        # PNG exercises the ffmpeg conversion path.
        png = os.path.join(tempfile.gettempdir(), "e2e_cover.png")
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=blue:size=320x240:duration=1",
             "-frames:v", "1", png], check=True,
        )
        with open(png, "rb") as f:
            r2 = c.post(f"/api/v1/videos/{vid}/thumbnail", files={"file": ("cover.png", f, "image/png")})
        assert r2.status_code == 200, r2.text
        # Bad extension rejected; custom cleared on delete (reverts to auto).
        assert c.post(f"/api/v1/videos/{vid}/thumbnail", files={"file": ("x.txt", b"hi", "text/plain")}).status_code == 400
        assert c.delete(f"/api/v1/videos/{vid}/thumbnail").status_code == 200
        assert c.get(f"/api/v1/videos/{vid}").json()["custom_thumbnail_path"] is None
        assert c.get(f"/api/v1/videos/{vid}/thumbnail").status_code == 404
        assert c.delete(f"/api/v1/videos/{vid}").status_code == 204

    def test_schedule_guards(self, client):
        c, maker, _ = client

        async def seed():
            import asyncio

            async with maker() as s:
                s.add(Account(username="sn", password_enc="x"))
                s.add(Video(original_filename="a.mp4", raw_path="/tmp/a.mp4",
                            md5_hash="snv", status=VideoStatus.uploaded))
                await s.commit()

        import asyncio

        _run(seed())
        acc = c.get("/api/v1/accounts").json()[0]["id"]
        vids = c.get("/api/v1/videos").json()
        vid = vids[0]["id"]
        assert c.post("/api/v1/posts/schedule", json={"video_id": 999999, "account_id": acc}).status_code == 404
        assert c.post("/api/v1/posts/schedule", json={"video_id": vid, "account_id": acc}).status_code == 400


class TestPosts:
    def test_schedule_get_list_queue_delete(self, client):
        c, maker, _ = client

        async def seed():
            async with maker() as s:
                s.add(Account(username="p1", password_enc="x"))
                s.add(Video(original_filename="p.mp4", raw_path="/tmp/p.mp4",
                            md5_hash="pv", status=VideoStatus.processed))
                await s.commit()
                acc = (await s.execute(select(Account).where(Account.username == "p1"))).scalar_one()
                vid = (await s.execute(select(Video).where(Video.md5_hash == "pv"))).scalar_one()
                return acc.id, vid.id

        import asyncio

        acc_id, vid_id = _run(seed())
        r = c.post("/api/v1/posts/schedule",
                   json={"video_id": vid_id, "account_id": acc_id, "caption": "hi", "hashtags": "#a"})
        assert r.status_code == 201, r.text
        pid = r.json()["id"]
        assert r.json()["caption"] == "hi"
        assert c.get(f"/api/v1/posts/{pid}").status_code == 200
        assert any(p["id"] == pid for p in c.get("/api/v1/posts").json())
        assert any(p["id"] == pid for p in c.get("/api/v1/posts/queue").json())
        # Only failed posts can be retried.
        assert c.post(f"/api/v1/posts/{pid}/retry").status_code == 400
        assert c.delete(f"/api/v1/posts/{pid}").status_code == 204

    def test_manual_schedule_prefers_source_caption(self, client):
        c, maker, _ = client

        async def seed():
            async with maker() as s:
                s.add(Account(username="cap1", password_enc="x"))
                s.add(Video(original_filename="s.mp4", raw_path="/tmp/s.mp4",
                            md5_hash="capsrc", status=VideoStatus.processed,
                            source_caption="stolen gems #repost"))
                await s.commit()
                acc = (await s.execute(select(Account).where(Account.username == "cap1"))).scalar_one()
                v = (await s.execute(select(Video).where(Video.md5_hash == "capsrc"))).scalar_one()
                return acc.id, v.id

        import asyncio

        acc_id, vid_id = _run(seed())
        r = c.post("/api/v1/posts/schedule", json={"video_id": vid_id, "account_id": acc_id})
        assert r.status_code == 201, r.text
        # Source caption posts verbatim; no extra hashtag set is appended.
        assert r.json()["caption"] == "stolen gems #repost"
        assert r.json()["hashtags"] == ""


class TestResources:
    def test_sources_crud_lifecycle(self, client, monkeypatch):
        c, _, _ = client
        calls = []

        class FakeTask:
            def delay(self, sid):
                calls.append(sid)

        monkeypatch.setattr("app.tasks.source_tasks.ingest_source", FakeTask())
        # Validation first (no broker touched).
        assert c.post("/api/v1/sources", json={"username": "bad name!"}).status_code == 422
        assert c.post("/api/v1/sources", json={"username": "a", "max_items": 0}).status_code == 422
        assert c.post("/api/v1/sources", json={"username": "a", "max_items": 201}).status_code == 422
        assert c.post("/api/v1/sources", json={"username": "a", "delay_min_s": 10, "delay_max_s": 5}).status_code == 422
        assert c.post("/api/v1/sources", json={"username": "a", "account_id": 999}).status_code == 404
        r = c.post("/api/v1/sources", json={"username": "@UPPER.Case_9"})
        assert r.status_code == 201, r.text
        body = r.json()
        sid = body["id"]
        assert body["username"] == "upper.case_9" and body["status"] == "idle"
        assert c.post("/api/v1/sources", json={"username": "upper.case_9"}).status_code == 409
        assert c.get("/api/v1/sources/999999").status_code == 404
        assert c.get(f"/api/v1/sources/{sid}/items").json() == []
        assert c.get(f"/api/v1/sources/{sid}/items?status=bogus").status_code == 422
        assert c.post(f"/api/v1/sources/{sid}/retry-failed").json() == {"reset": 0}
        assert c.post(f"/api/v1/sources/{sid}/stop").status_code == 409  # idle
        assert c.post("/api/v1/sources/999999/start").status_code == 404
        # Start claims atomically and queues the task.
        started = c.post(f"/api/v1/sources/{sid}/start")
        assert started.status_code == 200, started.text
        assert started.json()["status"] == "running" and calls == [sid]
        assert c.post(f"/api/v1/sources/{sid}/start").status_code == 409  # double-start
        assert c.put(f"/api/v1/sources/{sid}", json={}).status_code == 409
        assert c.delete(f"/api/v1/sources/{sid}").status_code == 409
        stopped = c.post(f"/api/v1/sources/{sid}/stop")
        assert stopped.status_code == 200 and stopped.json()["status"] == "stopping"
        assert c.post(f"/api/v1/sources/{sid}/stop").status_code == 409
        assert c.post(f"/api/v1/sources/{sid}/start").status_code == 409  # stopping != runnable
        # Settings editable once parked (simulate park via delete-guard path):
        # force back to idle through a fresh source for the PUT happy path.
        r2 = c.post("/api/v1/sources", json={"username": "second.page"}).json()
        ok = c.put(f"/api/v1/sources/{r2['id']}", json={"max_items": 50, "reels_only": False})
        assert ok.status_code == 200 and ok.json()["max_items"] == 50
        assert c.put(f"/api/v1/sources/{r2['id']}", json={"account_id": 999}).status_code == 404
        assert c.delete(f"/api/v1/sources/{r2['id']}").status_code == 204
        assert c.get(f"/api/v1/sources/{r2['id']}").status_code == 404

    def test_effects_crud(self, client):
        c, _, _ = client
        r = c.post("/api/v1/effects", json={"name": "t1", "description": "d", "ffmpeg_filter": "eq=1"})
        assert r.status_code == 201, r.text
        eid = r.json()["id"]
        assert c.post("/api/v1/effects", json={"name": "t1"}).status_code == 409
        assert c.put(f"/api/v1/effects/{eid}", json={"name": "t1", "description": "d2"}).status_code == 200
        # Engagement rides the list payload live (computed, never stored).
        rows = c.get("/api/v1/effects").json()
        assert rows and all("avg_engagement" in e for e in rows)
        assert c.delete(f"/api/v1/effects/{eid}").status_code == 204

    def test_audio_upload_list_update_delete(self, client, tmp_path):
        c, _, _ = client
        _wav(tmp_path / "t.wav")
        with open(tmp_path / "t.wav", "rb") as f:
            r = c.post("/api/v1/audio/upload", files={"file": ("t.wav", f, "audio/wav")},
                       data={"name": "snd", "music_volume": "0.5"})
        assert r.status_code == 201, r.text
        tid = r.json()["id"]
        assert r.json()["duration"] and r.json()["duration"] > 2.5
        # Form fields must arrive (regression: unannotated params were dropped).
        assert r.json()["name"] == "snd" and r.json()["music_volume"] == 0.5
        assert len(c.get("/api/v1/audio").json()) == 1
        assert c.put(f"/api/v1/audio/{tid}", json={"name": "snd", "music_volume": 0.9}).status_code == 200
        assert c.put(f"/api/v1/audio/{tid}", json={"name": "snd", "music_volume": 9}).status_code == 422
        assert c.delete(f"/api/v1/audio/{tid}").status_code == 204
        bad = c.post("/api/v1/audio/upload", files={"file": ("x.txt", b"hi", "text/plain")})
        assert bad.status_code == 400

    def test_bios_crud_history(self, client):
        c, _, _ = client
        acc = c.post("/api/v1/accounts", json={"username": "b1", "password": "pw"}).json()["id"]
        assert c.post("/api/v1/bios", json={"account_id": 999, "text": "x"}).status_code == 404
        r = c.post("/api/v1/bios", json={"account_id": acc, "text": "hello", "link_url": "https://t.me/x"})
        assert r.status_code == 201, r.text
        bid = r.json()["id"]
        assert c.put(f"/api/v1/bios/{bid}", json={"account_id": acc, "text": "hello2"}).status_code == 200
        h = c.get(f"/api/v1/bios/{bid}/history")
        assert h.status_code == 200 and h.json() == []
        assert c.delete(f"/api/v1/bios/{bid}").status_code == 204

    def test_bio_ensure_and_section_apply_validation(self, client, tmp_path, monkeypatch):
        import asyncio

        c, maker, _ = client
        # _resolve_route runs on SyncSessionLocal — repoint it at the isolated DB.
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        import app.database

        sync_engine = create_engine(f"sqlite:///{tmp_path}/t.db")
        monkeypatch.setattr(app.database, "SyncSessionLocal", sessionmaker(bind=sync_engine))
        acc = c.post("/api/v1/accounts", json={"username": "s1", "password": "pw"}).json()["id"]
        assert c.post("/api/v1/bios/ensure", json={"account_id": 999}).status_code == 404
        assert c.post("/api/v1/bios/ensure", json={}).status_code == 400
        b1 = c.post("/api/v1/bios/ensure", json={"account_id": acc}).json()
        assert b1["text"] == "" and b1["account_id"] == acc
        b2 = c.post("/api/v1/bios/ensure", json={"account_id": acc}).json()
        assert b2["id"] == b1["id"]  # idempotent: one config per account
        # Attaching a proxy must not 500 the route (sync/async session bug):
        # validation still answers first, through the resolved proxy.
        px = c.post("/api/v1/proxies", json={"url": "http://9.9.9.9:8080", "protocol": "http"}).json()

        async def link():
            async with maker() as s:
                x = await s.get(Account, acc)
                x.proxy_id = px["id"]
                await s.commit()

        _run(link())
        # Partial PUT keeps the other sections intact.
        c.put(f"/api/v1/bios/{b1['id']}", json={"account_id": acc, "text": "hello"})
        row = c.get("/api/v1/bios").json()[0]
        assert (row["text"], row["link_url"]) == ("hello", "")
        # Section validation happens before anything touches Instagram.
        assert c.post(f"/api/v1/bios/{b1['id']}/apply", json={"fields": ["nope"]}).status_code == 422
        assert c.post(f"/api/v1/bios/{b1['id']}/apply", json={"fields": ["link"]}).status_code == 422
        # Live-photo removal resolves the config first (live call needs IG).
        assert c.post("/api/v1/bios/999/picture/remove-live").status_code == 404

    def test_bio_link_guard(self, client):
        c, _, _ = client
        acc = c.post("/api/v1/accounts", json={"username": "b2", "password": "pw"}).json()["id"]
        bad = c.post("/api/v1/bios", json={"account_id": acc, "text": "x", "link_url": "javascript:alert(1)"})
        assert bad.status_code == 422
        ok = c.post("/api/v1/bios", json={"account_id": acc, "text": "x", "link_url": "t.me/x"})
        assert ok.status_code == 201 and ok.json()["link_url"] == "https://t.me/x"
        bid = ok.json()["id"]
        # update_bio validates the account FK as well.
        assert c.put(f"/api/v1/bios/{bid}", json={"account_id": 999999, "text": "x"}).status_code == 404

    def test_proxies_crud_import(self, client, tmp_path):
        c, _, _ = client
        r = c.post("/api/v1/proxies", json={"url": "http://1.1.1.1:8080", "protocol": "http"})
        assert r.status_code == 201, r.text
        assert c.post("/api/v1/proxies", json={"url": "x", "protocol": "nope"}).status_code == 400
        lst = (tmp_path / "l.txt")
        lst.write_text("2.2.2.2:8080\nsocks5://3.3.3.3:1080 |DE\njunk line here\n", encoding="utf-8")
        with open(lst, "rb") as f:
            imp = c.post("/api/v1/proxies/import", files={"file": ("l.txt", f, "text/plain")},
                         data={"default_protocol": "http", "default_country": "FR"})
        assert imp.status_code == 200, imp.text
        assert imp.json()["added"] == 2
        rows = c.get("/api/v1/proxies").json()
        assert len(rows) == 3
        # Form fields must actually arrive (not silently fall back to defaults):
        plain = next(p for p in rows if p["url"] == "http://2.2.2.2:8080")
        assert plain["country"] == "FR", plain

    def test_proxy_pipeline_shape(self, client):
        c, _, _ = client
        c.post("/api/v1/proxies", json={"url": "http://1.1.1.1:8080", "protocol": "http"})
        pipe = c.get("/api/v1/proxies/pipeline")
        assert pipe.status_code == 200, pipe.text
        body = pipe.json()
        assert body["counts"]["total"] == 1
        assert body["counts"]["never_checked"] == 1
        assert body["checker"]["batch"] == 60
        assert body["checker"]["threads"] == 20
        assert body["checker"]["max_fails"] == 5
        assert body["pool"]["purge_after_days"] >= 1
        assert set(body["last_runs"]) == {"health_check", "pool_refresh", "purge", "auto_disabled"}
        assert isinstance(body["recent"], list)

    def test_proxy_reset_zeroes_everything(self, client, tmp_path, monkeypatch):
        import asyncio

        c, maker, _ = client
        # The reset endpoint runs on SyncSessionLocal (same pattern as pool
        # purge) — repoint it at this test's isolated DB file.
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        import app.database

        sync_engine = create_engine(f"sqlite:///{tmp_path}/t.db")
        monkeypatch.setattr(app.database, "SyncSessionLocal", sessionmaker(bind=sync_engine))
        man = c.post("/api/v1/proxies", json={"url": "http://9.9.9.9:8080", "protocol": "http"}).json()
        auto = c.post("/api/v1/proxies", json={"url": "http://8.8.8.8:8080", "protocol": "http"}).json()
        acc = c.post("/api/v1/accounts", json={"username": "rz", "password": "pw"}).json()["id"]

        async def seed():
            async with maker() as s:
                a = await s.get(Proxy, auto["id"])
                a.source = "gh-test"
                m = await s.get(Proxy, man["id"])
                m.is_healthy = True
                m.fail_count = 3
                m.latency_ms = 120
                m.last_error = "boom"
                x = await s.get(Account, acc)
                x.proxy_id = man["id"]
                await s.commit()

        _run(seed())
        r = c.post("/api/v1/proxies/reset")
        assert r.status_code == 200, r.text
        assert r.json() == {"deleted_auto": 1, "reset_manual": 1, "unlinked_accounts": 1}
        rows = c.get("/api/v1/proxies").json()
        assert [p["url"] for p in rows] == ["http://9.9.9.9:8080"]
        assert rows[0]["source"] == "manual"

        async def check():
            async with maker() as s:
                m = await s.get(Proxy, man["id"])
                assert (m.is_healthy, m.fail_count, m.latency_ms, m.last_checked, m.last_error) == (False, 0, None, None, None)
                assert (await s.get(Account, acc)).proxy_id is None
                assert (await s.execute(select(Proxy))).scalars().all()[0].source == "manual"

        _run(check())

    def test_rule_pin_flow(self, client):
        import asyncio

        c, maker, _ = client

        async def seed():
            async with maker() as s:
                s.add(Account(username="pin1", password_enc="x"))
                s.add(Video(original_filename="a.mp4", raw_path="/tmp/a.mp4",
                            md5_hash="pina", status=VideoStatus.processed))
                s.add(Video(original_filename="b.mp4", raw_path="/tmp/b.mp4",
                            md5_hash="pinb", status=VideoStatus.uploaded))
                s.add(Video(original_filename="c.mp4", raw_path="/tmp/c.mp4",
                            md5_hash="pinc", status=VideoStatus.processed))
                await s.commit()
                acc = (await s.execute(select(Account).where(Account.username == "pin1"))).scalar_one()
                va = (await s.execute(select(Video).where(Video.md5_hash == "pina"))).scalar_one()
                vb = (await s.execute(select(Video).where(Video.md5_hash == "pinb"))).scalar_one()
                vc = (await s.execute(select(Video).where(Video.md5_hash == "pinc"))).scalar_one()
                return acc.id, va.id, vb.id, vc.id

        acc, va, vb, vc = _run(seed())
        base = {"name": "pr", "hour": 10, "account_id": acc}
        assert c.post("/api/v1/schedule", json={**base, "pinned_video_id": 999999}).status_code == 404
        assert c.post("/api/v1/schedule", json={**base, "pinned_video_id": vb}).status_code == 422
        r = c.post("/api/v1/schedule", json={**base, "pinned_video_id": va})
        assert r.status_code == 201, r.text
        body = r.json()
        rid = body["id"]
        assert body["pinned_video_id"] == va
        assert body["pinned_video_label"] == f"#{va} a.mp4"
        assert body["pinned_video_status"] == "processed"
        # Double-pin rejected; bad payloads rejected.
        r2 = c.post("/api/v1/schedule", json={"name": "pr2", "hour": 11, "account_id": acc}).json()["id"]
        assert c.post(f"/api/v1/schedule/{r2}/pin", json={"video_id": va}).status_code == 422
        assert c.post(f"/api/v1/schedule/{rid}/pin", json={"video_id": 999999}).status_code == 404
        assert c.post(f"/api/v1/schedule/{rid}/pin", json={}).status_code == 422
        # bool is not a valid video id (bool subclasses int — must not resolve video 1).
        assert c.post(f"/api/v1/schedule/{rid}/pin", json={"video_id": True}).status_code == 422
        assert c.post("/api/v1/schedule/999999/pin", json={"video_id": va}).status_code == 404
        # Queued videos can't be pinned.
        assert c.post("/api/v1/posts/schedule",
                       json={"video_id": vc, "account_id": acc, "caption": "q"}).status_code == 201
        assert c.post(f"/api/v1/schedule/{r2}/pin", json={"video_id": vc}).status_code == 422
        # Unpin falls back to queue mode; re-pin re-arms.
        assert c.post(f"/api/v1/schedule/{rid}/unpin").json()["pinned_video_id"] is None
        repin = c.post(f"/api/v1/schedule/{rid}/pin", json={"video_id": va}).json()
        assert repin["pinned_video_id"] == va and repin["is_active"] is True
        # Deleting the video retires the pin. The FK reference is nulled —
        # FK enforcement forbids a dangling pin — and the retirement is
        # recorded in the system log with the rule name.
        assert c.delete(f"/api/v1/videos/{va}").status_code == 204
        row = next(x for x in c.get("/api/v1/schedule").json() if x["id"] == rid)
        assert row["is_active"] is False and row["pinned_video_id"] is None
        assert row["pinned_video_label"] is None

    def test_rules_captions_crud(self, client):
        c, _, _ = client
        acc = c.post("/api/v1/accounts", json={"username": "r1", "password": "pw"}).json()["id"]
        assert c.post("/api/v1/schedule", json={"name": "x", "hour": 10, "account_id": 999}).status_code == 404
        rule = c.post("/api/v1/schedule", json={"name": "morn", "hour": 10, "minute": 5, "account_id": acc})
        assert rule.status_code == 201, rule.text
        rid = rule.json()["id"]
        assert c.post(f"/api/v1/schedule/{rid}/toggle").json()["is_active"] is False
        # Source-caption preference defaults on and toggles via full-body PUT.
        assert rule.json()["prefer_source_caption"] is True
        full = {**rule.json(), "prefer_source_caption": False}
        full.pop("id", None); full.pop("created_at", None)
        full.pop("pinned_video_label", None); full.pop("pinned_video_status", None)
        assert c.put(f"/api/v1/schedule/{rid}", json=full).json()["prefer_source_caption"] is False
        assert c.delete(f"/api/v1/schedule/{rid}").status_code == 204
        cap = c.post("/api/v1/captions", json={"name": "c", "content": "hello {x}"})
        assert cap.status_code == 201
        tag = c.post("/api/v1/hashtags", json={"name": "h", "tags": "#a,#b"})
        assert tag.status_code == 201
        assert len(c.get("/api/v1/captions").json()) == 1
        assert c.delete(f"/api/v1/captions/{cap.json()['id']}").status_code == 204
        assert c.delete(f"/api/v1/hashtags/{tag.json()['id']}").status_code == 204

    def test_analytics_logs_settings(self, client):
        c, maker, _ = client

        async def seed():
            async with maker() as s:
                s.add(Account(username="a1", password_enc="x"))
                s.add(Video(original_filename="v.mp4", raw_path="/tmp/v.mp4", md5_hash="av",
                            status=VideoStatus.processed, effect_preset="clean_natural", audio_track="hit"))
                s.add(EffectPreset(name="clean_natural", ffmpeg_filter="eq=1"))
                s.add(AudioTrack(name="hit", file_path="/tmp/hit.mp3"))
                await s.commit()
                acc = (await s.execute(select(Account).where(Account.username == "a1"))).scalar_one()
                vid = (await s.execute(select(Video).where(Video.md5_hash == "av"))).scalar_one()
                import datetime as dt

                s.add(Post(video_id=vid.id, account_id=acc.id, status=PostStatus.posted,
                           posted_at=dt.datetime.now(dt.timezone.utc),
                           views_7d=100, likes_7d=10, comments_7d=2, engagement_rate=12.0,
                           last_analytics_check=dt.datetime.now(dt.timezone.utc)))
                await s.commit()

        import asyncio

        _run(seed())
        ov = c.get("/api/v1/analytics/overview?days=30").json()
        assert ov["total_posts"] == 1 and ov["total_views"] == 100
        assert ov["last_analytics_refresh"] is not None
        assert c.get("/api/v1/analytics/overview?days=-5").status_code == 422
        assert len(c.get("/api/v1/analytics/accounts").json()) == 1
        eff = c.get("/api/v1/analytics/effects").json()
        assert eff[0]["posts"] == 1 and eff[0]["views"] == 100
        aud = c.get("/api/v1/analytics/audio").json()
        assert aud[0]["posts"] == 1
        assert len(c.get("/api/v1/analytics/posts?limit=10").json()) == 1
        assert c.get("/api/v1/analytics/posts?limit=-3").status_code == 422
        assert len(c.get("/api/v1/analytics/time-slots").json()) >= 0
        csv = c.get("/api/v1/analytics/export")
        assert csv.status_code == 200 and "audio_track" in csv.text and "hit" in csv.text
        assert isinstance(c.get("/api/v1/logs?limit=10").json(), list)
        assert c.get("/api/v1/logs/stats").status_code == 200
        assert c.get("/api/v1/logs?level=BOGUS").status_code == 400
        assert c.delete("/api/v1/logs?older_than_days=-1").status_code == 422
        assert c.delete("/api/v1/logs?older_than_days=0").status_code == 204
        st = c.get("/api/v1/settings").json()
        assert any(s["key"] == "pool_country" for s in st)
        assert c.put("/api/v1/settings/pool_country", json={"value": "DE"}).json()["value"] == "DE"
        assert c.put("/api/v1/settings/nope_nada", json={"value": "x"}).status_code == 404
        assert c.put("/api/v1/settings/pool_country", json={"value": "x" * 6000}).status_code == 422

    def test_system_stats_shape(self, client):
        c, _, _ = client
        r = c.get("/api/v1/system/stats")
        assert r.status_code == 200, r.text
        body = r.json()
        assert 0 <= body["cpu"]["total"] <= 100 * body["cpu"]["count"]
        assert body["mem"]["total"] > 0
        assert body["disk"] and body["uptime_s"] > 0
        assert isinstance(body["docker"], bool) and isinstance(body["containers"], list)

    def test_preview_thumbnail_404(self, client):
        c, _, _ = client
        # Unauthenticated preview reveals nothing (401 before any existence check).
        assert c.get("/api/v1/videos/999999/preview").status_code == 401
        assert c.get("/api/v1/videos/999999/thumbnail").status_code == 404
        tok = c.post("/api/v1/auth/login",
                     json={"username": "admin", "password": os.environ["ADMIN_PASSWORD"]}).json()["access_token"]
        h = {"Authorization": f"Bearer {tok}"}
        assert c.get("/api/v1/videos/999999/preview", headers=h).status_code == 404

    def test_delete_video_with_posts_guarded(self, client):
        c, maker, _ = client

        async def seed():
            import datetime as dt

            async with maker() as s:
                s.add(Account(username="dv", password_enc="x"))
                s.add(Video(original_filename="d.mp4", raw_path="/tmp/d.mp4",
                            md5_hash="dv", status=VideoStatus.processed))
                await s.commit()
                acc = (await s.execute(select(Account).where(Account.username == "dv"))).scalar_one()
                vid = (await s.execute(select(Video).where(Video.md5_hash == "dv"))).scalar_one()
                s.add(Post(video_id=vid.id, account_id=acc.id, status=PostStatus.posted,
                           posted_at=dt.datetime.now(dt.timezone.utc)))
                await s.commit()
                return vid.id

        import asyncio

        vid_id = _run(seed())
        r = c.delete(f"/api/v1/videos/{vid_id}")
        assert r.status_code == 409, r.text
        assert "post" in r.json()["detail"].lower()
        # The video (and its history) survive the refused delete.
        assert c.get(f"/api/v1/videos/{vid_id}").status_code == 200


class TestAnalyticsNoZeroing:
    def test_failed_lookup_keeps_good_stats(self, tmp_path, monkeypatch):
        import datetime as dt
        import random
        import time

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        monkeypatch.setattr(time, "sleep", lambda s: None)
        monkeypatch.setattr(random, "uniform", lambda a, b: 0)

        import app.database as dbmod
        from app.database import Base
        from app.services.instagram_service import InstagramService
        from app.tasks import periodic_tasks

        engine = create_engine(f"sqlite:///{tmp_path}/ana.db")
        Base.metadata.create_all(engine)
        maker = sessionmaker(bind=engine)
        monkeypatch.setattr(dbmod, "SyncSessionLocal", maker)
        monkeypatch.setattr(InstagramService, "media_info", lambda self, u, m: {})
        s = maker()
        now = dt.datetime.now(dt.timezone.utc)
        s.add(Account(username="ana", password_enc="x"))
        s.add(Video(original_filename="a.mp4", raw_path="/tmp/a.mp4", md5_hash="ana1",
                    status=VideoStatus.processed))
        s.flush()
        acc = s.execute(select(Account).where(Account.username == "ana")).scalar_one()
        vid = s.execute(select(Video).where(Video.md5_hash == "ana1")).scalar_one()
        acc.created_at = now - dt.timedelta(days=30)
        s.add(Post(video_id=vid.id, account_id=acc.id, status=PostStatus.posted,
                   posted_at=now - dt.timedelta(hours=2),
                   views_7d=500, likes_7d=50, comments_7d=5, engagement_rate=11.0))
        s.commit()
        out = periodic_tasks.fetch_all_analytics.apply().get()
        assert out.get("updated") == 0
        p = s.execute(select(Post)).scalars().all()[0]
        assert (p.views_7d, p.likes_7d, p.engagement_rate) == (500, 50, 11.0)


class TestPostingPipeline:
    """Full beat->execute posting flow with celery eager + mocked IG upload.

    No network, no redis, no worker: proves claim single-flight, archiving,
    counters, stale handling and no-duplicate ticks end to end.
    """

    def _setup(self, tmp_path, monkeypatch):
        import datetime as dt
        import random
        import subprocess
        import time

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        import app.database as dbmod
        from app.database import Base
        from app.services.instagram_service import InstagramService
        from app.tasks import celery_app

        vid_src = str(tmp_path / "post.mp4")
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=4:size=320x240:rate=10",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", vid_src], check=True)
        engine = create_engine(f"sqlite:///{tmp_path}/pipe.db")
        Base.metadata.create_all(engine)
        maker = sessionmaker(bind=engine)
        monkeypatch.setattr(dbmod, "SyncSessionLocal", maker)
        monkeypatch.setattr(settings, "MEDIA_ROOT", str(tmp_path / "media"))
        monkeypatch.setattr(time, "sleep", lambda s: None)
        monkeypatch.setattr(random, "uniform", lambda a, b: 0)
        monkeypatch.setattr(random, "randint", lambda a, b: 0)
        celery_app.celery.conf.task_always_eager = True
        self.calls = []

        outer = self

        def fake_upload(_self, username, password, video_path, caption,
                        trial=False, trial_strategy="manual", thumbnail_path=None):
            outer.calls.append((username, video_path, trial, thumbnail_path))
            if outer.fail_with is not None:
                return None, None, outer.fail_with
            return "mid1", "https://www.instagram.com/reel/AAA/", ""

        monkeypatch.setattr(InstagramService, "upload_reel", fake_upload)
        self.fail_with = None
        s = maker()
        now = dt.datetime.now(dt.timezone.utc)
        s.add(Account(username="pipe", password_enc="x", max_daily_posts=3))
        s.add(Video(original_filename="p.mp4", raw_path=vid_src, processed_path=vid_src,
                    md5_hash="pipe1", status=VideoStatus.processed))
        s.add(ScheduleRule(name="tick", day_of_week=-1, hour=now.hour, minute=now.minute))
        old = now - dt.timedelta(days=30)
        s.flush()
        acc = s.execute(select(Account).where(Account.username == "pipe")).scalar_one()
        acc.created_at = old
        s.commit()
        return maker

    def test_happy_path_no_duplicates(self, tmp_path, monkeypatch):
        from app.models import Post, PostStatus, Video, VideoStatus
        from app.tasks.post_tasks import check_and_post

        maker = self._setup(tmp_path, monkeypatch)
        r1 = check_and_post.apply().get()
        # Fresh posts take the ETA path (apply_async with the jitter
        # countdown, executed eagerly here); the tick backstop has nothing
        # left to fire.
        assert r1["created"] == 1 and r1["fired"] == 0, r1
        s = maker()
        posts = s.execute(select(Post)).scalars().all()
        assert len(posts) == 1 and posts[0].status == PostStatus.posted
        assert posts[0].ig_media_id == "mid1"
        vid = s.execute(select(Video).where(Video.md5_hash == "pipe1")).scalar_one()
        assert vid.status == VideoStatus.posted
        acc = s.execute(select(Account).where(Account.username == "pipe")).scalar_one()
        assert (acc.posts_today, acc.total_posts) == (1, 1) and acc.last_post is not None
        assert self.calls and self.calls[0][0] == "pipe"
        # Second tick: nothing processed left, no duplicates ever.
        r2 = check_and_post.apply().get()
        assert r2["created"] == 0
        assert len(s.execute(select(Post)).scalars().all()) == 1

    def test_failed_upload_keeps_video_requeueable(self, tmp_path, monkeypatch):
        from app.models import Post, PostStatus
        from app.tasks.post_tasks import check_and_post

        maker = self._setup(tmp_path, monkeypatch)
        self.fail_with = "generic: boom"
        r1 = check_and_post.apply().get()
        # ETA path (eager): created by the tick, executed by apply_async.
        assert r1["created"] == 1 and r1["fired"] == 0
        s = maker()
        posts = s.execute(select(Post)).scalars().all()
        assert len(posts) == 1 and posts[0].status == PostStatus.failed
        assert "boom" in (posts[0].fail_reason or "")

    def test_stale_video_fails_cleanly(self, tmp_path, monkeypatch):
        import datetime as dt

        from app.models import Post, PostStatus
        from app.tasks.post_tasks import execute_post

        maker = self._setup(tmp_path, monkeypatch)
        s = maker()
        acc = s.execute(select(Account).where(Account.username == "pipe")).scalar_one()
        vid = s.execute(select(Video).where(Video.md5_hash == "pipe1")).scalar_one()
        s.add(Post(video_id=vid.id, account_id=acc.id, status=PostStatus.scheduled,
                   scheduled_for=dt.datetime.now(dt.timezone.utc)))
        s.commit()
        pid = s.execute(select(Post)).scalars().all()[0].id
        # Bypass the ORM (which would nullify the FK and hit NOT NULL):
        # raw-SQL delete leaves a genuinely dangling post.video_id, exactly
        # what the executor's stale guard is for.
        from sqlalchemy import text

        s.execute(text("DELETE FROM videos WHERE id = :i"), {"i": vid.id})
        s.commit()
        out = execute_post.apply(args=[pid]).get()
        assert out["status"] == "failed" and "video" in out["error"]

    def test_breakdowns_grouped(self, client):
        c, maker, _ = client

        async def seed():
            import datetime as dt

            async with maker() as s:
                s.add(Account(username="g1", password_enc="x"))
                for h, fx in (("g1", "fx_a"), ("g2", "fx_b")):
                    s.add(Video(original_filename=f"{h}.mp4", raw_path=f"/tmp/{h}.mp4",
                                md5_hash=h, status=VideoStatus.processed, effect_preset=fx))
                await s.commit()
                acc = (await s.execute(select(Account).where(Account.username == "g1"))).scalar_one()
                for h, views in (("g1", 10), ("g2", 90)):
                    vid = (await s.execute(select(Video).where(Video.md5_hash == h))).scalar_one()
                    s.add(Post(video_id=vid.id, account_id=acc.id, status=PostStatus.posted,
                               posted_at=dt.datetime.now(dt.timezone.utc),
                               views_7d=views, engagement_rate=float(views)))
                s.add(EffectPreset(name="fx_a", ffmpeg_filter="eq=1"))
                s.add(EffectPreset(name="fx_b", ffmpeg_filter="eq=2"))
                s.add(EffectPreset(name="fx_zero", ffmpeg_filter="eq=3"))
                await s.commit()

        import asyncio

        _run(seed())
        rows = {r["name"]: r for r in c.get("/api/v1/analytics/effects").json()}
        assert rows["fx_a"]["views"] == 10 and rows["fx_b"]["views"] == 90
        assert rows["fx_zero"]["posts"] == 0 and rows["fx_zero"]["views"] == 0


class TestGuardianEndpoints:
    def test_account_health_shape_and_404(self, client):
        c, maker, _ = client

        async def seed():
            async with maker() as s:
                s.add(Account(username="hp1", password_enc="x"))
                s.add(Video(original_filename="h.mp4", raw_path="/tmp/h.mp4",
                            md5_hash="hv", status=VideoStatus.processed))
                await s.commit()
                acc = (await s.execute(select(Account).where(Account.username == "hp1"))).scalar_one()
                vid = (await s.execute(select(Video).where(Video.md5_hash == "hv"))).scalar_one()
                s.add(Post(video_id=vid.id, account_id=acc.id, status=PostStatus.failed))
                await s.commit()
                return acc.id

        import asyncio

        acc_id = _run(seed())
        r = c.get(f"/api/v1/accounts/{acc_id}/health")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["account_id"] == acc_id and body["username"] == "hp1"
        assert body["level"] == "healthy" and body["fail_streak"] == 1
        assert isinstance(body["reasons"], list) and body["reasons"]
        assert c.get("/api/v1/accounts/999999/health").status_code == 404

    def test_best_slots_personalized_and_fallback(self, client):
        import datetime as dt

        c, maker, _ = client

        async def seed():
            async with maker() as s:
                s.add(Account(username="sl1", password_enc="x"))
                s.add(Account(username="sl2", password_enc="x"))
                for h in ("s1", "s2", "s3"):
                    s.add(Video(original_filename=f"{h}.mp4", raw_path=f"/tmp/{h}.mp4",
                                md5_hash=h, status=VideoStatus.processed))
                await s.commit()
                acc = (await s.execute(select(Account).where(Account.username == "sl1"))).scalar_one()
                vids = (await s.execute(select(Video))).scalars().all()
                base = dt.datetime(2026, 1, 5, 0, 0, tzinfo=dt.timezone.utc)  # a Monday
                for v, (hour, views) in zip(vids, [(18, 100), (18, 300), (9, 10)]):
                    s.add(Post(video_id=v.id, account_id=acc.id, status=PostStatus.posted,
                               posted_at=base.replace(hour=hour), views_7d=views))
                await s.commit()
                return acc.id

        import asyncio

        acc_id = _run(seed())
        r = c.get(f"/api/v1/analytics/best-slots?account_id={acc_id}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["personalized"] is True
        assert body["slots"][0]["hour_utc"] == 18
        # Default SCHEDULE_TZ=UTC in tests: local wall-clock == UTC hour.
        assert body["slots"][0]["local"] == "18:00"
        assert body["slots"][0]["hour_local"] == 18
        # Fresh account borrows the global fallback, honestly labeled.
        other = c.get("/api/v1/accounts").json()
        other_id = [a["id"] for a in other if a["username"] == "sl2"][0]
        r2 = c.get(f"/api/v1/analytics/best-slots?account_id={other_id}")
        assert r2.status_code == 200 and r2.json()["personalized"] is False
        assert c.get("/api/v1/analytics/best-slots?account_id=999999").status_code == 404

    def test_video_score_with_unprobable_file(self, client):
        c, maker, _ = client

        async def seed():
            async with maker() as s:
                s.add(Video(original_filename="sc.mp4", raw_path="/tmp/does-not-exist.mp4",
                            md5_hash="scv", status=VideoStatus.processed,
                            duration=15.0, source_caption="nice day #reels #sun #fun"))
                await s.commit()
                vid = (await s.execute(select(Video).where(Video.md5_hash == "scv"))).scalar_one()
                return vid.id

        import asyncio

        vid_id = _run(seed())
        r = c.get(f"/api/v1/videos/{vid_id}/score")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["video_id"] == vid_id
        assert body["verdict"] in ("ready", "needs-work", "risky")
        assert sum(b["points"] for b in body["breakdown"]) == body["score"]
        assert any("probe" in s for s in body["suggestions"])
        assert c.get("/api/v1/videos/999999/score").status_code == 404

    def test_video_score_uses_post_caption_and_tags(self, client):
        c, maker, _ = client

        async def seed():
            async with maker() as s:
                s.add(Account(username="sc2", password_enc="x"))
                s.add(Video(original_filename="sc2.mp4", raw_path="/tmp/does-not-exist-2.mp4",
                            md5_hash="scv2", status=VideoStatus.processed, duration=15.0))
                await s.commit()
                acc = (await s.execute(select(Account).where(Account.username == "sc2"))).scalar_one()
                vid = (await s.execute(select(Video).where(Video.md5_hash == "scv2"))).scalar_one()
                s.add(Post(video_id=vid.id, account_id=acc.id, status=PostStatus.scheduled,
                           caption="launch day", hashtags="#a #b #c #d"))
                await s.commit()
                return vid.id

        import asyncio

        vid_id = _run(seed())
        body = c.get(f"/api/v1/videos/{vid_id}/score").json()
        cap = next(b for b in body["breakdown"] if b["key"] == "caption")
        assert cap["points"] == 20, body
        assert not any("No hashtags" in s for s in body["suggestions"])
