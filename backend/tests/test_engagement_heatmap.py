"""Tests for the engagement heatmap + pipeline funnel aggregates.

Pure unit tests for aggregate_heatmap (no DB), plus e2e through
TestClient for /analytics/engagement-heatmap and /analytics/funnel.
Both endpoints are read-only: they must never change row counts.
"""
import asyncio
import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.deps import get_current_admin, get_db
from app.config import settings
from app.database import Base
from app.main import app
from app.models import Account, Post, PostStatus, Video, VideoSource, VideoStatus
from app.services import engagement_heatmap as heat


# ---------- pure aggregation ----------

def test_aggregate_heatmap_empty_gives_stable_grid():
    cells = heat.aggregate_heatmap([])
    assert len(cells) == 7 * 24
    assert all(c["posts"] == 0 and c["avg_views"] == 0 for c in cells)
    # ordered dow 0..6, hour 0..23
    assert cells[0] == {"dow": 0, "hour": 0, "posts": 0, "avg_views": 0}
    assert cells[-1] == {"dow": 6, "hour": 23, "posts": 0, "avg_views": 0}


def test_aggregate_heatmap_buckets_and_averages():
    # SCHEDULE_TZ is UTC in the test env; use explicit UTC datetimes and
    # compute the expected local cell with the same zoneinfo math.
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(settings.SCHEDULE_TZ)
    monday = dt.datetime(2026, 1, 5, 18, 0, tzinfo=dt.timezone.utc)  # a Monday
    cells = heat.aggregate_heatmap([(monday, 100), (monday, 300), (monday, 10)])
    local = monday.astimezone(tz)
    cell = next(c for c in cells if c["dow"] == local.weekday() and c["hour"] == local.hour)
    assert cell["posts"] == 3
    assert cell["avg_views"] == int((100 + 300 + 10) / 3)
    # total posts conserved across the grid
    assert sum(c["posts"] for c in cells) == 3


def test_aggregate_heatmap_handles_naive_datetimes():
    naive = dt.datetime(2026, 1, 6, 9, 30)  # treated as UTC
    cells = heat.aggregate_heatmap([(naive, 50)])
    assert sum(c["posts"] for c in cells) == 1


# ---------- e2e ----------

def _run(coro):
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    if loop.is_closed():
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
    app.dependency_overrides[get_current_admin] = lambda: "admin"
    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as c:
        yield c, maker
    app.dependency_overrides.clear()


def _seed(c, maker):
    async def seed():
        async with maker() as s:
            s.add(Account(username="hm1", password_enc="x"))
            s.add(Account(username="hm2", password_enc="x"))
            s.add(VideoSource(username="src1", max_items=10))
            await s.commit()
            acc1 = (await s.execute(select(Account).where(Account.username == "hm1"))).scalar_one()
            vids = []
            for i, st in enumerate([VideoStatus.processed, VideoStatus.processed, VideoStatus.uploaded]):
                v = Video(original_filename=f"h{i}.mp4", raw_path=f"/tmp/h{i}.mp4",
                          md5_hash=f"hm{i}", status=st)
                s.add(v)
                vids.append(v)
            await s.commit()
            # Recent posts (inside the default 90-day heatmap window).
            base = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)).replace(
                minute=0, second=0, microsecond=0
            )
            for v, (hour, views) in zip(vids, [(18, 100), (18, 300), (9, 10)]):
                s.add(Post(video_id=v.id, account_id=acc1.id, status=PostStatus.posted,
                           posted_at=base.replace(hour=hour), views_7d=views))
            # one scheduled post for the funnel
            s.add(Post(video_id=vids[0].id, account_id=acc1.id, status=PostStatus.scheduled))
            await s.commit()
            return acc1.id

    return _run(seed())


class TestHeatmapEndpoint:
    def test_personalized_heatmap(self, client):
        c, maker = client
        acc_id = _seed(c, maker)
        before = _run(self._counts(maker))
        r = c.get(f"/api/v1/analytics/engagement-heatmap?account_id={acc_id}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["personalized"] is True
        assert body["account_id"] == acc_id
        assert len(body["cells"]) == 168
        assert body["total_posts"] == 3
        assert body["max_avg_views"] > 0
        assert body["tz"] == settings.SCHEDULE_TZ
        # read-only: row counts unchanged
        assert _run(self._counts(maker)) == before

    def test_global_fallback_and_404(self, client):
        c, maker = client
        _seed(c, maker)
        others = c.get("/api/v1/accounts").json()
        fresh_id = [a["id"] for a in others if a["username"] == "hm2"][0]
        r = c.get(f"/api/v1/analytics/engagement-heatmap?account_id={fresh_id}")
        assert r.status_code == 200 and r.json()["personalized"] is False
        assert r.json()["total_posts"] == 3  # global history borrowed
        assert c.get("/api/v1/analytics/engagement-heatmap?account_id=999999").status_code == 404

    def test_no_account_param_gives_global(self, client):
        c, maker = client
        _seed(c, maker)
        r = c.get("/api/v1/analytics/engagement-heatmap")
        assert r.status_code == 200
        assert r.json()["personalized"] is False
        assert r.json()["total_posts"] == 3

    async def _counts(self, maker):
        async with maker() as s:
            return {
                "posts": (await s.execute(select(Post))).scalars().all().__len__(),
                "videos": (await s.execute(select(Video))).scalars().all().__len__(),
            }


class TestFunnelEndpoint:
    def test_funnel_counts_and_order(self, client):
        c, maker = client
        _seed(c, maker)
        r = c.get("/api/v1/analytics/funnel")
        assert r.status_code == 200, r.text
        stages = {s["key"]: s["count"] for s in r.json()["stages"]}
        assert [s["key"] for s in r.json()["stages"]] == ["sources", "library", "ready", "scheduled", "posted"]
        assert stages["sources"] == 1
        assert stages["library"] == 3
        assert stages["ready"] == 2
        assert stages["scheduled"] == 1
        assert stages["posted"] == 3
