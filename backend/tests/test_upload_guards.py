"""Upload guards: Content-Length fail-fast + WS unauthenticated-socket cap."""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import system as system_api
from app.api.deps import get_current_admin, get_db
from app.config import settings
from app.database import Base
from app.main import app
from app.utils.uploads import reject_oversize_content_length


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
        yield c


def _request_with_content_length(value):
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [(b"content-length", value.encode())] if value is not None else [],
    }
    return Request(scope)


def test_rejects_declared_body_over_cap():
    with pytest.raises(HTTPException) as ei:
        reject_oversize_content_length(_request_with_content_length(str(600 * 1024 * 1024)), 500 * 1024 * 1024, "File")
    assert ei.value.status_code == 413


def test_allows_declared_body_at_or_under_cap():
    reject_oversize_content_length(_request_with_content_length(str(500 * 1024 * 1024)), 500 * 1024 * 1024, "File")
    reject_oversize_content_length(_request_with_content_length("123"), 500 * 1024 * 1024, "File")


def test_malformed_or_missing_content_length_ignored():
    # The streaming loops still enforce the cap for these cases.
    reject_oversize_content_length(_request_with_content_length("not-a-number"), 100, "File")
    reject_oversize_content_length(_request_with_content_length(None), 100, "File")


class _FakeWS:
    """Never sends the auth frame: hangs in receive_text until cancelled."""

    def __init__(self, ip="9.9.9.9"):
        self.client = SimpleNamespace(host=ip, port=1234)
        self.accepted = False
        self.closed_code = None

    async def accept(self):
        self.accepted = True

    async def receive_text(self):
        await asyncio.sleep(3600)
        return ""

    async def close(self, code=1000):
        self.closed_code = code


def test_ws_pending_auth_cap_denies_during_handshake():
    async def _run():
        system_api._pending_auth.clear()
        try:
            fakes = [_FakeWS() for _ in range(system_api.WS_MAX_PENDING_AUTH_PER_IP + 1)]
            tasks = [asyncio.ensure_future(system_api.ws_feed(f)) for f in fakes]
            # Let every task run its synchronous counter prefix (no awaits in
            # it), so all 9 register before any of them proceeds to accept.
            await asyncio.sleep(0.5)
            denied = fakes[-1]
            assert denied.closed_code == 1013
            assert not denied.accepted
            for f in fakes[:-1]:
                assert f.accepted
                assert f.closed_code is None
            for t in tasks[:-1]:
                t.cancel()
            await asyncio.gather(*tasks[:-1], return_exceptions=True)
            assert system_api._pending_auth == {}
        finally:
            system_api._pending_auth.clear()

    asyncio.new_event_loop().run_until_complete(_run())


def test_ws_auth_window_is_short():
    # The accept->auth window must stay small: every second an
    # unauthenticated socket is held open is attacker-cheap, server-expensive.
    assert system_api.WS_AUTH_TIMEOUT <= 5.0


def test_video_upload_rejects_huge_declared_content_length(client):
    # End-to-end through the real endpoint: 413 before any byte is streamed.
    huge = 600 * 1024 * 1024  # over the 500MB MAX_UPLOAD_MB default
    r = client.post(
        "/api/v1/videos/upload",
        headers={"Content-Length": str(huge)},
        files={"file": ("x.mp4", b"tiny", "video/mp4")},
    )
    assert r.status_code == 413


# ---- multipart Content-Length slack ----


def test_allows_declared_body_within_multipart_slack():
    # A file of exactly max_bytes has a body a few hundred bytes larger
    # (boundaries + part headers) — that must not 413 at the pre-check.
    from app.utils.uploads import _MULTIPART_OVERHEAD_SLACK

    cap = 500 * 1024 * 1024
    reject_oversize_content_length(
        _request_with_content_length(str(cap + 1000)), cap, "File"
    )
    reject_oversize_content_length(
        _request_with_content_length(str(cap + _MULTIPART_OVERHEAD_SLACK)), cap, "File"
    )


def test_rejects_declared_body_beyond_slack():
    from app.utils.uploads import _MULTIPART_OVERHEAD_SLACK

    cap = 500 * 1024 * 1024
    with pytest.raises(HTTPException) as ei:
        reject_oversize_content_length(
            _request_with_content_length(str(cap + _MULTIPART_OVERHEAD_SLACK + 1)),
            cap,
            "File",
        )
    assert ei.value.status_code == 413


# ---- bio picture: non-HTTP error mid-stream cleans up ----


class _ExplodingWriter:
    """aiofiles.open replacement whose write() raises mid-stream."""

    def __init__(self, real_open, path, mode):
        self._real_open = real_open
        self._path = path
        self._mode = mode

    async def __aenter__(self):
        self._fh = self._real_open(self._path, self._mode)
        self._fh.__enter__()
        return self

    async def __aexit__(self, *a):
        self._fh.__exit__(*a)
        return False

    async def write(self, data):
        raise OSError("disk exploded")


def test_bio_picture_disk_error_cleans_partial_file(client, tmp_path, monkeypatch):
    import aiofiles

    real_open = aiofiles.open
    monkeypatch.setattr(
        aiofiles, "open", lambda path, mode="r": _ExplodingWriter(real_open, path, mode)
    )

    # Create account + bio through the API surface.
    r = client.post("/api/v1/accounts", json={"username": "u_pic", "password": "x" * 12})
    assert r.status_code == 201, r.text[:200]
    aid = r.json()["id"]
    r = client.post("/api/v1/bios", json={"account_id": aid, "text": "t"})
    assert r.status_code == 201, r.text[:200]
    bid = r.json()["id"]

    r = client.post(
        f"/api/v1/bios/{bid}/picture",
        files={"file": ("pic.png", b"\x89PNG" + b"x" * 100, "image/png")},
    )
    assert r.status_code == 400, r.text[:200]
    pics_dir = tmp_path / "media" / "profile_pics"
    leftovers = list(pics_dir.glob("bio_*")) if pics_dir.exists() else []
    assert leftovers == []
