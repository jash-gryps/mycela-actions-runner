"""Tests for scripts/reconcile_cronjobs.py helpers + skip/recreate decisions."""

import pytest

from scripts import reconcile_cronjobs as rc


def test_id_from_url():
    assert rc._id_from_url("https://x/api/cron/trigger?id=harbor-nb1&token=t") == "harbor-nb1"
    assert rc._id_from_url("https://x/api/cron/trigger?token=t") is None
    assert rc._id_from_url("") is None


def test_schedule_matches_true():
    desired = {"minutes": [30], "hours": -1, "mdays": -1, "months": -1,
               "wdays": -1, "timezone": "America/New_York"}
    current = dict(desired, expiresAt=0)  # extra API fields are ignored
    assert rc._schedule_matches(current, desired) is True


def test_schedule_matches_false_on_timezone():
    desired = {"minutes": [30], "hours": -1, "mdays": -1, "months": -1,
               "wdays": -1, "timezone": "America/New_York"}
    current = dict(desired, timezone="UTC")
    assert rc._schedule_matches(current, desired) is False


def test_schedule_matches_false_when_empty():
    assert rc._schedule_matches({}, {"timezone": "America/New_York"}) is False


def test_breaker_trips_after_consecutive_failures():
    b = rc._Breaker()
    b.record(False)                 # 1
    with pytest.raises(SystemExit):
        b.record(False)             # 2 → trip
    # a success resets the counter
    b2 = rc._Breaker()
    b2.record(False)
    b2.record(True)
    b2.record(False)  # no raise — counter was reset
