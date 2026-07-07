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


# ── catch-up + dedup helpers ──────────────────────────────────────────────────

def test_most_recent_fire_includes_exact_now():
    # At exactly the fire minute, most_recent_fire returns that minute (not prior).
    got = sd.most_recent_fire("15 10 * * *", _et(2026, 7, 2, 10, 15))
    assert got == _et(2026, 7, 2, 10, 15)


def test_most_recent_fire_before_todays_fire():
    # At 10:14 (before today's 10:15), the most recent fire was yesterday.
    got = sd.most_recent_fire("15 10 * * *", _et(2026, 7, 2, 10, 14))
    assert got == _et(2026, 7, 1, 10, 15)


def test_fire_key_minute_precision():
    assert sd.fire_key("harbor-nb1", _et(2026, 7, 2, 10, 15)) == "harbor-nb1@2026-07-02T10:15"


def test_parse_run_name_key_roundtrip():
    # run-name format: "<id> @<fire> (#<n>)[ dry]"
    assert sd._parse_run_name_key("harbor-nb1 @2026-07-02T10:15 (#164)") == "harbor-nb1@2026-07-02T10:15"
    assert sd._parse_run_name_key("harbor-nb1 @2026-07-02T10:15 (#164) dry") == "harbor-nb1@2026-07-02T10:15"


def test_parse_run_name_key_ignores_manual_and_legacy():
    # Manual dispatch with empty fire_time, and old "pipeline run #N" names → None.
    assert sd._parse_run_name_key("harbor-nb1 @ (#170)") is None
    assert sd._parse_run_name_key("pipeline run #162") is None
    assert sd._parse_run_name_key("pipeline run #163 (dry run)") is None


def test_dedup_key_matches_between_dispatch_and_parse():
    # The key we'd dedup on must equal the key parsed back from the run-name the
    # dispatch produces — the contract that makes catch-up idempotent.
    nb_id, fire = "grove-nb2", _et(2026, 7, 2, 10, 15)
    fire_iso = fire.strftime("%Y-%m-%dT%H:%M")
    run_name = f"{nb_id} @{fire_iso} (#171)"
    assert sd._parse_run_name_key(run_name) == sd.fire_key(nb_id, fire)
