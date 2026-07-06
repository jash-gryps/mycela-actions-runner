"""Tests for the GitHub-native scheduler's due-time logic."""

from datetime import datetime
from zoneinfo import ZoneInfo

from scripts import schedule_dispatch as sd

ET = ZoneInfo("America/New_York")


def _et(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=ET)


def test_due_exactly_on_time():
    # "15 10 * * *" (10:15 ET daily); tick at 10:15 → due.
    assert sd.is_due("15 10 * * *", _et(2026, 7, 2, 10, 15), 5) is True


def test_due_within_window_after():
    # tick at 10:18, 3 min after the 10:15 fire → still due (window 5).
    assert sd.is_due("15 10 * * *", _et(2026, 7, 2, 10, 18), 5) is True


def test_not_due_past_window():
    # tick at 10:21, 6 min after → outside a 5-min window.
    assert sd.is_due("15 10 * * *", _et(2026, 7, 2, 10, 21), 5) is False


def test_not_due_before_fire():
    # tick at 10:14, before today's 10:15 fire → prev fire was yesterday → not due.
    assert sd.is_due("15 10 * * *", _et(2026, 7, 2, 10, 14), 5) is False


def test_hourly_due_at_half_past():
    # "30 * * * *" fires every :30; tick at 11:31 → due.
    assert sd.is_due("30 * * * *", _et(2026, 7, 2, 11, 31), 5) is True
    # tick at 11:40 → 10 min after :30 → not due.
    assert sd.is_due("30 * * * *", _et(2026, 7, 2, 11, 40), 5) is False


def test_weekday_only_not_due_on_weekend():
    # "0 9 * * 1-5" (weekdays 09:00). 2026-07-04 is a Saturday → not due at 09:02.
    assert sd.is_due("0 9 * * 1-5", _et(2026, 7, 4, 9, 2), 5) is False
    # 2026-07-02 is a Thursday → due at 09:02.
    assert sd.is_due("0 9 * * 1-5", _et(2026, 7, 2, 9, 2), 5) is True


def test_fires_once_across_consecutive_5min_ticks():
    # A 10:15 schedule must be "due" on exactly one of a clean 5-min tick sequence.
    ticks = [_et(2026, 7, 2, 10, m) for m in (5, 10, 15, 20, 25)]
    hits = [t for t in ticks if sd.is_due("15 10 * * *", t, 5)]
    assert hits == [_et(2026, 7, 2, 10, 15)]


def test_gryps_url_secret():
    assert sd._gryps_url_secret("harbor") == "GRYPS_URL_HARBOR"
    assert sd._gryps_url_secret("ridge") == "GRYPS_URL_RIDGE"
