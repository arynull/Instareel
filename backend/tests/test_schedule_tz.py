"""Schedule timezone (SCHEDULE_TZ) behavior.

Rules fire at the admin's wall-clock, not the server's: a rule set for
"Sat 12:00" must fire at 12:00 in SCHEDULE_TZ, whatever zone the server
runs in.
"""
import os
import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.database as database
from app.config import Settings, settings
from app.database import Base
from app.models import ScheduleRule
from app.tasks import sync_helpers


@pytest.fixture()
def sync_factory(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/s.db")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SyncSessionLocal", maker)
    return maker


def _rule(s, **kw):
    defaults = dict(name="t", day_of_week=5, hour=12, minute=0, is_active=True)
    defaults.update(kw)
    r = ScheduleRule(**defaults)
    s.add(r)
    s.commit()
    return r


def test_schedule_now_uses_configured_tz(monkeypatch):
    monkeypatch.setattr(settings, "SCHEDULE_TZ", "Asia/Tehran")
    assert sync_helpers._schedule_now().tzinfo.key == "Asia/Tehran"


def test_due_rules_matches_wall_clock_in_schedule_tz(sync_factory, monkeypatch):
    # 2026-09-26 is a Saturday; 08:30 UTC == 12:00 in Tehran.
    fixed = dt.datetime(2026, 9, 26, 12, 0, tzinfo=ZoneInfo("Asia/Tehran"))
    assert fixed.weekday() == 5
    monkeypatch.setattr(sync_helpers, "_schedule_now", lambda: fixed)
    with sync_factory() as s:
        _rule(s)  # Sat 12:00 fires at Tehran noon …
        assert len(sync_helpers.due_rules(s)) == 1
        _rule(s, hour=13)  # … but 13:00 does not fire yet
        assert len(sync_helpers.due_rules(s)) == 1


def test_due_rules_explicit_at_unchanged(sync_factory):
    # A caller-supplied `at` is used as-is (tests / back-compat).
    with sync_factory() as s:
        _rule(s)
        at = dt.datetime(2026, 9, 26, 12, 0, tzinfo=dt.timezone.utc)
        assert len(sync_helpers.due_rules(s, at=at)) == 1


def test_invalid_schedule_tz_falls_back_to_utc():
    assert Settings(SCHEDULE_TZ="Not/AZone").SCHEDULE_TZ == "UTC"
    assert Settings(SCHEDULE_TZ="Asia/Tehran").SCHEDULE_TZ == "Asia/Tehran"


def test_best_slots_convert_to_schedule_tz(monkeypatch):
    from app.services.best_slots import aggregate_slots

    monkeypatch.setattr(settings, "SCHEDULE_TZ", "Asia/Tehran")
    slots = aggregate_slots([(21, 1000), (18, 100)])
    top = slots[0]
    assert top["hour_utc"] == 21
    assert top["local"] == "00:30"      # 21:00 UTC -> 00:30 Tehran
    assert top["hour_local"] == 0
    assert top["tz"] == "Asia/Tehran" and top["tz_label"] == "Tehran"


def test_timezone_endpoint_reports_configured_tz(tmp_path, monkeypatch):
    from app.main import create_app

    monkeypatch.setattr(settings, "SCHEDULE_TZ", "Asia/Tehran")
    monkeypatch.setattr(settings, "MEDIA_ROOT", str(tmp_path / "media"))
    app = create_app()
    with TestClient(app) as c:
        tokens = c.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": os.environ["ADMIN_PASSWORD"]},
        ).json()
        r = c.get(
            "/api/v1/system/timezone",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["tz"] == "Asia/Tehran"
        assert body["label"] == "Tehran"
        assert body["utc_offset"] == "UTC+03:30"
