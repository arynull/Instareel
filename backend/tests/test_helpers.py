"""Minimal pytest suite for pure helpers — no DB, no network, no subprocess.

Run from backend/:  python -m pytest tests/ -q
"""
import json
import os
import sys

from sqlalchemy import select

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("FERNET_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite://")
os.environ.setdefault("SYNC_DATABASE_URL", "sqlite://")

from app.utils.ffmpeg import _parse_probe_json, build_command, build_filter  # noqa: E402
from app.utils.instagram_helpers import device_settings_for, session_owner_info, session_path_for  # noqa: E402
from app.core.security import decrypt_secret, encrypt_secret  # noqa: E402


class TestSessionPath:
    def test_dot_becomes_underscore(self):
        assert session_path_for("deer.9693176", "media").endswith("deer_9693176.json")

    def test_safe_name_unchanged(self):
        assert session_path_for("plain_user", "media").endswith("plain_user.json")

    def test_creates_directory(self, tmp_path):
        p = session_path_for("user1", str(tmp_path))
        assert os.path.isdir(os.path.dirname(p))


class TestSessionOwnerInfo:
    def test_dict_cookies(self):
        uid, uname = session_owner_info(
            {"cookies": {"ds_user_id": "123", "ds_user": "new.name", "sessionid": "123:abc"}})
        assert (uid, uname) == ("123", "new.name")

    def test_list_cookies(self):
        uid, uname = session_owner_info(
            {"cookies": [{"name": "ds_user_id", "value": "77"}, {"name": "ds_user", "value": "u"}]})
        assert (uid, uname) == ("77", "u")

    def test_authorization_data_preferred(self):
        uid, uname = session_owner_info(
            {"authorization_data": {"ds_user_id": 999}, "cookies": {"ds_user": "u"}})
        assert (uid, uname) == ("999", "u")

    def test_sessionid_fallback(self):
        assert session_owner_info({"cookies": {"sessionid": "4242:tok"}}) == ("4242", None)

    def test_missing_info(self):
        assert session_owner_info({"cookies": {"sessionid": "x"}}) == (None, None)
        assert session_owner_info({}) == (None, None)
        assert session_owner_info("nope") == (None, None)


class TestDeviceSettings:
    def test_stable_fingerprint_per_account(self):
        assert device_settings_for("a") == device_settings_for("a")
        assert device_settings_for("a") != device_settings_for("b")

    def test_tracks_current_app_version(self):
        d = device_settings_for("x")
        # Numeric major comparison (lexicographic ">=" would accept "99.x" too).
        assert int(str(d["app_version"]).split(".")[0]) >= 400  # Instagram rejects outdated versions
        assert d["model"] == "Pixel 8 Pro"


class TestFfmpegBuilder:
    def test_crop_scale_chain_always_present(self):
        fc = build_filter()
        # Conditional 9:16 center-crop (either axis may overflow) + upscale.
        assert "crop=min(iw\\,ih*9/16):min(ih\\,iw*16/9)" in fc
        assert "scale=720:1280" in fc
        assert "overlay" not in fc

    def test_crop_commas_escaped_but_colons_not(self):
        fc = build_filter()
        crop = fc.split("crop=")[1].split(",scale=")[0]
        assert "\\," in crop  # min() arg separators
        assert crop.count(":") == 3  # w:h:x:y structure intact

    def test_center_crop_math_fits_all_aspect_ratios(self):
        import re

        fc = build_filter()
        crop = fc.split("crop=")[1].split(",scale=")[0]
        w_e, h_e, x_e, y_e = crop.split(":")

        def ev(expr, iw, ih):
            e = expr.replace("\\,", ",").replace("iw", str(iw)).replace("ih", str(ih))
            assert re.fullmatch(r"[\d\.\+\-\*/\(\), min]+", e), e
            return eval(e, {"__builtins__": {}}, {"min": min})  # noqa: S307 — test-only, guarded charset

        cases = [(1280, 720), (720, 1280), (356, 638), (640, 640),
                 (1080, 1920), (1920, 1080), (320, 568), (600, 1067)]
        for iw, ih in cases:
            ow, oh, x, y = (int(ev(e, iw, ih)) for e in (w_e, h_e, x_e, y_e))
            assert 0 <= x and 0 <= y and x + ow <= iw and y + oh <= ih, (iw, ih, ow, oh, x, y)
            assert abs(ow / oh - 9 / 16) < 0.02, (iw, ih, ow, oh)

    def test_effect_and_custom_filters_appended(self):
        fc = build_filter(effect_filter="eq=saturation=1.2", custom_filters="unsharp")
        assert "eq=saturation=1.2" in fc and "unsharp" in fc

    def test_command_has_encode_flags(self):
        cmd = build_command("in.mp4", "out.mp4")
        joined = " ".join(cmd)
        assert "-c:v" in cmd and "libx264" in joined
        assert "+faststart" in joined

    def test_trim_window(self):
        cmd = build_command("in.mp4", "out.mp4", trim_start=2, trim_end=5)
        assert "-ss" in cmd and "2" in cmd and "3" in cmd

    def test_silent_track_added_when_no_audio(self):
        cmd = build_command("in.mp4", "out.mp4", has_audio=False)
        assert "anullsrc" in " ".join(cmd)


class TestProbeParsing:
    def test_parses_streams(self):
        raw = json.dumps({
            "streams": [
                {"codec_type": "video", "width": 1920, "height": 1080},
                {"codec_type": "audio"},
            ],
            "format": {"duration": "12.5"},
        }).encode()
        info = _parse_probe_json(raw)
        assert info["width"] == 1920
        assert info["height"] == 1080
        assert info["duration"] == 12.5
        assert info["has_audio"] is True

    def test_no_audio_flag(self):
        raw = json.dumps({
            "streams": [{"codec_type": "video", "width": 720, "height": 1280}],
            "format": {"duration": "5"},
        }).encode()
        info = _parse_probe_json(raw)
        assert info["has_audio"] is False
        assert info["duration"] == 5.0

    def test_empty_probe_defaults(self):
        info = _parse_probe_json(b"{}")
        assert info["duration"] == 0
        assert info["width"] == 0
        assert info["height"] == 0
        assert info["has_audio"] is False
        assert info["video_packets"] is None

    def test_packet_counts_parsed(self):
        raw = json.dumps({
            "streams": [
                {"codec_type": "video", "width": 720, "height": 1280, "nb_read_packets": "483"},
                {"codec_type": "audio", "nb_read_packets": "921"},
            ],
            "format": {"duration": "19.2"},
        }).encode()
        info = _parse_probe_json(raw)
        assert info["video_packets"] == 483


class TestDefaultEffects:
    def test_names_unique_and_valid(self):
        from app.services.default_effects import DEFAULT_EFFECT_PRESETS

        names = [p["name"] for p in DEFAULT_EFFECT_PRESETS]
        assert len(names) >= 40
        assert len(set(names)) == len(names)
        for name in names:
            assert 1 <= len(name) <= 128
            assert all(c.isalnum() or c in "-_" for c in name)

    def test_filters_are_single_chain_safe(self):
        from app.services.default_effects import DEFAULT_EFFECT_PRESETS

        for p in DEFAULT_EFFECT_PRESETS:
            f = p["ffmpeg_filter"]
            assert f.strip(), p["name"]
            # Must compose inside [0:v]...[outv]: no graph separators/labels.
            assert ";" not in f and "[" not in f and "]" not in f, p["name"]

    def test_every_preset_builds_a_command(self):
        from app.services.default_effects import DEFAULT_EFFECT_PRESETS

        for p in DEFAULT_EFFECT_PRESETS:
            fc = build_filter(effect_filter=p["ffmpeg_filter"])
            assert "min(iw\\,ih*9/16)" in fc and "scale=720:1280" in fc, p["name"]
            cmd = build_command("in.mp4", "out.mp4", effect_filter=p["ffmpeg_filter"])
            assert "libx264" in " ".join(cmd), p["name"]


class TestAccountHealthPolicy:
    def _now(self):
        import datetime as dt

        return dt.datetime(2026, 9, 18, 12, 0, tzinfo=dt.timezone.utc)

    def test_warmup_caps_fresh_account(self):
        import datetime as dt

        from app.tasks.sync_helpers import effective_max_posts

        now = self._now()
        fresh = now - dt.timedelta(days=3)
        assert effective_max_posts(fresh, 3, now) == 1
        # Naive datetimes (SQLite) behave the same as aware ones.
        assert effective_max_posts(fresh.replace(tzinfo=None), 3, now) == 1

    def test_warmup_releases_after_7_days(self):
        import datetime as dt

        from app.tasks.sync_helpers import effective_max_posts

        now = self._now()
        assert effective_max_posts(now - dt.timedelta(days=7), 3, now) == 3
        assert effective_max_posts(now - dt.timedelta(days=30), 5, now) == 5

    def test_warmup_never_raises_cap(self):
        import datetime as dt

        from app.tasks.sync_helpers import effective_max_posts

        now = self._now()
        assert effective_max_posts(now, 1, now) == 1
        assert effective_max_posts(None, 3, now) == 3

    def test_throttle_backoff(self):
        from app.tasks.sync_helpers import throttle_cooldown_hours

        assert throttle_cooldown_hours(0) == 6
        assert throttle_cooldown_hours(1) == 12
        assert throttle_cooldown_hours(2) == 24
        assert throttle_cooldown_hours(9) == 24

    def test_spare_prefers_same_country_then_load_then_latency(self):
        from app.tasks.sync_helpers import rank_spare_proxies

        assert rank_spare_proxies([], "DE") is None
        de_busy = {"proxy": "de-busy", "load": 5, "country": "DE", "latency": 50}
        de_idle = {"proxy": "de-idle", "load": 0, "country": "de", "latency": 900}
        us_idle = {"proxy": "us-idle", "load": 0, "country": "US", "latency": 20}
        # Same country wins even with worse latency/load (no geo-hop).
        assert rank_spare_proxies([us_idle, de_busy], "DE") == "de-busy"
        # Within a country: lower load, then lower latency; None latency last.
        de_noping = {"proxy": "de-noping", "load": 0, "country": "DE", "latency": None}
        assert rank_spare_proxies([de_busy, de_idle, de_noping], "DE") == "de-idle"
        assert rank_spare_proxies([us_idle, de_noping], "XX") == "us-idle"

    def test_account_age_days_handles_naive(self):
        import datetime as dt

        from app.tasks.sync_helpers import account_age_days

        now = self._now()
        assert account_age_days(now - dt.timedelta(hours=36), now) == 1.5
        assert account_age_days(None, now) > 10**8

    def test_missing_presets_top_up(self):
        from app.services.default_effects import DEFAULT_EFFECT_PRESETS, missing_presets

        assert missing_presets([]) == DEFAULT_EFFECT_PRESETS
        names = [p["name"] for p in DEFAULT_EFFECT_PRESETS]
        assert missing_presets(names) == []
        partial = missing_presets(names[:5])
        assert [p["name"] for p in partial] == names[5:]


class TestFeedRetry:
    def _feed(self):
        from app.services.instagram_service import _feed_with_retry

        return _feed_with_retry

    def test_healthy_session_no_sleep(self, monkeypatch):
        calls = []
        monkeypatch.setattr("time.sleep", lambda s: calls.append(s))

        class Cl:
            def get_timeline_feed(self):
                return {"ok": True}

        assert self._feed()(Cl()) == (True, "")
        assert calls == []

    def test_auth_failure_fails_fast(self, monkeypatch):
        calls = []
        monkeypatch.setattr("time.sleep", lambda s: calls.append(s))

        class Cl:
            def get_timeline_feed(self):
                raise Exception("login_required: need login")

        ok, kind = self._feed()(Cl())
        assert ok is False and kind == "login_required"
        assert calls == []  # no point retrying auth — go straight to login

    def test_network_blip_retried_once_then_ok(self, monkeypatch):
        calls = []
        monkeypatch.setattr("time.sleep", lambda s: calls.append(s))
        state = {"n": 0}

        class Cl:
            def get_timeline_feed(self):
                state["n"] += 1
                if state["n"] == 1:
                    raise ConnectionError("reset by peer")
                return {"ok": True}

        assert self._feed()(Cl()) == (True, "")
        assert calls == [5]

    def test_persistent_outage_returns_last_kind(self, monkeypatch):
        calls = []
        monkeypatch.setattr("time.sleep", lambda s: calls.append(s))

        class Cl:
            def get_timeline_feed(self):
                raise TimeoutError("timed out")

        ok, kind = self._feed()(Cl())
        assert ok is False and kind == "generic"
        assert calls == [5]


class TestPostClaim:
    """Single-flight claim + video reservation against a real (in-memory) DB."""

    def _session(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from app.database import Base

        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        return sessionmaker(bind=engine)()

    _seq = 0

    def _seed_post(self, s, status="scheduled", when=None):
        import datetime as dt

        from app.models import Account, Post, PostStatus, Video

        TestPostClaim._seq += 1
        tag = TestPostClaim._seq
        acc = Account(username=f"claimer{tag}", password_enc="x")
        vid = Video(original_filename="a.mp4", raw_path="/tmp/a.mp4", md5_hash=f"h{tag}")
        s.add_all([acc, vid])
        s.flush()
        post = Post(
            video_id=vid.id,
            account_id=acc.id,
            status=PostStatus[status],
            scheduled_for=when or dt.datetime.now(dt.timezone.utc),
        )
        s.add(post)
        s.commit()
        return acc.id, vid.id, post.id

    def test_claim_then_busy_then_missing(self):
        from app.models import Post, PostStatus
        from app.tasks.sync_helpers import claim_post

        s = self._session()
        _, _, pid = self._seed_post(s)
        assert claim_post(s, pid) == "claimed"
        claimed = s.get(Post, pid)
        assert claimed is not None and claimed.status == PostStatus.posting
        # Beat redelivery / worker crash redelivery must not re-upload.
        assert claim_post(s, pid) == "busy"
        assert claim_post(s, 999999) == "missing"

    def test_claim_refuses_non_scheduled(self):
        from app.tasks.sync_helpers import claim_post

        s = self._session()

        for st in ("posting", "posted", "failed"):
            _, _, pid = self._seed_post(s, status=st)
            assert claim_post(s, pid) == "busy", st

    def test_video_reservation_window(self):
        import datetime as dt

        from app.tasks.sync_helpers import video_already_queued

        s = self._session()
        _, vid, _ = self._seed_post(s)
        assert video_already_queued(s, vid) is True
        assert video_already_queued(s, vid + 999) is False

    def test_video_freed_after_post_or_far_schedule(self):
        import datetime as dt

        from app.models import Post, PostStatus
        from app.tasks.sync_helpers import video_already_queued

        s = self._session()
        _, vid, pid = self._seed_post(s)
        # Posted videos are archived by the executor — no longer queued.
        row = s.get(Post, pid)
        assert row is not None
        row.status = PostStatus.posted
        s.commit()
        assert video_already_queued(s, vid) is False
        # A post scheduled far outside the window doesn't block either.
        far = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)
        _, vid2, _ = self._seed_post(s, when=far)
        assert video_already_queued(s, vid2) is False
        assert video_already_queued(s, vid2, window_min=180) is True


class TestResolveRuleVideo:
    """Hybrid scheduling decisions: pinned one-shots vs queue mode."""

    _seq = 0

    def _session(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        import app.models  # noqa: F401 — register every table before create_all
        from app.database import Base

        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        return sessionmaker(bind=engine)()

    def _video(self, s, status="processed"):
        import datetime as dt

        from app.models import Video, VideoStatus

        TestResolveRuleVideo._seq += 1
        tag = TestResolveRuleVideo._seq
        v = Video(original_filename=f"v{tag}.mp4", raw_path=f"/tmp/v{tag}.mp4",
                  md5_hash=f"rv{tag}", status=VideoStatus[status])
        s.add(v)
        s.commit()
        return v

    def _rule(self, s, **kw):
        from app.models import ScheduleRule

        r = ScheduleRule(name="t", hour=10, **kw)
        s.add(r)
        s.commit()
        return r

    def test_queue_modes(self):
        from app.tasks.sync_helpers import resolve_rule_video

        s = self._session()
        r = self._rule(s)
        assert resolve_rule_video(s, r, set()) == (None, "empty")
        v1 = self._video(s)
        self._video(s)
        v, d = resolve_rule_video(s, r, set())
        assert d == "fire" and v is not None and v.id == v1.id  # oldest first
        # Queue mode has no fall-through: oldest taken → nothing left.
        assert resolve_rule_video(s, r, {v1.id}) == (None, "empty")

    def test_pinned_dispositions(self):
        import datetime as dt

        from app.models import Account, Post, PostStatus
        from app.tasks.sync_helpers import resolve_rule_video

        s = self._session()
        # Gone pin.
        r = self._rule(s, pinned_video_id=999999)
        assert resolve_rule_video(s, r, set()) == (None, "retire_gone")
        # Waiting pins.
        for st in ("uploaded", "processing", "failed"):
            v = self._video(s, status=st)
            r.pinned_video_id = v.id
            assert resolve_rule_video(s, r, set()) == (None, "wait"), st
        # Posted pin retires.
        vp = self._video(s, status="posted")
        r.pinned_video_id = vp.id
        assert resolve_rule_video(s, r, set()) == (None, "retire_posted")
        # Queued pin waits.
        vq = self._video(s)
        acc = Account(username="rvq", password_enc="x")
        s.add(acc)
        s.flush()
        s.add(Post(video_id=vq.id, account_id=acc.id, status=PostStatus.scheduled,
                   scheduled_for=dt.datetime.now(dt.timezone.utc)))
        s.commit()
        r.pinned_video_id = vq.id
        assert resolve_rule_video(s, r, set()) == (None, "wait")
        # Clean pin fires.
        vf = self._video(s)
        r.pinned_video_id = vf.id
        v, d = resolve_rule_video(s, r, set())
        assert d == "fire" and v is not None and v.id == vf.id
        assert resolve_rule_video(s, r, {vf.id}) == (None, "wait")


class TestSiblingGuard:
    def _session(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from app.database import Base

        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        return sessionmaker(bind=engine)()

    def _video_and_posts(self, s, sibling_status="posting", sibling_age_h=0):
        import datetime as dt
        from sqlalchemy import update

        from app.models import Account, Post, PostStatus, Video

        TestPostClaim._seq += 1
        tag = TestPostClaim._seq
        s.add_all([
            Account(username=f"sib{tag}", password_enc="x"),
            Video(original_filename="b.mp4", raw_path="/tmp/b.mp4", md5_hash=f"sib{tag}"),
        ])
        s.flush()
        acc = s.execute(select(Account).where(Account.username == f"sib{tag}")).scalar_one()
        vid = s.execute(select(Video).where(Video.md5_hash == f"sib{tag}")).scalar_one()
        now = dt.datetime.now(dt.timezone.utc)
        mine = Post(video_id=vid.id, account_id=acc.id, status=PostStatus.posting, scheduled_for=now)
        sib = Post(video_id=vid.id, account_id=acc.id, status=PostStatus[sibling_status], scheduled_for=now)
        s.add_all([mine, sib])
        s.commit()
        if sibling_age_h:
            old = now - dt.timedelta(hours=sibling_age_h)
            s.execute(update(Post).where(Post.id == sib.id).values(updated_at=old))
            s.commit()
            s.expire_all()
        return mine.id, vid.id

    def test_fresh_posting_sibling_blocks(self):
        from app.tasks.sync_helpers import find_blocking_sibling

        s = self._session()
        mine, vid = self._video_and_posts(s, "posting")
        sib = find_blocking_sibling(s, mine, vid)
        assert sib is not None and sib.status.value == "posting"

    def test_posted_sibling_blocks(self):
        from app.tasks.sync_helpers import find_blocking_sibling

        s = self._session()
        mine, vid = self._video_and_posts(s, "posted")
        assert find_blocking_sibling(s, mine, vid) is not None

    def test_stale_posting_sibling_ignored(self):
        from app.tasks.sync_helpers import find_blocking_sibling

        s = self._session()
        mine, vid = self._video_and_posts(s, "posting", sibling_age_h=3)
        assert find_blocking_sibling(s, mine, vid) is None

    def test_nonblocking_statuses_and_self(self):
        from app.tasks.sync_helpers import find_blocking_sibling

        s = self._session()
        for st in ("scheduled", "failed"):
            mine, vid = self._video_and_posts(s, st)
            assert find_blocking_sibling(s, mine, vid) is None, st
        # Self is never its own blocker.
        mine2, vid2 = self._video_and_posts(s, "failed")
        assert find_blocking_sibling(s, mine2, vid2) is None

    def test_async_mirror_matches_sync(self):
        import asyncio
        import datetime as dt

        async def go():
            from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

            from app.database import Base
            from app.models import Account, Post, PostStatus, Video
            from app.services import scheduler_service

            engine = create_async_engine("sqlite+aiosqlite://")
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as s:
                s.add_all([
                    Account(username="am1", password_enc="x"),
                    Video(original_filename="c.mp4", raw_path="/tmp/c.mp4", md5_hash="am1"),
                ])
                await s.flush()
                acc_id = (await s.execute(select(Account).where(Account.username == "am1"))).scalar_one().id
                vid_id = (await s.execute(select(Video).where(Video.md5_hash == "am1"))).scalar_one().id
                s.add(Post(
                    video_id=vid_id, account_id=acc_id, status=PostStatus.scheduled,
                    scheduled_for=dt.datetime.now(dt.timezone.utc),
                ))
                await s.commit()
                assert await scheduler_service.video_already_queued(s, vid_id) is True
                assert await scheduler_service.video_already_queued(s, vid_id + 999) is False
            await engine.dispose()

        asyncio.run(go())


class TestSessionCookieHelpers:
    def test_sanitize_strips_quotes_whitespace_and_decoding(self):
        from app.utils.instagram_helpers import sanitize_sessionid

        assert sanitize_sessionid('  "12345%3Aabcdef%20XYZ"  ') == "12345:abcdefXYZ"
        assert sanitize_sessionid("12345:abc def") == "12345:abcdef"
        assert sanitize_sessionid("") == ""

    def test_validity_mirrors_instagrapi_gate(self):
        from app.utils.instagram_helpers import sessionid_looks_valid, sessionid_owner_id

        good = "1234567890:" + "AbC123xYz" * 4
        assert sessionid_owner_id(good) == "1234567890"
        assert sessionid_looks_valid(good) is True
        assert sessionid_looks_valid("short") is False
        assert sessionid_looks_valid("no-leading-digits-here-xxxxxxxxxx") is False
        assert sessionid_looks_valid("") is False

    def test_parse_netscape_cookies_file(self):
        from app.utils.instagram_helpers import extract_sessionid, parse_cookies_file

        raw = (
            "# Netscape HTTP Cookie File\n"
            ".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\t12345%3AabcDEF12345678901234567890\n"
            ".instagram.com\tTRUE\t/\tTRUE\t0\tcsrftoken\tXYZ\n"
            "\n"
            "garbage-line-without-tabs\n"
        )
        cookies = parse_cookies_file(raw)
        assert cookies["sessionid"].startswith("12345")
        assert cookies["csrftoken"] == "XYZ"
        assert extract_sessionid(cookies) == "12345:abcDEF12345678901234567890"

    def test_parse_json_cookie_exports(self):
        from app.utils.instagram_helpers import extract_sessionid, parse_cookies_file

        flat = '{"sessionid": "999%3A' + "z" * 30 + '", "other": "1"}'
        assert extract_sessionid(parse_cookies_file(flat)) == "999:" + "z" * 30
        listed = '{"cookies": [{"name": "sessionid", "value": "777:' + "q" * 30 + '"}]}'
        assert extract_sessionid(parse_cookies_file(listed)) == "777:" + "q" * 30
        assert parse_cookies_file("") == {}
        assert parse_cookies_file("{not json") == {}

    def test_extract_rejects_truncated_cookie(self):
        from app.utils.instagram_helpers import extract_sessionid

        assert extract_sessionid({"sessionid": "12345:abc"}) is None
        assert extract_sessionid({}) is None


class TestTrendingAudio:
    def _mp3(self, tmp_path, name="trend.mp3"):
        p = tmp_path / name
        p.write_bytes(b"ID3" + b"\x00" * 1024)
        return str(p)

    def test_no_trending_keeps_legacy_command(self):
        cmd = build_command("in.mp4", "out.mp4")
        joined = " ".join(cmd)
        assert "amix" not in joined and "-map" in cmd and "0:a?" in cmd

    def test_mix_mode_loops_and_mixes(self, tmp_path):
        music = self._mp3(tmp_path)
        cmd = build_command("in.mp4", "out.mp4", trending_audio=music, music_volume=0.5, loop_audio_to=12.5)
        joined = " ".join(cmd)
        assert "-stream_loop" in cmd and "12.5" in cmd
        assert "amix=inputs=2:duration=first" in joined
        assert "[aout]" in joined and "volume=0.5" in joined

    def test_duck_mode_replaces_audio(self, tmp_path):
        music = self._mp3(tmp_path)
        cmd = build_command("in.mp4", "out.mp4", trending_audio=music, duck_original=True)
        joined = " ".join(cmd)
        assert "amix" not in joined and "volume=0.4,loudnorm" in joined

    def test_silent_video_gets_track_as_audio(self, tmp_path):
        music = self._mp3(tmp_path)
        cmd = build_command("in.mp4", "out.mp4", has_audio=False, trending_audio=music, loop_audio_to=8)
        joined = " ".join(cmd)
        assert "anullsrc" not in joined and "amix" not in joined

    def test_missing_file_silently_ignored(self):
        cmd = build_command("in.mp4", "out.mp4", trending_audio="/nope/missing.mp3")
        joined = " ".join(cmd)
        assert "amix" not in joined and "0:a?" in cmd

    def _audio_session(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from app.database import Base

        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        return sessionmaker(bind=engine)()

    def _seed_tracks(self, s, tmp_path):
        from app.models import AudioTrack

        live = AudioTrack(name="hit", file_path=self._mp3(tmp_path, "hit.mp3"), use_count=5)
        fresh = AudioTrack(name="new", file_path=self._mp3(tmp_path, "new.mp3"), use_count=0)
        off = AudioTrack(name="off", file_path=self._mp3(tmp_path, "off.mp3"), is_active=False)
        ghost = AudioTrack(name="ghost", file_path="/nope/gone.mp3")
        s.add_all([live, fresh, off, ghost])
        s.commit()

    def test_pick_prefers_usable_and_least_used(self, tmp_path):
        from app.tasks.sync_helpers import pick_audio

        s = self._audio_session()
        self._seed_tracks(s, tmp_path)
        seen = {pick_audio(s).name for _ in range(20)}
        # Only usable tracks ever surface; ghost/off never picked.
        assert seen <= {"hit", "new"}
        # Least-used dominates the weighting.
        assert sum(1 for _ in range(40) if pick_audio(s).name == "new") > 20

    def test_pick_empty_when_nothing_usable(self, tmp_path):
        from app.models import AudioTrack
        from app.tasks.sync_helpers import pick_audio

        s = self._audio_session()
        s.add(AudioTrack(name="gone", file_path="/nope/gone.mp3"))
        s.commit()
        assert pick_audio(s) is None

    def test_resolve_guards(self, tmp_path):
        from app.tasks.sync_helpers import resolve_audio

        s = self._audio_session()
        self._seed_tracks(s, tmp_path)
        hit = resolve_audio(s, "hit")
        assert hit is not None and hit.file_path.endswith("hit.mp3")
        assert resolve_audio(s, "off") is None
        assert resolve_audio(s, "ghost") is None
        assert resolve_audio(s, "nope") is None
        assert resolve_audio(s, "") is None
        assert resolve_audio(s, None) is None


class TestEffectiveDuration:
    def test_trim_window(self):
        from app.services.video_processor import effective_output_duration

        assert effective_output_duration(20.0, 2.0, 8.0) == 6.0
        assert effective_output_duration(20.0, None, 8.0) == 8.0

    def test_start_only_and_plain(self):
        from app.services.video_processor import effective_output_duration

        assert effective_output_duration(20.0, 5.0, None) == 15.0
        assert effective_output_duration(20.0, None, None) == 20.0

    def test_nonsense_trims_clamped(self):
        from app.services.video_processor import effective_output_duration

        assert effective_output_duration(20.0, 30.0, None) == 0.0
        assert effective_output_duration(20.0, 8.0, 5.0) == 12.0  # end<=start ignored


class TestValidateEncodeInputs:
    def _info(self, **kw):
        base = {"duration": 20.0, "width": 720, "height": 1280, "has_audio": True}
        base.update(kw)
        return base

    def test_happy_path_returns_out_len(self):
        from app.services.video_processor import validate_encode_inputs

        assert validate_encode_inputs(self._info(), 2.0, 8.0) == 6.0
        assert validate_encode_inputs(self._info(), None, None) == 20.0

    def test_short_and_streamless_rejected(self):
        import pytest

        from app.services.video_processor import validate_encode_inputs

        with pytest.raises(ValueError, match="too short"):
            validate_encode_inputs(self._info(duration=2.5), None, None)
        with pytest.raises(ValueError, match="no video stream"):
            validate_encode_inputs(self._info(width=0, height=0), None, None)

    def test_trim_past_eof_rejected_with_actionable_error(self):
        import pytest

        from app.services.video_processor import validate_encode_inputs

        # This used to die inside FFmpeg as "Could not open encoder
        # before EOF / frame= 0" with no hint at the trim.
        with pytest.raises(ValueError, match="past the end"):
            validate_encode_inputs(self._info(), 25.0, None)
        with pytest.raises(ValueError, match="past the end"):
            validate_encode_inputs(self._info(), 20.0, 30.0)

    def test_header_only_file_rejected(self):
        import pytest

        from app.services.video_processor import validate_encode_inputs

        # Streams listed but zero packets: passes every other check, then
        # dies in the encoder with "no packets". Fail fast instead.
        with pytest.raises(ValueError, match="zero video packets"):
            validate_encode_inputs(self._info(video_packets=0), None, None)
        # Unknown (probed without -count_packets) still passes.
        assert validate_encode_inputs(self._info(video_packets=None), None, None) == 20.0
        assert validate_encode_inputs(self._info(video_packets=483), None, None) == 20.0



class _FakeIGClient:
    """Stand-in for instagrapi.Client (no network). Records calls."""

    made: list = []

    def __init__(self, *a, **kw):
        self.calls: list = []
        self.settings: dict = {}
        self.fail_feed = 0
        self.feed_calls = 0
        self.request_timeout = kw.get("request_timeout", 1)
        _FakeIGClient.made.append(self)

    def set_device(self, d):
        self.calls.append(("set_device", d))

    def set_proxy(self, u):
        self.calls.append(("proxy", u))

    def load_settings(self, p):
        self.calls.append(("load", p))

    def get_timeline_feed(self):
        self.feed_calls += 1
        self.calls.append(("feed",))
        if self.feed_calls <= self.fail_feed:
            raise TimeoutError("boom")
        return {"ok": 1}

    def login(self, u, p):
        self.calls.append(("login", u))
        return True

    def dump_settings(self, p):
        self.calls.append(("dump", p))

    def account_edit(self, **kw):
        self.calls.append(("edit", kw))
        return True

    drop_fields: list = []

    def account_info(self):
        # Echoes the last edit (i.e. IG persisted everything), minus any
        # fields the test wants to simulate as silently dropped.
        self.calls.append(("info",))
        from types import SimpleNamespace

        edit = {}
        for c in self.calls:
            if c[0] == "edit":
                edit = c[1]
        d = {k: v for k, v in edit.items() if k not in _FakeIGClient.drop_fields}
        return SimpleNamespace(dict=lambda: d)

    def account_change_picture(self, p):
        self.calls.append(("pic", str(p)))
        return True

    trial_failures: int = 0

    def clip_upload(self, path, caption="", **kw):
        self.calls.append(("clip", dict(kw)))
        if kw.get("trial") and _FakeIGClient.trial_failures > 0:
            _FakeIGClient.trial_failures -= 1
            raise Exception("trial not eligible for this account")
        from types import SimpleNamespace

        return SimpleNamespace(id="111", pk="111", code="Dxyz")

    def account_set_private(self):
        self.calls.append(("private",))
        return True

    def account_set_public(self):
        self.calls.append(("public",))
        return True


class TestApplyProfile:
    def _svc(self, monkeypatch):
        import instagrapi

        from app.services.instagram_service import InstagramService

        _FakeIGClient.made.clear()
        monkeypatch.setattr(instagrapi, "Client", _FakeIGClient)
        monkeypatch.setattr("time.sleep", lambda s: None)
        return InstagramService()

    def test_full_apply_sends_only_set_fields(self, monkeypatch, tmp_path):
        pic = tmp_path / "pic.jpg"
        pic.write_bytes(b"\xff\xd8\xff" + b"\x00" * 100)
        svc = self._svc(monkeypatch)
        err = svc.apply_profile(
            "u", "p", biography="hey", external_url="https://t.me/x",
            full_name=" Brand ", make_private=True, picture_path=str(pic),
        )
        assert err == ""
        cl = _FakeIGClient.made[-1]
        kinds = [c[0] for c in cl.calls]
        assert "login" not in kinds  # healthy session reused
        edit = next(c[1] for c in cl.calls if c[0] == "edit")
        assert edit == {"biography": "hey", "external_url": "https://t.me/x", "full_name": "Brand"}
        assert ("pic", str(pic)) in cl.calls
        assert ("private",) in cl.calls and ("public",) not in kinds

    def test_empty_apply_is_noop_success(self, monkeypatch):
        svc = self._svc(monkeypatch)
        assert svc.apply_profile("u", "p") == ""
        cl = _FakeIGClient.made[-1]
        kinds = [c[0] for c in cl.calls]
        assert "feed" in kinds
        assert not ({"edit", "pic", "private", "public", "login"} & set(kinds))

    def test_silently_dropped_field_reported(self, monkeypatch):
        svc = self._svc(monkeypatch)
        _FakeIGClient.drop_fields = ["external_url"]
        try:
            err = svc.apply_profile("u", "p", biography="hey", external_url="https://t.me/x")
        finally:
            _FakeIGClient.drop_fields = []
        assert err.startswith("unpersisted") and "link" in err

    def test_missing_picture_fails_before_any_call(self, monkeypatch):
        svc = self._svc(monkeypatch)
        err = svc.apply_profile("u", "p", biography="x", picture_path="/nope/gone.jpg")
        assert err.startswith("picture:")
        assert _FakeIGClient.made == []

    def test_public_path_and_error_passthrough(self, monkeypatch):
        import instagrapi

        svc = self._svc(monkeypatch)
        assert svc.apply_profile("u", "p", make_private=False) == ""
        cl = _FakeIGClient.made[-1]
        assert ("public",) in [c if isinstance(c, tuple) and len(c) == 1 else c for c in cl.calls]

        class Boom(_FakeIGClient):
            def account_edit(self, **kw):
                raise Exception("challenge_required: verify")

        monkeypatch.setattr(instagrapi, "Client", Boom)
        err = svc.apply_profile("u", "p", biography="x")
        assert err.startswith("challenge")

    def test_dead_session_fails_without_login(self, monkeypatch):
        # A dead session must NEVER trigger an automatic password login —
        # that is the strongest automation signal. Fail closed; the admin
        # refreshes the session explicitly via the Accounts page.
        import instagrapi

        class Dead(_FakeIGClient):
            def get_timeline_feed(self):
                self.calls.append(("feed",))
                raise Exception("login_required: expired")

        monkeypatch.setattr(instagrapi, "Client", Dead)
        monkeypatch.setattr("time.sleep", lambda s: None)

        from app.services.instagram_service import InstagramService

        svc = InstagramService()
        err = svc.apply_profile("u", "p", biography="x")
        assert err.startswith("login_required")
        kinds = [c[0] for c in Dead.made[-1].calls]
        assert "login" not in kinds and "edit" not in kinds

    def test_remove_live_picture_dead_session_fails_without_login(self, monkeypatch):
        import instagrapi

        class Dead(_FakeIGClient):
            def get_timeline_feed(self):
                self.calls.append(("feed",))
                raise Exception("login_required: expired")

        monkeypatch.setattr(instagrapi, "Client", Dead)
        monkeypatch.setattr("time.sleep", lambda s: None)

        from app.services.instagram_service import InstagramService

        svc = InstagramService()
        err = svc.remove_live_picture("u", "p")
        assert err.startswith("login_required")
        kinds = [c[0] for c in Dead.made[-1].calls]
        assert "login" not in kinds


class TestAnonIngest:
    def test_classify_anon_error(self):
        from app.services.anon_ingest import classify_anon_error

        assert classify_anon_error(Exception("ERROR: Login required to access")) == "auth"
        assert classify_anon_error(Exception("Private account, login needed")) == "auth"
        assert classify_anon_error(Exception("HTTP Error 429: Too Many Requests")) == "throttled"
        assert classify_anon_error(Exception("timed out")) == "throttled"
        assert classify_anon_error(Exception("404: not found")) == "not-found"
        assert classify_anon_error(Exception("weird explosion")) == "generic"

    def _listing_payload(self, private=False):
        def node(sc, typename, product="", caption="cap"):
            return {"node": {
                "shortcode": sc, "__typename": typename,
                "is_video": typename == "GraphVideo",
                "product_type": product,
                "edge_media_to_caption": {"edges": [{"node": {"text": caption}}]},
            }}

        return {"data": {"user": {
            "is_private": private,
            "edge_owner_to_timeline_media": {"edges": [
                node("AAA", "GraphVideo", "clips", "reel one"),
                node("BBB", "GraphImage", "", "a photo"),
                node("CCC", "GraphVideo", "feed", "feed vid"),
            ]},
        }}}

    def test_list_public_posts_parses(self, monkeypatch):
        import requests

        from app.services.anon_ingest import list_public_posts

        seen = {}

        class Resp:
            status_code = 200

            def json(self):
                return self._payload

        def fake_get(url, params=None, headers=None, proxies=None, timeout=None):
            seen["params"] = params
            r = Resp()
            r._payload = self._listing_payload()
            return r

        monkeypatch.setattr(requests, "get", fake_get)
        items, err = list_public_posts("some.page", limit=10)
        assert err is None
        assert [i["shortcode"] for i in items] == ["AAA", "BBB", "CCC"]
        assert items[0]["is_video"] and items[0]["product_type"] == "clips"
        assert items[0]["caption"] == "reel one"
        assert not items[1]["is_video"]
        assert seen["params"] == {"username": "some.page"}

    def test_list_public_posts_private_and_429(self, monkeypatch):
        import requests

        from app.services.anon_ingest import list_public_posts

        class Resp:
            def __init__(self, code, payload=None):
                self.status_code = code
                self._payload = payload

            def json(self):
                return self._payload

        monkeypatch.setattr(requests, "get", lambda *a, **k: Resp(429))
        items, err = list_public_posts("x")
        assert items == [] and err.startswith("throttled:")

        payload = self._listing_payload(private=True)
        monkeypatch.setattr(requests, "get", lambda *a, **k: Resp(200, payload))
        items, err = list_public_posts("x")
        assert items == [] and err.startswith("private:")

    def test_download_post_with_fake_ydl(self, monkeypatch, tmp_path):
        import json

        import yt_dlp

        from app.services.anon_ingest import download_post

        d = tmp_path / "dl"
        d.mkdir()

        class FakeYDL:
            def __init__(self, params):
                self.params = params

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, url, download):
                assert "DcuQVFLvJMX" in url and download
                (d / "DcuQVFLvJMX.mp4").write_bytes(b"\x00" * 64)
                (d / "DcuQVFLvJMX.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 10)
                (d / "DcuQVFLvJMX.info.json").write_text(
                    json.dumps({"description": "hi 🎬 reels"}), encoding="utf-8")
                return {"id": "DcuQVFLvJMX"}

        monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
        res, err = download_post("DcuQVFLvJMX", str(d))
        assert err is None
        assert res["video"].endswith(".mp4") and res["cover"].endswith(".jpg")
        assert res["caption"] == "hi 🎬 reels"

    def test_download_post_classifies_auth_failure(self, monkeypatch, tmp_path):
        import yt_dlp

        from app.services.anon_ingest import download_post

        class BoomYDL:
            def __init__(self, params):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, url, download):
                raise Exception("ERROR: [instagram] Dxxx: Login required")

        monkeypatch.setattr(yt_dlp, "YoutubeDL", BoomYDL)
        res, err = download_post("Dxxx", str(tmp_path))
        assert res is None and err.startswith("auth:")

    def test_install_cover_moves_jpg_and_converts_other(self, monkeypatch, tmp_path):
        from app.tasks import source_tasks

        thumbs = tmp_path / "th"
        thumbs.mkdir()
        dirs = {"thumbnails": str(thumbs)}

        jpg = tmp_path / "c.jpg"
        jpg.write_bytes(b"\xff\xd8\xff" + b"\x00" * 10)
        got = source_tasks._install_cover(str(jpg), dirs)
        assert got and got.endswith(".jpg") and not jpg.exists()

        webp = tmp_path / "c.webp"
        webp.write_bytes(b"RIFF")
        monkeypatch.setattr(source_tasks, "_convert_cover",
                            lambda src, dst: (open(dst, "wb").write(b"x"), True)[1])
        got2 = source_tasks._install_cover(str(webp), dirs)
        assert got2 and got2.endswith(".jpg")

        assert source_tasks._install_cover(str(tmp_path / "nope.png"), dirs) is None


class TestHostStats:
    def test_host_snapshot_shape(self):
        from app.services.host_stats import host_snapshot

        s = host_snapshot()
        assert 0 <= s["cpu"]["total"] <= 100 * s["cpu"]["count"]
        assert len(s["cpu"]["per_core"]) == s["cpu"]["count"]
        assert s["mem"]["total"] > 0 and 0 <= s["mem"]["percent"] <= 100
        assert s["disk"] and all(d["total"] > 0 for d in s["disk"])
        assert s["net"]["sent"] >= 0 and s["uptime_s"] > 0

    def test_container_cpu_math(self):
        from app.services.host_stats import _container_cpu_percent

        stats = {
            "cpu_stats": {
                "cpu_usage": {"total_usage": 2_000_000_000, "percpu_usage": [0, 0]},
                "system_cpu_usage": 4_000_000_000, "online_cpus": 2,
            },
            "precpu_stats": {
                "cpu_usage": {"total_usage": 1_000_000_000},
                "system_cpu_usage": 2_000_000_000,
            },
        }
        assert _container_cpu_percent(stats) == 100.0
        assert _container_cpu_percent({}) == 0.0

    def test_container_snapshot_none_without_docker(self, monkeypatch):
        import sys

        from app.services import host_stats

        monkeypatch.setitem(sys.modules, "docker", None)
        assert host_stats.container_snapshot() is None
        snap = host_stats.full_snapshot()
        assert snap["docker"] is False and snap["containers"] == []


class TestPreviewTokens:
    def test_roundtrip_and_wrong_video_rejected(self):
        from app.core.security import create_preview_token, verify_preview_token

        tok = create_preview_token(42)
        assert verify_preview_token(tok, 42) is True
        assert verify_preview_token(tok, 43) is False
        assert verify_preview_token("garbage", 42) is False
        assert verify_preview_token(create_preview_token(7), 7) is True

    def test_access_token_is_not_a_preview_token(self):
        from app.core.security import create_access_token, verify_preview_token

        assert verify_preview_token(create_access_token("admin"), 1) is False


class TestMakeClientTimeout:
    def test_request_timeout_enforced_after_stale_session_load(self, monkeypatch, tmp_path):
        import json

        import instagrapi

        class StaleFile(_FakeIGClient):
            def load_settings(self, p):
                # Real init() restores request_timeout from the dumped file
                # (old instagrapi default was 1s) — our value must win after.
                self.settings = {"request_timeout": 1}
                self.request_timeout = 1
                self.calls.append(("load", p))

        monkeypatch.setattr(instagrapi, "Client", StaleFile)
        spath = tmp_path / "sess.json"
        spath.write_text(json.dumps({"request_timeout": 1}))

        from app.services.instagram_service import InstagramService

        svc = InstagramService(session_path=str(spath))
        assert svc._make_client("u").request_timeout == 30
        assert svc._make_client("u", request_timeout=60).request_timeout == 60


class TestRateLimitWiring:
    def test_login_brute_force_trips_429(self):
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as client:
            codes = [
                client.post(
                    "/api/v1/auth/login", json={"username": "nope", "password": "nope"}
                ).status_code
                for _ in range(7)
            ]
        assert 401 in codes  # wrong creds rejected...
        assert codes[-1] == 429  # ...and the 6th+ rapid hit is rate-limited

    def test_slowapi_middleware_installed(self):
        from slowapi.middleware import SlowAPIMiddleware

        from app.main import app

        assert any(
            getattr(m, "cls", None) is SlowAPIMiddleware for m in app.user_middleware
        )


class TestAnalyticsQueries:
    def test_account_comparison_runs(self):
        import asyncio
        import datetime as dt

        async def go():
            from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

            from app.database import Base
            from app.models import Account, Post, PostStatus, Video
            from app.services import analytics_service

            engine = create_async_engine("sqlite+aiosqlite://")
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as s:
                s.add(Account(username="cmp1", password_enc="x"))
                s.add(Video(original_filename="v.mp4", raw_path="/tmp/v.mp4", md5_hash="cmp1"))
                await s.flush()
                acc_id = (await s.execute(select(Account).where(Account.username == "cmp1"))).scalar_one().id
                vid_id = (await s.execute(select(Video).where(Video.md5_hash == "cmp1"))).scalar_one().id
                s.add(Post(
                    video_id=vid_id, account_id=acc_id, status=PostStatus.posted,
                    posted_at=dt.datetime.now(dt.timezone.utc),
                    views_7d=100, likes_7d=10, engagement_rate=10.0,
                ))
                await s.commit()
                rows = await analytics_service.account_comparison(s)
                assert rows and rows[0]["username"] == "cmp1"
                assert rows[0]["posts"] == 1 and rows[0]["views"] == 100
            await engine.dispose()

        asyncio.run(go())


class TestProxyImport:
    def _ok(self, line, default="http"):
        from app.services.proxy_service import parse_proxy_line

        spec, err = parse_proxy_line(line, default)
        assert err is None, (line, err)
        assert spec is not None
        return spec

    def _bad(self, line, default="http"):
        from app.services.proxy_service import parse_proxy_line

        spec, err = parse_proxy_line(line, default)
        assert spec is None and err not in (None, "blank/comment")
        return err

    def test_shapes(self):
        assert self._ok("1.2.3.4:8080") == (
            {"scheme": "http", "host": "1.2.3.4", "port": 8080,
             "username": "", "password": "", "country": ""})
        assert self._ok("socks5://1.2.3.4:1080")["scheme"] == "socks5"
        assert self._ok("https://1.2.3.4:443")["scheme"] == "https"
        assert self._ok("1.2.3.4:8080:u:p")["username"] == "u"
        assert self._ok("u:p@1.2.3.4:8080") == {
            "scheme": "http", "host": "1.2.3.4", "port": 8080,
            "username": "u", "password": "p", "country": ""}
        assert self._ok("socks5://u:p@h:1")["password"] == "p"
        assert self._ok("9.9.9.9:1080", "socks5")["scheme"] == "socks5"

    def test_country_suffix_comment_blank(self):
        from app.services.proxy_service import parse_proxy_line

        assert self._ok("1.2.3.4:8080 |de")["country"] == "DE"
        assert self._ok("1.2.3.4:8080 #us")["country"] == "US"
        assert parse_proxy_line("# just a comment") == (None, "blank/comment")
        assert parse_proxy_line("   ") == (None, "blank/comment")

    def test_rejections(self):
        assert self._bad("ftp://h:1").startswith("bad scheme")
        assert self._bad("nope") == "want host:port or host:port:user:pass"
        assert self._bad("h:99999") == "port out of range"
        assert self._bad("h:notaport") == "bad port"
        assert self._bad("h:1:u") == "want host:port or host:port:user:pass"
        assert self._bad("http://:8080") == "empty host"

    def test_fingerprint_normalizes(self):
        from app.services.proxy_service import proxy_fingerprint as f

        assert f("https", "H", 1) == f("http", "h", 1)
        assert f("http", "a", 1) != f("http", "a", 2)
        assert f("http", "a", 1) != f("socks5", "a", 1)


class TestProxyEngine:
    def _session(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from app.database import Base

        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        return sessionmaker(bind=engine)()

    def _proxy(self, s, url="http://1.1.1.1:8080", **kw):
        from app.models import Proxy, ProxyProtocol

        p = Proxy(url=url, protocol=ProxyProtocol.http, fail_count=0, **kw)
        s.add(p)
        s.commit()
        return p.id

    def test_looks_like_proxy_error(self):
        from app.tasks.sync_helpers import looks_like_proxy_error as looks

        assert looks("generic: ProxyError: tunnel failed") is True
        assert looks("generic: ConnectTimeout on POST") is True
        assert looks("throttled: slow down") is False
        assert looks("challenge_required: ...") is False
        assert looks("") is False

    def test_rank_prefers_clean_record(self):
        from app.tasks.sync_helpers import rank_spare_proxies as rank

        a = {"proxy": "flaky", "load": 0, "country": "DE", "latency": 10, "fail_count": 3}
        b = {"proxy": "clean", "load": 5, "country": "US", "latency": 900, "fail_count": 0}
        # Country still dominates (no geo-hop), but among peers the clean wins.
        assert rank([a, b], "DE") == "flaky"
        assert rank([a, b], "") == "clean"

    def test_shared_streak_disables_and_parks(self):
        from app.models import Account, Proxy
        from app.tasks.sync_helpers import MAX_PROXY_FAILS, record_proxy_check

        s = self._session()
        pid = self._proxy(s)
        acc = Account(username="parkme", password_enc="x", proxy_id=pid)
        s.add(acc)
        s.commit()
        disabled = False
        for _ in range(MAX_PROXY_FAILS):
            disabled = record_proxy_check(s, s.get(Proxy, pid), False, error="boom")
        assert disabled is True
        p = s.get(Proxy, pid)
        assert p is not None
        assert p.is_active is False and p.is_healthy is False
        assert p.last_error == "boom"
        a = s.get(Account, acc.id)
        assert a is not None and a.cooldown_until is not None
        # Success heals the streak for a re-enabled proxy.
        p.is_active = True
        s.commit()
        healed = s.get(Proxy, pid)
        assert healed is not None
        assert record_proxy_check(s, healed, True, latency_ms=42) is False
        p = s.get(Proxy, pid)
        assert p is not None
        assert (p.fail_count, p.is_healthy, p.latency_ms, p.last_error) == (0, True, 42, None)

    def test_resolve_proxy_object(self):
        from app.models import Proxy
        from app.tasks.sync_helpers import account_reachable, resolve_proxy, resolve_proxy_url

        s = self._session()
        pid = self._proxy(s)
        from app.models import Account

        acc = Account(username="r1", password_enc="x", proxy_id=pid)
        s.add(acc)
        s.commit()
        acc = s.get(Account, acc.id)
        assert isinstance(resolve_proxy(s, acc), Proxy)
        assert resolve_proxy_url(s, acc) == "http://1.1.1.1:8080"
        assert account_reachable(s, acc) is True


class TestProxyImportEndpoint:
    def test_bulk_import_dedupes_and_reports(self):
        import uuid

        from fastapi.testclient import TestClient

        from app.api.deps import get_current_admin
        from app.main import app

        tag = uuid.uuid4().hex[:10]
        text = (
            "# comment line\n"
            "\n"
            f"{tag}a.test:8080\n"
            f"socks5://{tag}b.test:1080 |DE\n"
            f"{tag}c.test:1:u:p\n"
            f"{tag}a.test:8080\n"
            "not a proxy!!!\n"
        )
        app.dependency_overrides[get_current_admin] = lambda: "admin"
        try:
            with TestClient(app) as client:
                r1 = client.post(
                    "/api/v1/proxies/import",
                    files={"file": ("list.txt", text, "text/plain")},
                    data={"default_protocol": "http"},
                )
                assert r1.status_code == 200, r1.text
                body = r1.json()
                assert body["added"] == 3, body
                assert body["duplicates_skipped"] == 1, body
                assert any(e["reason"] != "duplicate" for e in body["errors"])
                rows = client.get("/api/v1/proxies").json()
                mine = [p for p in rows if ".test:" in (p["url"] or "")]
                assert len(mine) == 3
                by_url = {p["url"]: p for p in mine}
                assert by_url[f"http://{tag}a.test:8080"]["username"] is None
                assert by_url[f"socks5://{tag}b.test:1080"]["country"] == "DE"
                assert by_url[f"http://{tag}c.test:1"]["username"] == "u"
                # Re-import is fully idempotent.
                r2 = client.post(
                    "/api/v1/proxies/import",
                    files={"file": ("list.txt", text, "text/plain")},
                    data={"default_protocol": "http"},
                )
                assert r2.json()["added"] == 0
        finally:
            app.dependency_overrides.clear()


class TestAutoPool:
    def _session(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from app.database import Base

        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        return sessionmaker(bind=engine)()

    def test_country_gate(self):
        from app.tasks.sync_helpers import pool_allows_country as gate

        assert gate("DE", "", "DE", True) is True
        assert gate("", "DE", "DE", True) is True  # source default counts
        assert gate("US", "", "DE", True) is False
        assert gate("", "", "DE", True) is False
        assert gate("US", "", "DE", False) is True  # not required: all pass
        assert gate("US", "", "", True) is True  # no pool country: all pass

    def test_get_setting(self):
        from app.models import Setting
        from app.tasks.sync_helpers import get_setting

        s = self._session()
        assert get_setting(s, "pool_country", "") == ""
        s.add(Setting(key="pool_country", value="DE"))
        s.commit()
        assert get_setting(s, "pool_country", "") == "DE"

    def test_purge_only_old_inactive_auto(self):
        import datetime as dt

        from app.models import Proxy, ProxyProtocol
        from app.tasks.sync_helpers import purge_stale_auto_proxies

        s = self._session()
        old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=10)
        fresh = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)

        def mk(url, source, active, checked):
            p = Proxy(url=url, protocol=ProxyProtocol.http, source=source,
                      is_active=active, is_healthy=False, fail_count=9,
                      last_checked=checked)
            s.add(p)
            return p

        mk("http://old-auto:1", "list-a", False, old)     # reaped: proven dead
        mk("http://manual:1", "manual", False, old)       # immortal
        mk("http://legacy:1", None, False, old)           # pre-feature rows immortal
        mk("http://fresh:1", "list-a", False, fresh)      # reaped: disabled+unhealthy needs no age gate
        mk("http://active:1", "list-a", True, old)        # active rows kept
        mk("http://new:1", "list-a", True, None)          # brand-new rows kept (still proving)
        s.commit()

        assert purge_stale_auto_proxies(s) == 2
        left = sorted(p.url for p in s.execute(select(Proxy)).scalars().all())
        assert left == [
            "http://active:1", "http://legacy:1",
            "http://manual:1", "http://new:1",
        ]


class TestCheckBatching:
    def _session(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from app.database import Base

        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        return sessionmaker(bind=engine)()

    def test_due_oldest_first_with_limit(self):
        import datetime as dt

        from app.models import Proxy, ProxyProtocol
        from app.tasks.sync_helpers import due_for_check

        s = self._session()
        now = dt.datetime.now(dt.timezone.utc)
        ids = {}
        for name, checked in (("old", now - dt.timedelta(hours=5)),
                              ("new", now - dt.timedelta(minutes=1)),
                              ("never", None)):
            p = Proxy(url=f"http://{name}:1", protocol=ProxyProtocol.http,
                      last_checked=checked)
            s.add(p)
            s.flush()
            ids[name] = p.id
        s.commit()
        assert due_for_check(s, 10) == [ids["never"], ids["old"], ids["new"]]
        assert due_for_check(s, 2) == [ids["never"], ids["old"]]

    def test_sweep_skips_e2e(self, monkeypatch):
        import socket
        from types import SimpleNamespace

        import httpx

        from app.services import proxy_service

        def _bomb(*a, **k):
            raise AssertionError("e2e must not run in sweep mode")

        monkeypatch.setattr(httpx, "Client", _bomb)

        class FakeSock:
            def close(self):
                pass

        monkeypatch.setattr(socket, "create_connection", lambda *a, **k: FakeSock())
        probe = SimpleNamespace(id=1, url="http://9.9.9.9:8080", username=None, password_enc=None)
        ok, ms = proxy_service.check_proxy_sync(probe, sweep_only=True)
        assert ok is True and isinstance(ms, int)

    def test_sweep_failure_is_tcp(self, monkeypatch):
        import socket
        from types import SimpleNamespace

        from app.services import proxy_service

        def _dead(*a, **k):
            raise OSError("unreachable")

        monkeypatch.setattr(socket, "create_connection", _dead)
        probe = SimpleNamespace(id=2, url="http://10.255.255.1:8080", username=None, password_enc=None)
        assert proxy_service.check_proxy_sync(probe, tcp_timeout=1, sweep_only=True) == (False, None)

    def test_full_path_still_verifies_e2e(self, monkeypatch):
        import socket
        from types import SimpleNamespace

        import httpx

        from app.services import proxy_service

        class FakeSock:
            def close(self):
                pass

        monkeypatch.setattr(socket, "create_connection", lambda *a, **k: FakeSock())

        seen = {}

        class FakeResp:
            status_code = 200

        class FakeClient:
            def __init__(self, **kw):
                seen.update(kw)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, *a, **k):
                return FakeResp()

            def close(self):
                pass

        monkeypatch.setattr(httpx, "Client", FakeClient)
        probe = SimpleNamespace(id=3, url="http://9.9.9.9:8080", username=None, password_enc=None)
        ok, ms = proxy_service.check_proxy_sync(probe)
        assert ok is True and isinstance(ms, int)


class TestPoolPolicy:
    def _session(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from app.database import Base

        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        return sessionmaker(bind=engine)()

    def test_purge_honors_custom_age(self):
        import datetime as dt

        from app.models import Proxy, ProxyProtocol
        from app.tasks.sync_helpers import purge_stale_auto_proxies

        s = self._session()
        old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)
        # Proven dead (disabled+unhealthy): gone even under a 30-day policy.
        s.add(Proxy(url="http://gone:1", protocol=ProxyProtocol.http, source="x",
                    is_active=False, is_healthy=False, fail_count=9, last_checked=old))
        # Disabled but last known healthy: only the retention arm takes it.
        s.add(Proxy(url="http://retired:1", protocol=ProxyProtocol.http, source="x",
                    is_active=False, is_healthy=True, fail_count=0, last_checked=old))
        s.commit()
        assert purge_stale_auto_proxies(s, max_age_days=30) == 1
        assert purge_stale_auto_proxies(s, max_age_days=1) == 1

    def test_purge_unlinks_parked_accounts_and_spares_manual(self):
        import datetime as dt

        from app.models import Account, Proxy, ProxyProtocol
        from app.tasks.sync_helpers import purge_stale_auto_proxies

        s = self._session()
        old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)
        dead = Proxy(url="http://dead:1", protocol=ProxyProtocol.http, source="x",
                     is_active=False, is_healthy=False, fail_count=5,
                     created_at=old, last_checked=old)
        manual = Proxy(url="http://manual:1", protocol=ProxyProtocol.http, source="manual",
                       is_active=False, is_healthy=False, fail_count=5,
                       created_at=old, last_checked=old)
        s.add_all([dead, manual])
        s.flush()
        s.add(Account(username="parked", password_enc="x", proxy_id=dead.id))
        s.commit()

        assert purge_stale_auto_proxies(s) == 1
        left = sorted(p.url for p in s.execute(select(Proxy)).scalars().all())
        assert left == ["http://manual:1"]
        acc = s.execute(select(Account)).scalar_one()
        assert acc.proxy_id is None

    def test_purge_reaps_stillborn_but_keeps_proving(self):
        import datetime as dt

        from app.models import Proxy, ProxyProtocol
        from app.tasks.sync_helpers import purge_stale_auto_proxies

        s = self._session()
        now = dt.datetime.now(dt.timezone.utc)
        old = now - dt.timedelta(days=3)
        young = now - dt.timedelta(hours=1)

        def mk(url, source, active, healthy, fails, created, checked):
            p = Proxy(url=url, protocol=ProxyProtocol.http, source=source,
                      is_active=active, is_healthy=healthy, fail_count=fails,
                      created_at=created, last_checked=checked)
            s.add(p)
        mk("http://sick-old:1", "list-a", True, False, 3, old, young)    # reaped: never proved itself
        mk("http://sick-young:1", "list-a", True, False, 3, young, young)  # kept: still proving
        mk("http://good-old:1", "list-a", True, True, 0, old, young)       # kept: proven
        mk("http://manual-old:1", "manual", True, False, 3, old, young)    # kept: immortal
        s.commit()

        assert purge_stale_auto_proxies(s) == 1
        left = sorted(p.url for p in s.execute(select(Proxy)).scalars().all())
        assert left == ["http://good-old:1", "http://manual-old:1", "http://sick-young:1"]

    def test_cap_auto_pool_deletes_worst_first(self):
        from app.models import Proxy, ProxyProtocol
        from app.tasks.sync_helpers import cap_auto_pool

        s = self._session()

        def mk(url, source, active, fails):
            s.add(Proxy(url=url, protocol=ProxyProtocol.http, source=source,
                        is_active=active, is_healthy=not active, fail_count=fails))
        mk("http://dead:1", "x", False, 9)
        mk("http://sick:1", "x", True, 4)
        mk("http://good:1", "x", True, 0)
        mk("http://manual:1", "manual", False, 9)
        s.commit()

        assert cap_auto_pool(s, max_auto=2) == 1
        left = sorted(p.url for p in s.execute(select(Proxy)).scalars().all())
        assert left == ["http://good:1", "http://manual:1", "http://sick:1"]

    def test_pick_spare_skips_unchecked(self):
        import datetime as dt

        from app.models import Proxy, ProxyProtocol
        from app.tasks.sync_helpers import pick_spare_proxy

        s = self._session()
        p = Proxy(url="http://fresh:8080", protocol=ProxyProtocol.http,
                  is_active=True, is_healthy=True, fail_count=0)
        s.add(p)
        s.commit()
        pid = p.id
        # Healthy-looking but never checked: not routable.
        assert pick_spare_proxy(s) is None
        p.last_checked = dt.datetime.now(dt.timezone.utc)
        s.commit()
        picked = pick_spare_proxy(s)
        assert picked is not None and picked.id == pid

    def test_check_all_threaded_completes(self, monkeypatch, tmp_path):
        import app.database as dbmod
        from sqlalchemy import create_engine, select
        from sqlalchemy.orm import sessionmaker

        from app.database import Base
        from app.models import Proxy, ProxyProtocol
        from app.services import proxy_service
        from app.tasks import periodic_tasks

        engine = create_engine(f"sqlite:///{tmp_path}/check.db")
        Base.metadata.create_all(engine)
        maker = sessionmaker(bind=engine)
        monkeypatch.setattr(dbmod, "SyncSessionLocal", maker)

        s = maker()
        bad = Proxy(url="http://bad:1", protocol=ProxyProtocol.http, source="x")
        g1 = Proxy(url="http://good1:1", protocol=ProxyProtocol.http, source="x")
        g2 = Proxy(url="http://good2:1", protocol=ProxyProtocol.http, source="x")
        s.add_all([bad, g1, g2])
        s.commit()
        bad_id, g1_id = bad.id, g1.id

        def fake_check(probe, tcp_timeout=10, e2e_timeout=15, sweep_only=False):
            if probe.id == bad_id:
                return False, None
            return True, 7

        monkeypatch.setattr(proxy_service, "check_proxy_sync", fake_check)

        assert periodic_tasks.check_all_proxies() == {"swept": 3, "verified": 2}
        # Fresh session: this test's own session cached pre-task state.
        s.close()
        s = maker()
        rows = {p.url: p for p in s.execute(select(Proxy)).scalars().all()}
        assert (rows["http://bad:1"].is_healthy, rows["http://bad:1"].fail_count) == (False, 1)
        assert rows["http://bad:1"].last_error == "tcp sweep failed"
        for url in ("http://good1:1", "http://good2:1"):
            assert (rows[url].is_healthy, rows[url].fail_count, rows[url].latency_ms) == (True, 0, 7)
        assert rows["http://good1:1"].id == g1_id

    def test_pool_defaults_shipped(self):
        from app.api.system import DEFAULT_SETTINGS

        assert DEFAULT_SETTINGS["pool_country"] == ("", "proxy")
        assert DEFAULT_SETTINGS["pool_require_country"] == ("false", "proxy")
        assert DEFAULT_SETTINGS["pool_purge_after_days"] == ("7", "proxy")
        assert DEFAULT_SETTINGS["pool_stillborn_hours"] == ("48", "proxy")


class TestCheckSessionReason:
    def _svc(self, monkeypatch, feed_impl):
        import instagrapi

        from app.services.instagram_service import InstagramService

        _FakeIGClient.made.clear()

        class FeedFake(_FakeIGClient):
            def get_timeline_feed(self):
                self.calls.append(("feed",))
                return feed_impl(self)

        monkeypatch.setattr(instagrapi, "Client", FeedFake)
        monkeypatch.setattr("time.sleep", lambda s: None)
        return InstagramService()

    def test_valid(self, monkeypatch):
        svc = self._svc(monkeypatch, lambda self: {"ok": 1})
        assert svc.check_session("u") == (True, "ok")

    def test_kinds_carry_hints(self, monkeypatch):
        cases = [
            (Exception("login_required: expired"), "login_required", "refresh"),
            (Exception("challenge_required: verify"), "challenge", "verification"),
            (Exception("throttled: slow down"), "throttled", "rate-limited"),
            (TimeoutError("timed out"), "generic", "proxy"),
        ]
        for exc, kind, hint_word in cases:
            def _raise(self, _e=exc):
                raise _e

            svc = self._svc(monkeypatch, _raise)
            ok, reason = svc.check_session("u")
            assert ok is False and reason.startswith(kind) and hint_word in reason


class TestTrialUpload:
    def _svc(self, monkeypatch):
        import instagrapi

        from app.services.instagram_service import InstagramService

        _FakeIGClient.made.clear()
        _FakeIGClient.trial_failures = 0
        monkeypatch.setattr(instagrapi, "Client", _FakeIGClient)
        monkeypatch.setattr("time.sleep", lambda s: None)
        return InstagramService()

    def test_trial_passthrough(self, monkeypatch):
        svc = self._svc(monkeypatch)
        mid, url, err = svc.upload_reel("u", "p", "v.mp4", "cap", trial=True, trial_strategy="auto")
        assert err == "" and mid == "111" and url == "https://www.instagram.com/reel/Dxyz/"
        clips = [c[1] for c in _FakeIGClient.made[-1].calls if c[0] == "clip"]
        assert clips == [{"trial": True, "trial_graduation_strategy": "auto", "thumbnail": None}]

    def test_regular_untouched(self, monkeypatch):
        svc = self._svc(monkeypatch)
        mid, _, err = svc.upload_reel("u", "p", "v.mp4", "cap")
        assert err == "" and mid == "111"
        clips = [c[1] for c in _FakeIGClient.made[-1].calls if c[0] == "clip"]
        assert clips == [{"thumbnail": None}]

    def test_thumbnail_path_forwarded(self, monkeypatch):
        from pathlib import Path

        svc = self._svc(monkeypatch)
        mid, _, err = svc.upload_reel("u", "p", "v.mp4", "cap", thumbnail_path="/tmp/cover.jpg")
        assert err == "" and mid == "111"
        clips = [c[1] for c in _FakeIGClient.made[-1].calls if c[0] == "clip"]
        assert clips == [{"thumbnail": Path("/tmp/cover.jpg")}]

    def test_trial_rejection_falls_back_once(self, monkeypatch):
        svc = self._svc(monkeypatch)
        _FakeIGClient.trial_failures = 1
        mid, _, err = svc.upload_reel("u", "p", "v.mp4", "cap", trial=True)
        assert err == "" and mid == "111"
        clips = [c[1] for c in _FakeIGClient.made[-1].calls if c[0] == "clip"]
        # First attempt trial, fallback regular — exactly two uploads, one post.
        assert clips[0].get("trial") is True and clips[1] == {"thumbnail": None}

    def test_idless_upload_never_marked_posted(self, monkeypatch):
        import instagrapi

        svc = self._svc(monkeypatch)

        class Ghost(_FakeIGClient):
            def clip_upload(self, path, caption="", **kw):
                from types import SimpleNamespace

                self.calls.append(("clip", dict(kw)))
                return SimpleNamespace()  # no id/pk/code

        monkeypatch.setattr(instagrapi, "Client", Ghost)
        mid, url, err = svc.upload_reel("u", "p", "v.mp4", "cap")
        assert mid is None and url is None and err.startswith("generic")

    def test_non_trial_error_no_fallback(self, monkeypatch):
        import instagrapi

        svc = self._svc(monkeypatch)

        class Boom(_FakeIGClient):
            def clip_upload(self, path, caption="", **kw):
                self.calls.append(("clip", dict(kw)))
                raise Exception("throttled: slow down")

        monkeypatch.setattr(instagrapi, "Client", Boom)
        mid, _, err = svc.upload_reel("u", "p", "v.mp4", "cap", trial=True)
        assert mid is None and err.startswith("throttled")
        assert len([c for c in Boom.made[-1].calls if c[0] == "clip"]) == 1

    def test_dead_session_never_triggers_login(self, monkeypatch):
        # The critical unattended path: execute_post retries, so an
        # automatic fresh login on a dead session would become a login
        # storm (up to 4 password logins in ~7 min) — the strongest
        # automation signal Instagram sees. Fail closed instead.
        import instagrapi

        class Dead(_FakeIGClient):
            def get_timeline_feed(self):
                self.calls.append(("feed",))
                raise Exception("login_required: expired")

        monkeypatch.setattr(instagrapi, "Client", Dead)
        monkeypatch.setattr("time.sleep", lambda s: None)

        from app.services.instagram_service import InstagramService

        svc = InstagramService()
        mid, url, err = svc.upload_reel("u", "p", "v.mp4", "cap")
        assert mid is None and url is None and err.startswith("login_required")
        kinds = [c[0] for c in Dead.made[-1].calls]
        assert "login" not in kinds and "clip" not in kinds


class TestScheduleAutofill:
    def test_empty_text_gets_weighted_autofill(self):
        import asyncio

        async def go():
            from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

            from fastapi.testclient import TestClient

            from app.api.deps import get_current_admin, get_db
            from app.database import Base
            from app.main import app
            from app.models import Account, CaptionTemplate, HashtagSet, Video, VideoStatus

            engine = create_async_engine("sqlite+aiosqlite://")
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            maker = async_sessionmaker(engine, expire_on_commit=False)

            async def override_db():
                async with maker() as s:
                    yield s

            app.dependency_overrides[get_current_admin] = lambda: "admin"
            app.dependency_overrides[get_db] = override_db
            try:
                async with maker() as s:
                    s.add(Account(username="sn1", password_enc="x"))
                    s.add(Video(original_filename="s.mp4", raw_path="/tmp/s.mp4",
                                md5_hash="sn1", status=VideoStatus.processed))
                    s.add(CaptionTemplate(name="c1", content="Hello world"))
                    s.add(HashtagSet(name="h1", tags="#a, #b, #c"))
                    await s.commit()
                    acc_id = (await s.execute(select(Account).where(Account.username == "sn1"))).scalar_one().id
                    vid_id = (await s.execute(select(Video).where(Video.md5_hash == "sn1"))).scalar_one().id
                with TestClient(app) as client:
                    r = client.post("/api/v1/posts/schedule",
                                    json={"video_id": vid_id, "account_id": acc_id})
                    assert r.status_code == 201, r.text
                    body = r.json()
                    assert body["caption"] == "Hello world"
                    assert all(t in body["hashtags"] for t in ("#a", "#b", "#c"))
                    # Explicit text is respected, not overwritten.
                    r2 = client.post("/api/v1/posts/schedule",
                                     json={"video_id": vid_id, "account_id": acc_id,
                                           "caption": "Mine", "hashtags": "#z"})
                    assert r2.status_code == 400  # already queued by the first call
                async with maker() as s:
                    cap = (await s.execute(select(CaptionTemplate).where(CaptionTemplate.name == "c1"))).scalar_one()
                    assert cap.use_count == 1
            finally:
                app.dependency_overrides.clear()
                await engine.dispose()

        asyncio.run(go())


class TestSchedulerEdges:
    def _session(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from app.database import Base

        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        return sessionmaker(bind=engine)()

    def test_due_rules_matching(self):
        import datetime as dt

        from app.models import ScheduleRule
        from app.tasks import sync_helpers as sched

        s = self._session()
        now = dt.datetime.now(dt.timezone.utc)
        s.add_all([
            ScheduleRule(name="every", day_of_week=-1, hour=now.hour, minute=now.minute),
            ScheduleRule(name="off", day_of_week=-1, hour=now.hour, minute=now.minute, is_active=False),
            ScheduleRule(name="other-hour", day_of_week=-1,
                         hour=(now.hour + 5) % 24, minute=now.minute),
        ])
        s.commit()
        assert [r.name for r in sched.due_rules(s, now)] == ["every"]

    def test_next_video_preference_and_fallback(self):
        from app.models import Video, VideoStatus
        from app.tasks import sync_helpers as sched

        s = self._session()
        s.add_all([
            Video(original_filename="o.mp4", raw_path="/tmp/o.mp4", md5_hash="n1",
                  status=VideoStatus.processed, effect_preset="plain"),
            Video(original_filename="n.mp4", raw_path="/tmp/n.mp4", md5_hash="n2",
                  status=VideoStatus.processed, effect_preset="cine"),
        ])
        s.commit()

        def _preset(effect):
            v = sched.next_video(s, effect)
            assert v is not None
            return v.effect_preset

        assert _preset("cine") == "cine"
        assert _preset("nope") == "plain"
        assert _preset(None) == "plain"

    def test_already_scheduled_window(self):
        import datetime as dt

        from app.models import Account, Post, PostStatus, ScheduleRule, Video, VideoStatus
        from app.tasks import sync_helpers as sched

        s = self._session()
        s.add(Account(username="w1", password_enc="x"))
        s.add(Video(original_filename="w.mp4", raw_path="/tmp/w.mp4", md5_hash="w1",
                    status=VideoStatus.processed))
        s.add(ScheduleRule(name="r", day_of_week=-1, hour=1, minute=2, account_id=1))
        s.flush()
        s.add(Post(video_id=1, account_id=1, status=PostStatus.scheduled,
                   scheduled_for=dt.datetime.now(dt.timezone.utc)))
        s.commit()
        rule = s.get(ScheduleRule, 1)
        assert rule is not None
        assert sched.already_scheduled(s, rule) is True
        # video_already_queued keys on video_id (the post targets video 1):
        assert sched.video_already_queued(s, 1) is True
        assert sched.video_already_queued(s, 999) is False

    def test_pick_hashtags_blank_safe(self):
        # Schema is NOT NULL, so None is unconstructible — whitespace-only
        # rows are the real-world empty case (plus the None-tolerant guard).
        from app.models import HashtagSet
        from app.tasks import sync_helpers as sched

        s = self._session()
        s.add(HashtagSet(name="empty", tags="  "))
        s.add(HashtagSet(name="commas", tags=",, ,"))
        s.commit()
        assert sched.pick_hashtags(s) == ""
        s.add(HashtagSet(name="good", tags="#a, #b, #c, #d"))
        s.commit()
        got = sched.pick_hashtags(s)
        assert 3 <= len(got.split()) <= 5 and all(t.startswith("#") for t in got.split())

    def test_eligible_defers_warm_account(self):
        import datetime as dt

        from app.models import Account, AccountStatus
        from app.tasks import sync_helpers as sched

        s = self._session()
        now = dt.datetime.now(dt.timezone.utc)
        young = Account(username="young", password_enc="x", posts_today=1, max_daily_posts=3)
        old = Account(username="old", password_enc="x", posts_today=0, max_daily_posts=3)
        s.add_all([young, old])
        s.flush()
        # Backdate created_at past warm-up for the old one.
        old.created_at = now - dt.timedelta(days=30)
        s.commit()
        pick = sched.eligible_account(s, None)
        assert pick is not None and pick.username == "old"

    def test_as_aware_utc(self):
        import datetime as dt

        from app.tasks.sync_helpers import as_aware_utc

        assert as_aware_utc(None) is None
        naive = dt.datetime(2026, 1, 1, 12, 0)
        out = as_aware_utc(naive)
        assert out is not None and out.tzinfo == dt.timezone.utc
        aware = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.timezone.utc)
        assert as_aware_utc(aware) == aware

    def test_media_dirs_created(self, tmp_path, monkeypatch):
        from app.config import settings
        from app.services.video_processor import media_dirs

        monkeypatch.setattr(settings, "MEDIA_ROOT", str(tmp_path / "m"))
        dirs = media_dirs()
        assert all(os.path.isdir(v) for v in dirs.values())
        assert set(dirs) >= {"raw", "processed", "thumbnails", "audio", "profile_pics"}


class TestSecurityUnits:
    def test_jwt_cycle_and_rejections(self):
        import datetime as dt

        import jwt

        from app.config import settings
        from app.core.security import create_access_token, create_refresh_token, decode_token

        tok = create_access_token("admin")
        assert decode_token(tok) == "admin"
        ref = create_refresh_token("admin")
        assert decode_token(ref, expected_type="refresh") == "admin"
        try:
            decode_token(ref)
            raise AssertionError("must reject")
        except ValueError as e:
            assert "type" in str(e).lower()
        expired = jwt.encode(
            {"sub": "x", "type": "access", "exp": dt.datetime(2000, 1, 1)},
            settings.SECRET_KEY, algorithm="HS256")
        try:
            decode_token(expired)
            raise AssertionError("must reject")
        except ValueError as e:
            assert "expired" in str(e).lower()
        nosub = jwt.encode({"type": "access"}, settings.SECRET_KEY, algorithm="HS256")
        try:
            decode_token(nosub)
            raise AssertionError("must reject")
        except ValueError:
            pass

    def test_password_and_legacy_decrypt(self):
        from app.core.security import decrypt_secret, encrypt_secret, hash_password, verify_password

        h = hash_password("pw")
        assert verify_password("pw", h) is True
        assert verify_password("nope", h) is False
        assert verify_password("pw", "not-a-hash") is False
        assert decrypt_secret(encrypt_secret("s3")) == "s3"
        assert decrypt_secret("plaintext-legacy") == "plaintext-legacy"
        assert decrypt_secret(None) is None and encrypt_secret(None) is None


class TestCrypto:
    def test_roundtrip(self):
        token = encrypt_secret("s3cret")
        assert token != "s3cret"
        assert decrypt_secret(token) == "s3cret"

    def test_empty_passthrough(self):
        assert encrypt_secret(None) is None
        assert decrypt_secret(None) is None


class TestShouldTakeMedia:
    def test_gates(self):
        from app.tasks.source_tasks import should_take_media

        assert should_take_media(2, "clips", True) == (True, "")
        assert should_take_media(2, "feed", True)[0] is False
        assert should_take_media(2, "feed", False) == (True, "")
        assert should_take_media(2, "", False) == (True, "")
        assert should_take_media(1, "", False)[0] is False
        assert should_take_media(8, "", False)[0] is False
        assert should_take_media(99, "", False)[0] is False


class TestPersistedMismatches:
    def test_all_saved(self):
        from app.services.instagram_service import _persisted_mismatches

        sent = {"biography": "hi", "external_url": "https://t.me/x", "full_name": "Brand"}
        actual = {"biography": "hi", "external_url": "https://t.me/x", "full_name": "Brand"}
        assert _persisted_mismatches(sent, actual) == []

    def test_silently_dropped_link(self):
        from app.services.instagram_service import _persisted_mismatches

        sent = {"external_url": "https://t.me/x"}
        assert _persisted_mismatches(sent, {"external_url": ""}) == ["link"]
        assert _persisted_mismatches(sent, {}) == ["link"]

    def test_tolerates_slash_and_truncation(self):
        from app.services.instagram_service import _persisted_mismatches

        assert _persisted_mismatches(
            {"external_url": "https://t.me/x"}, {"external_url": "https://t.me/x/"}
        ) == []
        assert _persisted_mismatches({"full_name": "n" * 100}, {"full_name": "n" * 64}) == []

    def test_unsent_sections_never_flagged(self):
        from app.services.instagram_service import _persisted_mismatches

        assert _persisted_mismatches({}, {"biography": "whatever"}) == []
        assert _persisted_mismatches({"biography": "hi"}, {"biography": "other"}) == ["biography"]


class TestAccountHealth:
    def test_healthy_account_scores_high(self):
        from app.services.account_health import compute_health

        out = compute_health(status="active")
        assert out["score"] == 100 and out["level"] == "healthy"

    def test_challenge_is_critical(self):
        from app.services.account_health import compute_health

        out = compute_health(status="challenge_required")
        assert out["level"] == "critical" and out["score"] <= 10

    def test_banned_and_disabled_zero(self):
        from app.services.account_health import compute_health

        assert compute_health(status="banned")["score"] == 0
        assert compute_health(status="disabled")["score"] == 0

    def test_cooldown_drops_below_healthy(self):
        import datetime as dt

        from app.services.account_health import compute_health

        out = compute_health(
            status="cooldown",
            cooldown_until=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=3),
        )
        assert out["score"] < 70 and any("cool" in r for r in out["reasons"])

    def test_dead_proxy_costs_25(self):
        from app.services.account_health import compute_health

        out = compute_health(status="active", proxy_fail_count=6, proxy_healthy=False)
        assert out["score"] == 75 and any("proxy" in r for r in out["reasons"])

    def test_failure_streak_and_rate(self):
        from app.services.account_health import compute_health

        # streak 4 -> -40, fail-rate 4/5 -> -20: exactly 40 = watch floor.
        out = compute_health(status="active", fail_streak=4, failed_7d=4, posted_7d=1)
        assert out["score"] == 40 and out["level"] == "watch"
        assert any("consecutive" in r for r in out["reasons"])

    def test_daily_cap_is_note_not_penalty(self):
        from app.services.account_health import compute_health

        out = compute_health(status="active", posts_today=3, daily_cap=3)
        assert out["score"] == 100 and any("cap" in r for r in out["reasons"])

    def test_expired_cooldown_not_scarred(self):
        import datetime as dt

        from app.services.account_health import compute_health

        out = compute_health(
            status="cooldown",
            cooldown_until=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1),
        )
        assert out["score"] == 90 and out["level"] == "healthy"
        assert any("expired" in r for r in out["reasons"])

    def test_summarize_statuses_shared_core(self):
        from app.models import PostStatus
        from app.services.account_health import summarize_statuses

        assert summarize_statuses([]) == (0, 0, 0)
        f, s, p = PostStatus.failed, PostStatus.scheduled, PostStatus.posted
        assert summarize_statuses([f, f, p, f]) == (2, 3, 1)
        assert summarize_statuses([p, f]) == (0, 1, 1)


class TestMaybePark:
    def _session(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from app.database import Base

        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        return sessionmaker(bind=engine)()

    def _account_with_fails(self, s, n_fail, tag, status="active"):
        import datetime as dt

        from app.models import Account, AccountStatus, Post, PostStatus, Video, VideoStatus

        acc = Account(username=f"h{tag}", password_enc="x", status=AccountStatus(status))
        s.add(acc)
        s.flush()
        vid = Video(original_filename="v.mp4", raw_path="/tmp/v.mp4",
                    md5_hash=f"mh{tag}", status=VideoStatus.processed)
        s.add(vid)
        s.flush()
        for _ in range(n_fail):
            s.add(Post(video_id=vid.id, account_id=acc.id, status=PostStatus.failed,
                       created_at=dt.datetime.now(dt.timezone.utc)))
        s.commit()
        return acc

    def test_parks_after_five_consecutive_fails(self):
        from app.models import AccountStatus
        from app.services.account_health import maybe_park_account_sync

        s = self._session()
        acc = self._account_with_fails(s, 5, "park5")
        assert maybe_park_account_sync(s, acc) is True
        assert acc.status == AccountStatus.cooldown and acc.cooldown_until is not None
        s.rollback()

    def test_no_park_below_threshold(self):
        from app.services.account_health import maybe_park_account_sync

        s = self._session()
        acc = self._account_with_fails(s, 3, "park3")
        assert maybe_park_account_sync(s, acc) is False

    def test_never_fights_existing_state(self):
        from app.services.account_health import maybe_park_account_sync

        s = self._session()
        acc = self._account_with_fails(s, 9, "park9", status="cooldown")
        assert maybe_park_account_sync(s, acc) is False


class TestMediaHash:
    def test_identical_images_same_hash(self):
        from PIL import Image

        from app.services.media_hash import dhash_hex, hamming

        a = Image.new("RGB", (64, 64), (10, 20, 30))
        b = Image.new("RGB", (64, 64), (10, 20, 30))
        assert dhash_hex(a) == dhash_hex(b) and hamming(dhash_hex(a), dhash_hex(b)) == 0

    def test_different_images_differ(self):
        from PIL import Image

        from app.services.media_hash import dhash_hex, hamming

        # Flat images hash degenerately (all-zero) — use a decreasing
        # gradient: every adjacent comparison is true -> all 64 bits set.
        flat = Image.new("RGB", (64, 64), (128, 128, 128))
        grad = Image.new("RGB", (64, 64))
        px = grad.load()
        assert px is not None
        for x in range(64):
            for y in range(64):
                v = 255 - x * 4
                px[x, y] = (v, v, v)
        assert hamming(dhash_hex(flat), dhash_hex(grad)) == 64

    def test_hamming_known_value(self):
        from app.services.media_hash import hamming

        assert hamming("ff00ff00ff00ff00", "ff00ff00ff00ff00") == 0
        assert hamming("ffffffffffffffff", "0000000000000000") == 64

    def test_frame_hash_never_raises(self):
        from app.services.media_hash import frame_hash

        assert frame_hash("/nonexistent/path.mp4") is None

    def test_normalize_caption(self):
        from app.services.media_hash import normalize_caption

        assert normalize_caption("Hello  #World\nNew") == "hello new"
        assert normalize_caption(None) == "" and normalize_caption("  ") == ""

    def test_find_near_and_caption_duplicates(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from app.database import Base
        from app.models import Video, VideoStatus
        from app.services.media_hash import find_caption_duplicate, find_near_duplicate

        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        s = sessionmaker(bind=engine)()
        s.add(Video(original_filename="a.mp4", raw_path="/tmp/a.mp4", md5_hash="qa",
                    status=VideoStatus.processed, phash="aaaaaaaaaaaaaaaa",
                    source_caption="Sunset vibes over the city #reels"))
        s.commit()
        hit = find_near_duplicate(s, "aaaaaaaaaaaaaaab")  # 1 bit off
        assert hit is not None and hit.original_filename == "a.mp4"
        assert find_near_duplicate(s, "0000000000000000") is None
        cap = find_caption_duplicate(s, "sunset VIBES over the city #other")
        assert cap is not None
        assert find_caption_duplicate(s, "totally different words here now") is None
        assert find_caption_duplicate(s, "short") is None


class TestBestSlots:
    def test_ranking_by_avg_views(self):
        from app.services.best_slots import aggregate_slots

        slots = aggregate_slots([(18, 100), (18, 200), (9, 50), (21, 1000)])
        assert [s["hour_utc"] for s in slots] == [21, 18, 9]
        assert slots[0]["avg_views"] == 1000 and slots[0]["local"] == "21:00"

    def test_local_conversion_uses_schedule_tz(self, monkeypatch):
        from app.config import settings
        from app.services.best_slots import local_hour, local_label

        monkeypatch.setattr(settings, "SCHEDULE_TZ", "Asia/Tehran")
        assert local_label(18) == "21:30" and local_label(0) == "03:30"
        assert local_label(20, 30) == "00:00"
        assert local_hour(18) == 21 and local_hour(21) == 0

    def test_limit_respected(self):
        from app.services.best_slots import aggregate_slots

        assert len(aggregate_slots([(h, 10) for h in range(12)], limit=5)) == 5


class TestViralScore:
    def test_ideal_video_ready(self):
        from app.services.viral_score import score_video_meta

        out = score_video_meta({
            "duration": 15, "width": 1080, "height": 1920, "has_audio": True,
            "audio_track": "trend1", "caption": "hi", "hashtags": "#a #b #c",
            "has_effect": True, "has_custom_cover": True, "has_cover": True,
        })
        assert out["score"] == 100 and out["verdict"] == "ready"
        assert out["suggestions"] == []

    def test_weak_video_risky(self):
        from app.services.viral_score import score_video_meta

        out = score_video_meta({
            "duration": 90, "width": 1920, "height": 1080, "has_audio": False,
            "audio_track": None, "caption": "", "hashtags": "",
            "has_effect": False, "has_custom_cover": False, "has_cover": False,
        })
        assert out["verdict"] == "risky" and len(out["suggestions"]) >= 4

    def test_middle_ground_needs_work(self):
        from app.services.viral_score import score_video_meta

        out = score_video_meta({
            "duration": 45, "width": 720, "height": 1280, "has_audio": True,
            "audio_track": None, "caption": "cap", "hashtags": "#a",
            "has_effect": False, "has_custom_cover": False, "has_cover": True,
        })
        assert out["verdict"] == "needs-work"
        assert sum(b["points"] for b in out["breakdown"]) == out["score"]
