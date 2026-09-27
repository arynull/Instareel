"""Posting jitter: second-resolution, human-like fire times.

Anti-detection rationale: whole-minute fire times (06:00:00, 06:03:00)
are a bot fingerprint — real users post at 06:03:27. The jitter applied
when a rule slot fires is uniform over 0..N minutes at *second* resolution,
so two rules in the same minute naturally spread apart.

Covers: bounds, zero/negative/invalid/default handling, second resolution
(statistical), and monotonic non-negativity (never schedules in the past).
"""
import datetime as dt

import pytest

from app.tasks.post_tasks import fire_time_with_jitter


def _now():
    return dt.datetime.now(dt.timezone.utc)


def test_zero_jitter_fires_immediately():
    before = _now()
    when = fire_time_with_jitter("0")
    after = _now()
    assert before <= when <= after + dt.timedelta(seconds=1)


@pytest.mark.parametrize("raw", ["5", 5, 5.9, " 5 "])
def test_jitter_within_bounds(raw):
    before = _now()
    when = fire_time_with_jitter(raw)
    delay = (when - before).total_seconds()
    assert 0 <= delay <= 5 * 60 + 1


@pytest.mark.parametrize("raw", [None, "bogus", "", "  ", object()])
def test_invalid_jitter_falls_back_to_default_5(raw):
    before = _now()
    when = fire_time_with_jitter(raw)
    delay = (when - before).total_seconds()
    assert 0 <= delay <= 5 * 60 + 1


@pytest.mark.parametrize("raw", ["-3", -10, "-0.5"])
def test_negative_jitter_clamped_to_zero(raw):
    """A negative jitter would schedule in the past — never allowed."""
    before = _now()
    when = fire_time_with_jitter(raw)
    assert when >= before - dt.timedelta(seconds=1)


def test_never_schedules_in_the_past():
    for raw in ["0", "1", "5", "30", None, "nope", "-5"]:
        assert fire_time_with_jitter(raw) >= _now() - dt.timedelta(seconds=1)


def test_upper_bound_respected_over_many_samples():
    for _ in range(200):
        when = fire_time_with_jitter("2")
        delay = (when - _now()).total_seconds()
        assert -1 <= delay <= 2 * 60 + 1


def test_second_resolution_not_whole_minutes():
    """With jitter=5, fire times must actually use the seconds — if every
    sample landed on :00 it would be whole-minute jitter in disguise."""
    seconds = {fire_time_with_jitter("5").second for _ in range(60)}
    assert len(seconds) > 20, f"only {len(seconds)} distinct seconds in 60 samples"


def test_distribution_spreads_within_the_window():
    """Rough uniformity: samples should cover early, middle and late parts
    of the window, not cluster at one end."""
    delays = sorted(
        (fire_time_with_jitter("10") - _now()).total_seconds() for _ in range(300)
    )
    assert delays[0] < 60          # some fire in the first minute
    assert delays[-1] > 9 * 60     # some fire in the last minute
    median = delays[len(delays) // 2]
    assert 3 * 60 < median < 7 * 60, f"median delay {median:.0f}s looks skewed"


def test_two_rules_in_same_minute_spread_apart():
    """Two slots firing together should rarely land on the same second."""
    collisions = sum(
        1
        for _ in range(100)
        if fire_time_with_jitter("5").replace(microsecond=0)
        == fire_time_with_jitter("5").replace(microsecond=0)
    )
    assert collisions < 10, f"{collisions}/100 same-second collisions"


def test_large_jitter_setting():
    before = _now()
    when = fire_time_with_jitter("120")
    delay = (when - before).total_seconds()
    assert 0 <= delay <= 120 * 60 + 1


def test_result_is_timezone_aware_utc():
    when = fire_time_with_jitter("5")
    assert when.tzinfo is not None
    assert when.utcoffset() == dt.timedelta(0)
