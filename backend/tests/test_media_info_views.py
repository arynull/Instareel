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
    def __init__(self, payload):
        self._payload = payload

    def media_info(self, media_id):
        return _FakeMedia(self._payload)


@pytest.fixture()
def svc_with(monkeypatch):
    def _svc(payload):
        svc = InstagramService.__new__(InstagramService)
        monkeypatch.setattr(
            InstagramService, "_make_client", lambda self, u, **kw: _FakeClient(payload)
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
