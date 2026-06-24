"""
shared/retry.py — Retry decorators for all external calls.

Rules (from CLAUDE.md):
- Retry on: 429 (rate limit), 5xx (server error), network timeout
- Do NOT retry: 4xx other than 429 (client errors are not transient)
- Use exponential backoff with jitter
- Log every retry attempt

Usage:
    from shared.retry import retry, retry_async

    @retry(max_attempts=3, base_delay=5)
    def call_github_api():
        ...

    @retry_async(max_attempts=3, base_delay=2)
    async def call_google_drive():
        ...

    # Or inline:
    result = retry_call(my_fn, args=(arg1,), max_attempts=3)
"""

import asyncio
import functools
import logging
import random
import time
from typing import Any, Callable, Optional, Type

logger = logging.getLogger(__name__)

# Exceptions that indicate a transient failure — safe to retry
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class RateLimitError(Exception):
    """Raised when a rate limit (429) is encountered."""
    pass


class ServerError(Exception):
    """Raised when a server error (5xx) is encountered."""
    pass


class ClientError(Exception):
    """Raised when a client error (4xx, non-429) is encountered. Do NOT retry."""
    pass


def retry(max_attempts: int = 3, base_delay: float = 5.0,
          max_delay: float = 120.0, exceptions: tuple = (RateLimitError, ServerError)):
    """
    Decorator for synchronous functions with exponential backoff.

    Args:
        max_attempts: Total number of attempts (including the first)
        base_delay: Initial delay in seconds (doubled each retry)
        max_delay: Maximum delay cap in seconds
        exceptions: Exception types to catch and retry on
    """
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs) -> Any:
            return _retry_sync(fn, args, kwargs, max_attempts, base_delay, max_delay, exceptions)
        return wrapper
    return decorator


def retry_async(max_attempts: int = 3, base_delay: float = 5.0,
                max_delay: float = 120.0, exceptions: tuple = (RateLimitError, ServerError)):
    """Decorator for async functions with exponential backoff."""
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs) -> Any:
            return await _retry_async(fn, args, kwargs, max_attempts, base_delay, max_delay, exceptions)
        return wrapper
    return decorator


def retry_call(fn: Callable, args: tuple = (), kwargs: dict = None,
               max_attempts: int = 3, base_delay: float = 5.0,
               max_delay: float = 120.0) -> Any:
    """Inline retry for cases where a decorator is inconvenient."""
    return _retry_sync(fn, args, kwargs or {}, max_attempts, base_delay, max_delay,
                       (RateLimitError, ServerError, TimeoutError, ConnectionError))


# ── Internal ──────────────────────────────────────────────────────────────────

def _retry_sync(fn, args, kwargs, max_attempts, base_delay, max_delay, exceptions):
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn(*args, **kwargs)
        except ClientError:
            raise  # Client errors are not retried
        except exceptions as e:
            last_error = e
            if attempt == max_attempts:
                logger.error(f"[retry] {fn.__name__} failed after {max_attempts} attempts: {e}")
                raise
            delay = _backoff(attempt, base_delay, max_delay)
            logger.warning(f"[retry] {fn.__name__} attempt {attempt}/{max_attempts} failed: {e}. "
                           f"Retrying in {delay:.1f}s")
            time.sleep(delay)
        except Exception as e:
            # Non-retryable exception — raise immediately
            logger.error(f"[retry] {fn.__name__} failed with non-retryable error: {type(e).__name__}: {e}")
            raise

    raise RuntimeError(f"[retry] {fn.__name__} exhausted {max_attempts} attempts") from last_error


async def _retry_async(fn, args, kwargs, max_attempts, base_delay, max_delay, exceptions):
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await fn(*args, **kwargs)
        except ClientError:
            raise
        except exceptions as e:
            last_error = e
            if attempt == max_attempts:
                logger.error(f"[retry] {fn.__name__} failed after {max_attempts} attempts: {e}")
                raise
            delay = _backoff(attempt, base_delay, max_delay)
            logger.warning(f"[retry] {fn.__name__} attempt {attempt}/{max_attempts} failed: {e}. "
                           f"Retrying in {delay:.1f}s")
            await asyncio.sleep(delay)
        except Exception as e:
            raise

    raise RuntimeError(f"[retry] {fn.__name__} exhausted {max_attempts} attempts") from last_error


def _backoff(attempt: int, base_delay: float, max_delay: float) -> float:
    """Exponential backoff with ±10% jitter to avoid thundering herd."""
    delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
    jitter = delay * random.uniform(-0.1, 0.1)
    return delay + jitter


def check_http_status(status_code: int, response_text: str = ""):
    """
    Call this after every HTTP response to raise the appropriate exception.
    Usage: check_http_status(response.status_code, response.text)
    """
    if status_code == 429:
        raise RateLimitError(f"Rate limited (429): {response_text[:200]}")
    if 500 <= status_code < 600:
        raise ServerError(f"Server error ({status_code}): {response_text[:200]}")
    if 400 <= status_code < 500:
        raise ClientError(f"Client error ({status_code}): {response_text[:200]}")
