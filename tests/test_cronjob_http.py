"""Tests for shared.cronjob_http.request_with_backoff — the rate-limit handling
that the June 2026 cron-job.org sync outage traced back to."""

import pytest

from shared.cronjob_http import request_with_backoff, _parse_retry_after
from shared.retry import RateLimitError, ClientError, ServerError


class FakeResponse:
    def __init__(self, status_code, headers=None, content=b"{}"):
        self.status_code = status_code
        self.headers = headers or {}
        self.content = content
        self.text = ""


def _requester_returning(sequence):
    """Build a fake requester that yields the given responses in order and
    records how many times it was called."""
    calls = {"n": 0}
    seq = list(sequence)

    def requester(method, url, headers=None, timeout=30, **kwargs):
        calls["n"] += 1
        return seq[min(calls["n"] - 1, len(seq) - 1)]

    return requester, calls


def test_success_returns_immediately():
    requester, calls = _requester_returning([FakeResponse(200)])
    slept = []
    resp = request_with_backoff("GET", "https://x/jobs", requester=requester,
                                sleep=slept.append)
    assert resp.status_code == 200
    assert calls["n"] == 1
    assert slept == []  # never waited


def test_retry_after_header_is_honored():
    # First a 429 with Retry-After: 5, then a 200. Should sleep ~5s, not the
    # 30s exponential-backoff default.
    requester, calls = _requester_returning([
        FakeResponse(429, headers={"Retry-After": "5"}),
        FakeResponse(200),
    ])
    slept = []
    resp = request_with_backoff("GET", "https://x/jobs", requester=requester,
                                sleep=slept.append)
    assert resp.status_code == 200
    assert calls["n"] == 2
    assert slept == [5.0]


def test_backoff_used_when_no_retry_after():
    requester, calls = _requester_returning([
        FakeResponse(429),  # no header
        FakeResponse(200),
    ])
    slept = []
    resp = request_with_backoff("GET", "https://x/jobs", requester=requester,
                                sleep=slept.append, base_delay=30.0)
    assert resp.status_code == 200
    # First backoff is base_delay ± 10% jitter.
    assert len(slept) == 1
    assert 27.0 <= slept[0] <= 33.0


def test_exhaustion_raises_rate_limit():
    # Always 429 → after max_attempts, raise RateLimitError. Requester is called
    # exactly max_attempts times; we sleep between attempts (max_attempts-1).
    requester, calls = _requester_returning([FakeResponse(429)])
    slept = []
    with pytest.raises(RateLimitError):
        request_with_backoff("GET", "https://x/jobs", requester=requester,
                             sleep=slept.append, max_attempts=4, base_delay=1.0)
    assert calls["n"] == 4
    assert len(slept) == 3


def test_client_error_not_retried():
    requester, calls = _requester_returning([FakeResponse(400)])
    slept = []
    with pytest.raises(ClientError):
        request_with_backoff("GET", "https://x/jobs", requester=requester,
                             sleep=slept.append)
    assert calls["n"] == 1
    assert slept == []


def test_server_error_500_not_retried():
    # A 500 (e.g. the PATCH-payload bug) is not in RETRYABLE_STATUSES — raise
    # immediately so it isn't masked by pointless retries.
    requester, calls = _requester_returning([FakeResponse(500)])
    with pytest.raises(ServerError):
        request_with_backoff("PATCH", "https://x/jobs/1", requester=requester,
                             sleep=lambda s: None)
    assert calls["n"] == 1


def test_503_is_retried():
    requester, calls = _requester_returning([
        FakeResponse(503, headers={"Retry-After": "2"}),
        FakeResponse(200),
    ])
    slept = []
    resp = request_with_backoff("GET", "https://x/jobs", requester=requester,
                                sleep=slept.append)
    assert resp.status_code == 200
    assert slept == [2.0]


def test_max_delay_caps_sleep():
    requester, calls = _requester_returning([
        FakeResponse(429, headers={"Retry-After": "9999"}),
        FakeResponse(200),
    ])
    slept = []
    request_with_backoff("GET", "https://x/jobs", requester=requester,
                         sleep=slept.append, max_delay=120.0)
    assert slept == [120.0]  # honored header, but capped


@pytest.mark.parametrize("value,expected", [
    (None, None),
    ("", None),
    ("10", 10.0),
    ("0", 0.0),
    ("not-a-date", None),
])
def test_parse_retry_after(value, expected):
    assert _parse_retry_after(value) == expected
