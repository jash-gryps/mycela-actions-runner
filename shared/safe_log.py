"""
shared/safe_log.py — Redact client identity before it reaches a PUBLIC sink.

This repo runs on public GitHub Actions: stdout, step summaries, and the Actions UI
are world-readable. Real client identity must never appear there (see docs/SECURITY.md).

`safe_log()` / `redact()` are defence-in-depth for the *identity* leak vector: they mask
the resolved real Gryps URL and any `*.gryps.io` URL out of a string before it is logged.

They deliberately do NOT try to scrub arbitrary *client data* (notebook output, table
names, row values) — that is impossible to pattern-match reliably, so the rule there is
"never put it in a public message at all" (a generic message + private email/GDrive),
enforced at the call sites, not here.

All pipelines share this module — never duplicate this logic.
"""

import os
import re

# Any https?://<host>.gryps.io... URL — the real client URL shape.
_GRYPS_URL_RE = re.compile(r"https?://[A-Za-z0-9.-]+\.gryps\.io[^\s'\"<>]*")


def redact(text):
    """
    Return `text` with client-identifying URLs replaced by `***`.

    Masks (a) the exact value of the resolved real URL in env GRYPS_URL, and
    (b) any `*.gryps.io` URL. Returns falsy input (None / "") unchanged so it is
    safe to wrap any log argument.
    """
    if not text:
        return text
    out = str(text)

    real = os.environ.get("GRYPS_URL", "")
    if real:
        out = out.replace(real, "***")

    out = _GRYPS_URL_RE.sub("***", out)
    return out


def safe_log(log_fn, message):
    """Call `log_fn` with `message` redacted — e.g. safe_log(logger.error, msg)."""
    log_fn(redact(message))
