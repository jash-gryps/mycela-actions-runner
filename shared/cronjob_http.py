"""
shared/cronjob_http.py — Robust HTTP requester for the cron-job.org REST API.

cron-job.org's free tier rate-limits aggressively and returns HTTP 429 (often on
the very first ``GET /jobs``). The historical failure mode was: a weak, header-
blind retry that gave up after a few fixed sleeps, aborting the whole sync before
a single job was touched — made worse by running the sync repeatedly in a short
window. This module centralises one robust requester used by BOTH
``notebooks/setup_cronjobs.py`` and ``scripts/migrate_cronjobs.py`` so their retry
behaviour can never drift apart again.

Behaviour:
- Retries 429 (rate limited) and 503 (service unavailable).
- Honours the ``Retry-After`` response header when present (seconds or HTTP-date);
  this is authoritative and usually short — prefer it over guessing.
- Falls back to exponential backoff with jitter when no header is given.
- Raises via ``shared.retry.check_http_status`` on any non-retryable 4xx/5xx, and
  on the final attempt if still rate limited (RateLimitError).

Both ``requester`` and ``sleep`` are injectable so the retry logic is unit-testable
without real network calls or real waiting.
"""

import datetime
import logging
import random
import time
from email.utils import parsedate_to_datetime
from typing import Callable, Optional

import requests

from shared.retry import check_http_status

logger = logging.getLogger(__name__)

# Statuses that mean "try again later" rather than "you did something wrong".
RETRYABLE_STATUSES = {429, 503}


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Parse a Retry-After header (delta-seconds or HTTP-date) into seconds.

    Returns None when the header is absent or unparseable, so the caller falls
    back to computed backoff.
    """
    if not value:
        return None
    value = value.strip()
    # Form 1: an integer/float number of seconds.
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    # Form 2: an HTTP-date.
    try:
        when = parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=datetime.timezone.utc)
        delta = (when - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
        return max(0.0, delta)
    except (TypeError, ValueError):
        return None


def _backoff(attempt: int, base_delay: float, max_delay: float) -> float:
    """Exponential backoff with ±10% jitter, capped at max_delay."""
    delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
    return delay + delay * random.uniform(-0.1, 0.1)


def request_with_backoff(
    method: str,
    url: str,
    *,
    headers: Optional[dict] = None,
    timeout: int = 30,
    max_attempts: int = 5,
    base_delay: float = 30.0,
    max_delay: float = 300.0,
    sleep: Callable[[float], None] = time.sleep,
    requester: Optional[Callable[..., requests.Response]] = None,
    **kwargs,
) -> requests.Response:
    """Perform an HTTP request, retrying on rate limits with backoff.

    Args:
        method, url: passed straight to the requester.
        headers, timeout, **kwargs: passed straight to the requester.
        max_attempts: total attempts including the first.
        base_delay/max_delay: exponential-backoff bounds (seconds) used only when
            the response carries no usable Retry-After header.
        sleep/requester: injection points for testing.

    Returns the successful ``requests.Response``.
    Raises RateLimitError/ServerError/ClientError (from ``check_http_status``) on
    failure or exhaustion.
    """
    requester = requester or requests.request
    resp: Optional[requests.Response] = None

    for attempt in range(1, max_attempts + 1):
        resp = requester(method, url, headers=headers, timeout=timeout, **kwargs)

        if resp.status_code in RETRYABLE_STATUSES and attempt < max_attempts:
            retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
            delay = retry_after if retry_after is not None else _backoff(attempt, base_delay, max_delay)
            delay = min(delay, max_delay)
            logger.warning(
                f"[cronjob-http] {resp.status_code} on {method} {url} — waiting "
                f"{delay:.0f}s (attempt {attempt}/{max_attempts})"
                + (" [Retry-After]" if retry_after is not None else "")
            )
            sleep(delay)
            continue

        # Non-retryable status, or the final attempt: let check_http_status decide.
        # It raises on any 4xx/5xx (including a still-429 final attempt) and is a
        # no-op for 2xx/3xx, in which case we return the response.
        check_http_status(resp.status_code, resp.text)
        return resp

    # Loop only exits via return/raise above; this satisfies type checkers.
    check_http_status(resp.status_code, resp.text)  # pragma: no cover
    return resp  # pragma: no cover
