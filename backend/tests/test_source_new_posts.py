"""Anonymous-only ingest tests for ingest_source.

Source ingest is strictly anonymous: no account, no session file, no
login — ever. When the anonymous listing fails, the run fails cleanly
instead of falling back to an account session. (The old fallback burned
sessions: bulk listing/downloads through a session is exactly the
pattern Instagram flags as scraping, and it cost us killed sessions.)
"""
from pathlib import Path
from types import SimpleNamespace

import pytest


def _entries(*shortcodes, is_video=True, product_type="clips"):
    return [
        {"shortcode": sc, "is_video": is_video,
         "product_type": product_type, "caption": None}
        for sc in shortcodes
    ]


def _fake_download_ok(shortcode, dest_dir, **kw):
    p = Path(dest_dir) / f"{shortcode}.mp4"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"FAKEVIDEO-" + shortcode.encode())
    return ({"caption": None, "video": str(p), "cover": None}, None)


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

    # The trap: an ACTIVE account with a valid session file on disk.
    # The old code auto-picked it as the download account; the new code
    # must never touch it, even when anonymous listing fails.
    from app.config import settings

    s = maker()
    acc = Account(username="dlacc", password_enc=encrypt_secret("pw"),
                  status=AccountStatus.active)
    s.add(acc)
    s.commit()
    acc_id = acc.id
    spath = session_path_for("dlacc", settings.MEDIA_ROOT)
    Path(spath).write_text("{}")
    s.close()

    import app.services.anon_ingest as anon_mod

    monkeypatch.setattr(
        anon_mod, "list_public_posts", lambda *a, **k: ([], None))
    monkeypatch.setattr(
        anon_mod, "download_post",
        lambda *a, **k: (None, "download_post not mocked for this test"))

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

    # No proxy in tests by default; individual tests override.
    monkeypatch.setattr(sched_mod, "pick_spare_proxy", lambda s, **k: None)

    class Env:
        def make_source(self, max_items=20, account_id=None):
            s = maker()
            src = VideoSource(
                username="somepage", status=SourceStatus.running,
                max_items=max_items, reels_only=True, with_covers=False,
                auto_process=False, delay_min_s=0, delay_max_s=0,
                account_id=account_id)
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

    e = Env()
    e.account_id = acc_id
    e._maker = maker
    return e


class TestAnonymousIngest:
    def test_successful_run_downloads_new_reels(self, env, monkeypatch):
        import app.services.anon_ingest as anon_mod

        monkeypatch.setattr(anon_mod, "list_public_posts",
                            lambda *a, **k: (_entries("sc1", "sc2", "sc3"), None))
        monkeypatch.setattr(anon_mod, "download_post", _fake_download_ok)
        sid = env.make_source(max_items=20)
        r = env.run(sid)
        assert r["status"] == "completed"
        assert r["mode"] == "anonymous"
        assert r["downloaded_this_run"] == 3
        assert env.video_count() == 3
        assert env.item_count(sid) == 3
        assert env.get_source(sid).status.value == "completed"

    def test_rerun_skips_already_seen(self, env, monkeypatch):
        import app.services.anon_ingest as anon_mod

        monkeypatch.setattr(anon_mod, "download_post", _fake_download_ok)
        monkeypatch.setattr(anon_mod, "list_public_posts",
                            lambda *a, **k: (_entries("sc1", "sc2"), None))
        sid = env.make_source(max_items=20)
        r1 = env.run(sid)
        assert r1["downloaded_this_run"] == 2

        # Second run lists sc1..sc3: only sc3 is new.
        monkeypatch.setattr(anon_mod, "list_public_posts",
                            lambda *a, **k: (_entries("sc1", "sc2", "sc3"), None))
        env.reset_running(sid)
        r2 = env.run(sid)
        assert r2["status"] == "completed"
        assert r2["downloaded_this_run"] == 1
        assert env.video_count() == 3
        assert env.item_count(sid) == 3

    def test_photos_and_non_reels_are_skipped(self, env, monkeypatch):
        import app.services.anon_ingest as anon_mod

        from app.models import SourceItem, SourceItemStatus

        monkeypatch.setattr(
            anon_mod, "list_public_posts",
            lambda *a, **k: (
                _entries("photo1", is_video=False)
                + _entries("feedvid", product_type="feed")
                + _entries("reel1"),
                None))
        monkeypatch.setattr(anon_mod, "download_post", _fake_download_ok)
        sid = env.make_source(max_items=20)
        r = env.run(sid)
        assert r["status"] == "completed"
        assert r["downloaded_this_run"] == 1
        assert env.video_count() == 1
        s = env._maker()
        skipped = s.query(SourceItem).filter_by(
            source_id=sid, status=SourceItemStatus.skipped).count()
        assert skipped == 2
        s.close()

    def test_empty_listing_completes_with_zero(self, env):
        # ([], None) is the fixture default: nothing new, no error.
        sid = env.make_source()
        r = env.run(sid)
        assert r["status"] == "completed"
        assert r["mode"] == "anonymous"
        assert r["downloaded_this_run"] == 0
        assert env.video_count() == 0

    def test_anon_429_fails_without_account_fallback(self, env, monkeypatch):
        # The core regression: anonymous listing 429'd AND an active
        # account with a session file exists (source even pinned to it).
        # The run must fail cleanly — never burn the session.
        import app.services.anon_ingest as anon_mod

        monkeypatch.setattr(anon_mod, "list_public_posts",
                            lambda *a, **k: ([], "429: rate limited"))
        sid = env.make_source(account_id=env.account_id)
        r = env.run(sid)
        assert r["status"] == "failed"
        assert "never uses an account session" in r["error"]
        assert env.video_count() == 0
        assert env.item_count(sid) == 0
        assert env.get_source(sid).status.value == "failed"

    def test_not_found_fails_cleanly(self, env, monkeypatch):
        import app.services.anon_ingest as anon_mod

        monkeypatch.setattr(anon_mod, "list_public_posts",
                            lambda *a, **k: ([], "not-found: page does not exist"))
        sid = env.make_source()
        r = env.run(sid)
        assert r["status"] == "failed"
        assert "not-found" in r["error"]

    def test_consecutive_failures_abort_the_run(self, env, monkeypatch):
        import app.services.anon_ingest as anon_mod

        monkeypatch.setattr(
            anon_mod, "list_public_posts",
            lambda *a, **k: (_entries(*[f"bad{i}" for i in range(7)]), None))
        monkeypatch.setattr(
            anon_mod, "download_post", lambda *a, **k: (None, "boom: denied"))
        sid = env.make_source(max_items=20)
        r = env.run(sid)
        assert r["status"] == "failed"
        assert "5 consecutive failures" in r["error"]
        assert env.video_count() == 0
        assert env.get_source(sid).failed_count == 5

    def test_instagram_service_is_never_instantiated(self, env, monkeypatch):
        # Tripwire: if any future change reintroduces session usage in
        # source ingest, this blows up instead of silently burning it.
        import app.services.anon_ingest as anon_mod
        import app.services.instagram_service as ig_mod

        def _bomb(*a, **k):
            raise AssertionError(
                "source ingest must never instantiate InstagramService")

        monkeypatch.setattr(ig_mod, "InstagramService", _bomb)

        # Happy path…
        monkeypatch.setattr(anon_mod, "list_public_posts",
                            lambda *a, **k: (_entries("sc1"), None))
        monkeypatch.setattr(anon_mod, "download_post", _fake_download_ok)
        sid = env.make_source(account_id=env.account_id)
        r = env.run(sid)
        assert r["status"] == "completed"

        # …and the failure path (anonymous 429 with a pinned account).
        monkeypatch.setattr(anon_mod, "list_public_posts",
                            lambda *a, **k: ([], "429: rate limited"))
        env.reset_running(sid)
        r = env.run(sid)
        assert r["status"] == "failed"

    def test_spare_proxy_routes_anon_traffic(self, env, monkeypatch):
        import app.services.anon_ingest as anon_mod
        import app.tasks.sync_helpers as sched_mod

        seen = {}
        fake_proxy = SimpleNamespace(url="http://spare:8080", username=None)
        monkeypatch.setattr(
            sched_mod, "pick_spare_proxy", lambda s, **k: fake_proxy)

        def fake_list(username, limit=None, proxy=None, **k):
            seen["list_proxy"] = proxy
            return (_entries("sc1"), None)

        def fake_dl(shortcode, dest_dir, proxy=None, **k):
            seen["dl_proxy"] = proxy
            return _fake_download_ok(shortcode, dest_dir)

        monkeypatch.setattr(anon_mod, "list_public_posts", fake_list)
        monkeypatch.setattr(anon_mod, "download_post", fake_dl)
        sid = env.make_source()
        r = env.run(sid)
        assert r["status"] == "completed"
        assert r["downloaded_this_run"] == 1
        assert seen["list_proxy"] == "http://spare:8080"
        assert seen["dl_proxy"] == "http://spare:8080"


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


class TestStopHandling:
    def test_stop_in_anonymous_phase_reports_stopped(self, env, monkeypatch):
        # The anonymous loop used to report "completed" on user stop.
        import app.services.anon_ingest as anon_mod
        from app.tasks import source_tasks

        monkeypatch.setattr(anon_mod, "list_public_posts",
                            lambda *a, **k: (_entries("sc1", "sc2", "sc3"), None))
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

        monkeypatch.setattr(anon_mod, "list_public_posts",
                            lambda *a, **k: (_entries("psc1", "psc2", "psc3"), None))
        monkeypatch.setattr(anon_mod, "download_post", _fake_download_ok)

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


class TestOrphanCleanup:
    def test_failed_download_does_not_orphan_cover(self, env, monkeypatch, tmp_path):
        """finalize() installs the cover into thumbnails/ first; when the
        video then fails validation (here: empty download), the cover must
        be removed too — not orphaned."""
        import app.services.anon_ingest as anon_mod

        monkeypatch.setattr(
            anon_mod, "list_public_posts",
            lambda *a, **k: (_entries("orphan1"), None))

        def _fake_download(shortcode, dest_dir, **kw):
            v = tmp_path / "dl_v.mp4"
            v.write_bytes(b"")  # empty -> finalize raises ValueError
            c = tmp_path / "dl_c.jpg"
            c.write_bytes(b"FAKECOVER")
            return ({"video": str(v), "cover": str(c), "caption": "cap"}, None)

        monkeypatch.setattr(anon_mod, "download_post", _fake_download)

        # with_covers=True so finalize() installs the cover before failing
        maker = env._maker
        from app.models import SourceStatus, VideoSource
        s = maker()
        src = VideoSource(
            username="somepage", status=SourceStatus.running,
            max_items=5, reels_only=True, with_covers=True,
            auto_process=False, delay_min_s=0, delay_max_s=0)
        s.add(src)
        s.commit()
        sid = src.id
        s.close()

        r = env.run(sid)
        assert r["status"] == "completed"

        from app.models import SourceItem, SourceItemStatus
        s = maker()
        item = s.query(SourceItem).filter_by(source_id=sid).one()
        assert item.status == SourceItemStatus.failed
        assert "empty download" in (item.error or "")
        s.close()

        # No orphaned cover in thumbnails/, no orphaned video in raw/
        # (the fixture patched media_dirs to tmp_path/"raw" and /"th").
        assert env.video_count() == 0
        assert list((tmp_path / "th").iterdir()) == []
        assert list((tmp_path / "raw").iterdir()) == []
