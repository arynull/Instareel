"""media_info must prefer play_count (the live reel metric) over view_count.

instagrapi documents view_count as "for Video and IGTV"; for reels the
fresh counter is play_count. When the API returns both and they disagree,
the old `view_count or play_count` order stored the stale one — the
dashboard showed "checked 1h ago" with numbers that were not from 1h ago.
"""
import pytest

from app.services.instagram_service import InstagramService


class _FakeMedia:
    def __init__(self, payload):
        self._payload = payload

    def dict(self):
        return dict(self._payload)


class _FakeClient:
    def __init__(self, payload, raw=None):
        self._payload = payload
        # Raw v1 payload (what private_request returns); None emulates an
        # older client without last_json or a public fallback response.
        self.last_json = None if raw is None else {"items": [raw]}

    def media_info(self, media_id):
        return _FakeMedia(self._payload)


@pytest.fixture()
def svc_with(monkeypatch):
    def _svc(payload, raw=None):
        svc = InstagramService.__new__(InstagramService)
        monkeypatch.setattr(
            InstagramService, "_make_client", lambda self, u, **kw: _FakeClient(payload, raw)
        )
        return svc

    return _svc


def test_play_count_wins_over_stale_view_count(svc_with):
    svc = svc_with({"like_count": 10, "comment_count": 2, "view_count": 800, "play_count": 1250})
    assert svc.media_info("u", "123")["view_count"] == 1250


def test_view_count_fallback_when_no_play_count(svc_with):
    svc = svc_with({"like_count": 10, "comment_count": 2, "view_count": 800})
    assert svc.media_info("u", "123")["view_count"] == 800


def test_zero_when_neither_present(svc_with):
    svc = svc_with({"like_count": 10, "comment_count": 2})
    assert svc.media_info("u", "123")["view_count"] == 0


def test_ig_play_count_preferred_when_parsed_fields_zero(svc_with):
    # Regression: IG's private API can lag the parsed play_count (0) while
    # the unified ig_play_count already carries the real number. The
    # extractor drops ig_play_count, so media_info must read the raw
    # payload and prefer it.
    svc = svc_with(
        {"like_count": 1, "comment_count": 0, "view_count": 0, "play_count": 0},
        raw={"ig_play_count": 164, "play_count": 0, "view_count": None, "like_count": 1},
    )
    assert svc.media_info("u", "123")["view_count"] == 164


def test_ig_play_count_absent_falls_back_to_play_count(svc_with):
    svc = svc_with(
        {"like_count": 10, "comment_count": 2, "view_count": 800, "play_count": 1250},
        raw={"play_count": 1250, "view_count": 800},
    )
    assert svc.media_info("u", "123")["view_count"] == 1250


def test_no_raw_payload_behaves_as_before(svc_with):
    svc = svc_with({"like_count": 10, "comment_count": 2, "view_count": 800, "play_count": 1250}, raw=None)
    assert svc.media_info("u", "123")["view_count"] == 1250


def test_zero_ig_play_count_does_not_mask_play_count(svc_with):
    svc = svc_with(
        {"like_count": 10, "comment_count": 2, "play_count": 300},
        raw={"ig_play_count": 0, "play_count": 300},
    )
    assert svc.media_info("u", "123")["view_count"] == 300
