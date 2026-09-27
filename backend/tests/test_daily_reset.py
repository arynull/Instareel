"""m6: daily-counts reset is idempotent per SCHEDULE_TZ day with catch-up.

The old reset_daily_counts only ran at the midnight beat tick — a worker/beat
outage at midnight meant counts were never reset and accounts stayed capped.
ensure_daily_counts_reset stamps the reset date, so the midnight task and the
per-minute scheduler backstop each perform it exactly once per local day.
"""
import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.database as database
from app.database import Base
from app.models import Account, AccountStatus, Setting
from app.core.security import encrypt_secret
from app.tasks import sync_helpers
from app.tasks.sync_helpers import DAILY_RESET_DATE_KEY, ensure_daily_counts_reset


@pytest.fixture()
def maker(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    Base.metadata.create_all(engine)
    m = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SyncSessionLocal", m)
    return m


def _account(maker, posts_today):
    with maker() as s:
        acc = Account(
            username="u1",
            password_enc=encrypt_secret("pw"),
            status=AccountStatus.active,
            posts_today=posts_today,
        )
        s.add(acc)
        s.commit()
        return acc.id


def _reset_date(maker):
    with maker() as s:
        row = s.get(Setting, DAILY_RESET_DATE_KEY)
        return row.value if row else None


def test_first_run_resets_and_stamps(maker):
    _account(maker, posts_today=3)
    assert ensure_daily_counts_reset() is True
    with maker() as s:
        acc = s.query(Account).one()
        assert acc.posts_today == 0
    today = sync_helpers._schedule_now().date().isoformat()
    assert _reset_date(maker) == today


def test_second_run_same_day_is_noop(maker):
    _account(maker, posts_today=3)
    assert ensure_daily_counts_reset() is True
    # New posts after the reset must not be wiped by a repeat run.
    with maker() as s:
        s.query(Account).one().posts_today = 2
        s.commit()
    assert ensure_daily_counts_reset() is False
    with maker() as s:
        assert s.query(Account).one().posts_today == 2


def test_missed_midnight_catches_up(maker):
    """A stale stamp (beat was down at midnight) triggers the reset on the
    next call — this is the backstop check_and_post runs every minute."""
    _account(maker, posts_today=3)
    yesterday = (sync_helpers._schedule_now().date() - dt.timedelta(days=1)).isoformat()
    with maker() as s:
        s.add(Setting(key=DAILY_RESET_DATE_KEY, value=yesterday, category="system"))
        s.commit()
    assert ensure_daily_counts_reset() is True
    with maker() as s:
        assert s.query(Account).one().posts_today == 0
    assert _reset_date(maker) == sync_helpers._schedule_now().date().isoformat()


def test_reset_task_returns_status(maker):
    from app.tasks.periodic_tasks import reset_daily_counts

    _account(maker, posts_today=1)
    assert reset_daily_counts() == {"ok": True, "reset": True}
    assert reset_daily_counts() == {"ok": True, "reset": False}
