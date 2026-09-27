"""M4/M5/M6: SQLite connection hardening + delete cascades.

- M4: PRAGMA foreign_keys=ON on every SQLite connection (both engines).
- M5: WAL journal mode + 30s busy timeout on every SQLite connection.
- M6: account delete cascades to schedule_rules and video_sources (which
  cascade to source_items) instead of orphaning them.
- FK-enforcement safety of the other delete endpoints (proxy, caption
  template, video with pinned rules).
"""
import asyncio

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError

import app.database as database
from app.database import Base
from app.models import (
    Account,
    AccountStatus,
    CaptionTemplate,
    Post,
    PostStatus,
    Proxy,
    ProxyProtocol,
    ScheduleRule,
    SourceItem,
    Video,
    VideoSource,
    VideoStatus,
)
from app.core.security import encrypt_secret


@pytest.fixture()
def pragma_factory(tmp_path):
    """Sync session factory on a file DB with the production pragmas."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    event.listen(engine, "connect", database._sqlite_pragmas)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_foreign_keys_enforced(pragma_factory):
    # A post pointing at a nonexistent account must fail — proves the
    # PRAGMA is actually on (SQLite parses FKs but doesn't enforce them
    # by default).
    with pragma_factory() as s:
        v = Video(original_filename="v.mp4", raw_path="/tmp/v.mp4", md5_hash="fk1")
        s.add(v)
        s.flush()
        s.add(Post(video_id=v.id, account_id=999999, status=PostStatus.scheduled))
        with pytest.raises(IntegrityError):
            s.commit()


def test_pragmas_set(pragma_factory):
    with pragma_factory() as s:
        assert s.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert s.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert s.execute(text("PRAGMA busy_timeout")).scalar() == 30000


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Minimal async TestClient with dependency overrides (mirrors e2e)."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from fastapi.testclient import TestClient

    from app.api.deps import get_current_admin, get_db
    from app.main import app

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    event.listen(engine.sync_engine, "connect", database._sqlite_pragmas)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def override_db():
        async with maker() as s:
            yield s

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.get_event_loop().run_until_complete(_create())
    app.dependency_overrides[get_current_admin] = lambda: "admin"
    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as c:
        yield c, maker
    app.dependency_overrides.clear()


async def _mk_account(s, username, **kw):
    acc = Account(
        username=username,
        password_enc=encrypt_secret("pw"),
        status=AccountStatus.active,
        **kw,
    )
    s.add(acc)
    await s.flush()
    return acc


def test_account_delete_cascades_rules_sources_items(client):
    c, maker = client

    async def seed():
        async with maker() as s:
            acc = await _mk_account(s, "cascade1")
            s.add(ScheduleRule(name="r1", day_of_week=-1, hour=6, account_id=acc.id))
            src = VideoSource(username="somepage", account_id=acc.id)
            s.add(src)
            await s.flush()
            s.add(SourceItem(source_id=src.id, media_pk="pk1"))
            await s.commit()
            return acc.id

    aid = asyncio.get_event_loop().run_until_complete(seed())
    r = c.delete(f"/api/v1/accounts/{aid}")
    assert r.status_code == 204, r.text

    async def check():
        async with maker() as s:
            from sqlalchemy import select, func

            for model, col, label in (
                (Account, Account.id, "account"),
                (ScheduleRule, ScheduleRule.account_id, "rule"),
                (VideoSource, VideoSource.account_id, "source"),
            ):
                n = (
                    await s.execute(
                        select(func.count()).select_from(model).where(col == aid)
                    )
                ).scalar()
                assert n == 0, f"{label} not cascaded"
            n_items = (
                await s.execute(select(func.count()).select_from(SourceItem))
            ).scalar()
            assert n_items == 0, "source items not cascaded"

    asyncio.get_event_loop().run_until_complete(check())


def test_delete_proxy_detaches_accounts(client):
    c, maker = client

    async def seed():
        async with maker() as s:
            p = Proxy(url="http://127.0.0.1:8080", protocol=ProxyProtocol.http)
            s.add(p)
            await s.flush()
            acc = await _mk_account(s, "proxyacc", proxy_id=p.id)
            await s.commit()
            return p.id, acc.id

    pid, aid = asyncio.get_event_loop().run_until_complete(seed())
    assert c.delete(f"/api/v1/proxies/{pid}").status_code == 204

    async def check():
        async with maker() as s:
            acc = await s.get(Account, aid)
            assert acc.proxy_id is None

    asyncio.get_event_loop().run_until_complete(check())


def test_delete_caption_detaches_rules(client):
    c, maker = client

    async def seed():
        async with maker() as s:
            t = CaptionTemplate(name="t1", content="hello {x}")
            s.add(t)
            await s.flush()
            s.add(
                ScheduleRule(
                    name="r1", day_of_week=-1, hour=6, caption_template_id=t.id
                )
            )
            await s.commit()
            return t.id

    cid = asyncio.get_event_loop().run_until_complete(seed())
    assert c.delete(f"/api/v1/captions/{cid}").status_code == 204

    async def check():
        async with maker() as s:
            from sqlalchemy import select

            rule = (
                await s.execute(select(ScheduleRule).where(ScheduleRule.name == "r1"))
            ).scalar_one()
            assert rule.caption_template_id is None

    asyncio.get_event_loop().run_until_complete(check())


def test_delete_video_clears_pinned_rules(client):
    c, maker = client

    async def seed():
        async with maker() as s:
            v = Video(original_filename="v.mp4", raw_path="/tmp/v.mp4", md5_hash="pin1")
            s.add(v)
            await s.flush()
            s.add(
                ScheduleRule(
                    name="pinned1", day_of_week=-1, hour=6, pinned_video_id=v.id
                )
            )
            await s.commit()
            return v.id

    vid = asyncio.get_event_loop().run_until_complete(seed())
    assert c.delete(f"/api/v1/videos/{vid}").status_code == 204

    async def check():
        async with maker() as s:
            from sqlalchemy import select

            rule = (
                await s.execute(
                    select(ScheduleRule).where(ScheduleRule.name == "pinned1")
                )
            ).scalar_one()
            assert rule.is_active is False
            assert rule.pinned_video_id is None

    asyncio.get_event_loop().run_until_complete(check())
