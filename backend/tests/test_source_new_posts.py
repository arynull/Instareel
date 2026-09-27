"""Phase-1 "what's new" + backfill tests for ingest_source (authed path).

Each run first pages the listing newest-first until a whole page brings
nothing new ("caught up"), then backfills older posts from the persisted
end_cursor. end_cursor only advances past fully-processed pages, so a
cap/stop/crash mid-page re-lists that page next run and the SourceItem
dedupe heals it — no video is silently skipped.
"""
import types
from pathlib import Path

import pytest


class _Media:
    def __init__(self, i):
        self.pk = str(1000 + i)
        self.code = f"sc{i}"
        self.media_type = 2
        self.product_type = "clips"
        self.caption_text = f"caption {i}"


def _m(i):
    return _Media(i)


class _FakeClient:
    """Scripted instagrapi stand-in: pages keyed by end_cursor."""

    def __init__(self):
        self.pages = {}
        self.calls = []

    def user_id_from_username(self, username):
        return 999

    def user_info(self, target_id):
        return types.SimpleNamespace(is_private=False)

    def user_medias_paginated(self, target_id, page_size, end_cursor=""):
        self.calls.append(end_cursor)
        return self.pages[end_cursor]

    def clip_download(self, pk_int, path):
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        dest = path / f"{pk_int}.mp4"
        dest.write_bytes(b"FAKEVIDEO-" + str(pk_int).encode())
        return dest


@pytest.fixture()
def env(monkeypatch, tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.models  # noqa: F401 — register tables before create_all
    from app.core.security import encrypt_secret
    from app.database import Base
    from app.models import (
        Account,
        AccountStatus,
        SourceItem,
        SourceItemStatus,
        SourceStatus,
        Video,
        VideoSource,
    )
    from app.utils.instagram_helpers import session_path_for
    from app import database as db_module

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)
    monkeypatch.setattr(db_module, "SyncSessionLocal", maker)

    client = _FakeClient()

    class _FakeService:
        def __init__(self, proxy_url=None, session_path=None):
            pass

        def _make_client(self, username, request_timeout=60):
            return client

    import app.services.instagram_service as ig_mod

    monkeypatch.setattr(ig_mod, "InstagramService", _FakeService)
    monkeypatch.setattr(ig_mod, "_feed_with_retry", lambda cl: (True, "ok"))

    import app.services.anon_ingest as anon_mod

    monkeypatch.setattr(
        anon_mod, "list_public_posts", lambda *a, **k: ([], "429: rate limited"))
    monkeypatch.setattr(
        anon_mod, "download_post", lambda *a, **k: (None, "anon unavailable"))

    import app.services.video_processor as vp_mod

    raw = tmp_path / "raw"
    th = tmp_path / "th"
    raw.mkdir()
    th.mkdir()
    monkeypatch.setattr(
        vp_mod, "media_dirs", lambda: {"raw": str(raw), "thumbnails": str(th)})

    import app.utils.ffmpeg as ff_mod

    monkeypatch.setattr(ff_mod, "probe_sync", lambda p: {"duration": 9.5})

    import app.services.media_hash as mh_mod

    monkeypatch.setattr(mh_mod, "frame_hash", lambda p: None)

    import app.tasks.sync_helpers as sched_mod

    monkeypatch.setattr(sched_mod, "account_reachable", lambda s, acc: True)
    monkeypatch.setattr(sched_mod, "resolve_proxy_url", lambda s, acc: None)

    from app.config import settings

    s = maker()
    acc = Account(username="dlacc", password_enc=encrypt_secret("pw"),
                  status=AccountStatus.active)
    s.add(acc)
    s.commit()
    spath = session_path_for("dlacc", settings.MEDIA_ROOT)
    Path(spath).write_text("{}")
    s.close()

    class Env:
        def make_source(self, max_items=20):
            s = maker()
            src = VideoSource(
                username="somepage", status=SourceStatus.running,
                max_items=max_items, reels_only=True, with_covers=False,
                auto_process=False, delay_min_s=0, delay_max_s=0)
            s.add(src)
            s.commit()
            sid = src.id
            s.close()
            return sid

        def reset_running(self, sid):
            s = maker()
            src = s.get(VideoSource, sid)
            src.status = SourceStatus.running
            s.commit()
            s.close()

        def run(self, sid):
            from app.tasks.source_tasks import ingest_source

            return ingest_source.run(sid)

        def video_count(self):
            s = maker()
            n = s.query(Video).count()
            s.close()
            return n

        def item_count(self, sid):
            s = maker()
            n = s.query(SourceItem).filter_by(source_id=sid).count()
            s.close()
            return n

        def get_source(self, sid):
            s = maker()
            src = s.get(VideoSource, sid)
            s.expunge(src)
            s.close()
            return src

        def seed_anon_row(self, sid, shortcode):
            s = maker()
            s.add(SourceItem(source_id=sid, media_pk=shortcode,
                             shortcode=shortcode, media_type="2",
                             status=SourceItemStatus.downloaded))
            s.commit()
            s.close()

    e = Env()
    e.client = client
    e._maker = maker
    return e


class TestWhatsNewPhase:
    def test_rerun_fetches_new_posts_then_backfills(self, env):
        sid = env.make_source(max_items=12)
        p1 = [_m(i) for i in range(1, 13)]
        env.client.pages = {
            "": (p1, "c1"),
            "c1": ([_m(i) for i in range(13, 17)], "c2"),
            "c2": ([], ""),
        }
        r1 = env.run(sid)
        assert r1["status"] == "completed"
        assert r1["downloaded_this_run"] == 12
        # Phase 1 never persists end_cursor.
        assert env.get_source(sid).end_cursor is None

        n1, n2 = _m(101), _m(102)
        env.client.pages = {
            "": ([n1, n2] + p1, "c0"),
            "c0": (p1, "c1"),
            "c1": ([_m(i) for i in range(21, 25)], "c2"),
            "c2": ([], ""),
        }
        env.reset_running(sid)
        env.client.calls.clear()
        r2 = env.run(sid)

        assert r2["status"] == "completed"
        # 2 brand-new posts + 4 older ones the first run never reached.
        assert r2["downloaded_this_run"] == 6
        assert env.video_count() == 18
        assert env.item_count(sid) == 18
        # Backfill cursor advanced past the fully-processed page.
        assert env.get_source(sid).end_cursor == "c2"
        # Phase 1 checked the head, phase 2 the tail — nothing re-downloaded.
        assert env.client.calls == ["", "c0", "c1", "c2"]

    def test_midpage_cap_gap_is_healed_next_run(self, env):
        # max_items < PAGE_SIZE: run 1 stops mid-page; those videos must not
        # be lost (the pre-existing per-page cursor bug this fixes).
        sid = env.make_source(max_items=12)
        page1 = [_m(i) for i in range(1, 21)]
        env.client.pages = {"": (page1, "c1"), "c1": ([], "")}
        r1 = env.run(sid)
        assert r1["downloaded_this_run"] == 12

        env.client.pages = {
            "": (page1, "c1"),
            "c1": ([_m(i) for i in range(21, 25)], "c2"),
            "c2": ([], ""),
        }
        env.reset_running(sid)
        env.client.calls.clear()
        r2 = env.run(sid)

        # The 8 unprocessed items of run 1's page are picked up, then 4 older.
        assert r2["downloaded_this_run"] == 12
        assert env.video_count() == 24
        assert env.item_count(sid) == 24

    def test_tiny_source_exhausts_without_backfill(self, env):
        sid = env.make_source(max_items=20)
        env.client.pages = {"": ([_m(1), _m(2), _m(3)], "")}
        r = env.run(sid)
        assert r["status"] == "completed"
        assert r["downloaded_this_run"] == 3
        assert r["exhausted"] is True
        # One listing call total — no redundant backfill phase.
        assert env.client.calls == [""]
        assert env.get_source(sid).end_cursor is None

    def test_cross_mode_shortcode_rows_are_recognized(self, env):
        # Rows written by anonymous runs use the shortcode as media_pk.
        sid = env.make_source(max_items=20)
        env.seed_anon_row(sid, "sc1")
        env.seed_anon_row(sid, "sc2")
        env.client.pages = {
            "": ([_m(1), _m(2)], "c1"),
            "c1": ([_m(3), _m(4)], "c2"),
            "c2": ([], ""),
        }
        r = env.run(sid)
        assert r["status"] == "completed"
        # m1/m2 recognized via shortcode — not re-downloaded; m3/m4 fetched.
        assert r["downloaded_this_run"] == 2
        assert env.video_count() == 2
        s = env._maker()
        from app.models import SourceItem

        assert s.query(SourceItem).filter_by(
            source_id=sid, media_pk="1001").count() == 0
        s.close()

    def test_no_new_posts_backfills_from_cursor(self, env):
        sid = env.make_source(max_items=20)
        p1 = [_m(i) for i in range(1, 13)]
        p2 = [_m(i) for i in range(13, 21)]
        env.client.pages = {"": (p1, "c1"), "c1": (p2, "c2"), "c2": ([], "")}
        r1 = env.run(sid)
        assert r1["downloaded_this_run"] == 20

        # Nothing new: phase 1 sees a fully-known head page and stops after it.
        env.client.pages = {
            "": (p1, "c0"),
            "c0": (p1 + p2, "c1"),
            "c1": ([_m(i) for i in range(21, 25)], "c2"),
            "c2": ([], ""),
        }
        env.reset_running(sid)
        env.client.calls.clear()
        r2 = env.run(sid)
        assert r2["downloaded_this_run"] == 4
        assert env.video_count() == 24
        assert env.client.calls == ["", "c0", "c1", "c2"]

    def test_stop_mid_run(self, env, monkeypatch):
        from app.tasks import source_tasks

        sid = env.make_source(max_items=20)
        env.client.pages = {"": ([_m(i) for i in range(1, 6)], "c1")}
        monkeypatch.setattr(source_tasks, "_stopped", lambda s, sid_: True)
        r = env.run(sid)
        assert r["status"] == "stopped"
        from app.models import SourceStatus

        assert env.get_source(sid).status == SourceStatus.idle
        assert env.video_count() == 0


class TestMediaKnown:
    def test_unknown(self, env):
        from app.tasks.source_tasks import _media_known

        s = env._maker()
        assert _media_known(s, 999, "1001", "sc1") is False
        s.close()

    def test_pk_and_shortcode_matches(self, env):
        from app.models import SourceItem, SourceItemStatus
        from app.tasks.source_tasks import _media_known

        sid = env.make_source()
        s = env._maker()
        s.add(SourceItem(source_id=sid, media_pk="1001", shortcode="sc1",
                         media_type="2", status=SourceItemStatus.downloaded))
        s.add(SourceItem(source_id=sid, media_pk="sc2", shortcode="sc2",
                         media_type="2", status=SourceItemStatus.downloaded))
        s.commit()
        assert _media_known(s, sid, "1001", "sc1") is True
        assert _media_known(s, sid, "1002", "sc2") is True
        assert _media_known(s, sid, "1003", "sc3") is False
        # Another source's rows don't leak across.
        assert _media_known(s, sid + 999, "1001", "sc1") is False
        s.close()


class TestFreshBugRegressions:
    def test_bad_pk_fails_one_item_not_the_run(self, env):
        # m10: int(pk) used to sit outside the per-item try — one malformed
        # pk killed the whole run. Now only that item fails.
        sid = env.make_source(max_items=20)

        class _BadPk:
            pk = "not_a_number"
            code = "scbad"
            media_type = 2
            product_type = "clips"
            caption_text = "bad"

        env.client.pages = {"": ([_BadPk(), _m(1), _m(2)], "")}
        r = env.run(sid)
        assert r["status"] == "completed"
        assert r["downloaded_this_run"] == 2
        assert env.video_count() == 2
        s = env._maker()
        from app.models import SourceItem, SourceItemStatus

        bad = s.query(SourceItem).filter_by(source_id=sid, media_pk="not_a_number").one()
        assert bad.status == SourceItemStatus.failed
        assert "unparseable media pk" in (bad.error or "")
        s.close()

    def test_stop_in_anonymous_phase_reports_stopped(self, env, monkeypatch):
        # The anonymous loop used to report "completed" on user stop.
        import app.services.anon_ingest as anon_mod
        from app.tasks import source_tasks

        entries = [
            {"shortcode": f"sc{i}", "is_video": True,
             "product_type": "clips", "caption": None}
            for i in range(1, 4)
        ]
        monkeypatch.setattr(anon_mod, "list_public_posts",
                            lambda *a, **k: (entries, None))
        monkeypatch.setattr(source_tasks, "_stopped", lambda s, sid_: True)
        sid = env.make_source(max_items=20)
        r = env.run(sid)
        assert r["status"] == "stopped"
        from app.models import SourceStatus

        assert env.get_source(sid).status == SourceStatus.idle
        assert env.video_count() == 0

    def test_stop_during_pacing_reports_stopped(self, env, monkeypatch):
        # Stop requested while pacing() sleeps between items used to break
        # out of the loop without setting stopped_early, so the run was
        # reported "completed". Regression for the bare `if pacing(): break`.
        import app.services.anon_ingest as anon_mod
        from app.tasks import source_tasks

        entries = [
            {"shortcode": f"psc{i}", "is_video": True,
             "product_type": "clips", "caption": None}
            for i in range(1, 4)
        ]
        monkeypatch.setattr(anon_mod, "list_public_posts",
                            lambda *a, **k: (entries, None))

        def fake_download(sc, dl_dir, **k):
            p = Path(dl_dir) / f"{sc}.mp4"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"FAKEVIDEO-" + sc.encode())
            return ({"caption": None, "video": str(p), "cover": None}, None)

        monkeypatch.setattr(anon_mod, "download_post", fake_download)

        # _stopped: False for the loop-entry check of item 1, True from the
        # pacing() call after its download (stop lands between items).
        calls = {"n": 0}

        def fake_stopped(s, sid_):
            calls["n"] += 1
            return calls["n"] >= 2

        monkeypatch.setattr(source_tasks, "_stopped", fake_stopped)
        sid = env.make_source(max_items=20)
        r = env.run(sid)
        assert calls["n"] >= 2, "the pacing() path was never reached"
        assert r["status"] == "stopped"
        from app.models import SourceStatus

        assert env.get_source(sid).status == SourceStatus.idle
        assert env.video_count() == 1
