"""Account card stats: total_posts/total_views/total_likes must be derived
from the posted Post rows — Account.total_views/total_likes are write-never
columns (always 0), and the card used to read them verbatim."""
import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.deps import get_current_admin, get_db
from app.config import settings
from app.database import Base
from app.main import app
from app.models import Account, Post, PostStatus, Video, VideoStatus


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


def _seed(maker):
    async def seed():
        async with maker() as s:
            s.add(Account(username="card1", password_enc="x"))
            s.add(Account(username="card2", password_enc="x"))
            await s.flush()
            a1 = (await s.execute(select(Account).where(Account.username == "card1"))).scalar_one()
            a2 = (await s.execute(select(Account).where(Account.username == "card2"))).scalar_one()
            vids = []
            for i, md5 in enumerate(["c1", "c2", "c3"]):
                v = Video(original_filename=f"{md5}.mp4", raw_path=f"/tmp/{md5}.mp4",
                          md5_hash=md5, status=VideoStatus.processed)
                s.add(v)
                vids.append(v)
            await s.flush()
            # card1: two posted posts (1000+2500 views, 100+250 likes),
            # one scheduled post (must NOT count), card2: no posts at all.
            s.add(Post(video_id=vids[0].id, account_id=a1.id, status=PostStatus.posted,
                       views_7d=1000, likes_7d=100))
            s.add(Post(video_id=vids[1].id, account_id=a1.id, status=PostStatus.posted,
                       views_7d=2500, likes_7d=250))
            s.add(Post(video_id=vids[2].id, account_id=a1.id, status=PostStatus.scheduled,
                       views_7d=99999, likes_7d=99999))
            await s.commit()
            return a1.id, a2.id

    return _run(seed())


def test_list_accounts_shows_live_post_stats(client):
    c, maker = client
    a1, a2 = _seed(maker)
    rows = {r["username"]: r for r in c.get("/api/v1/accounts").json()}

    one = rows["card1"]
    assert one["total_posts"] == 2
    assert one["total_views"] == 3500
    assert one["total_likes"] == 350

    two = rows["card2"]
    assert two["total_posts"] == 0
    assert two["total_views"] == 0
    assert two["total_likes"] == 0


def test_get_account_shows_live_post_stats(client):
    c, maker = client
    a1, _ = _seed(maker)
    row = c.get(f"/api/v1/accounts/{a1}").json()
    assert row["total_posts"] == 2
    assert row["total_views"] == 3500
    assert row["total_likes"] == 350
