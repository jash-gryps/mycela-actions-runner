#!/usr/bin/env python3
"""Fail loudly BEFORE the Neon compute quota runs out.

Why this exists
---------------
On 2026-08-25 the Neon free-tier compute quota was exhausted. Every database
connection started being refused, which meant:

  * every dashboard login failed as "Invalid username or password" (the auth
    code could not tell an outage from a bad password), and
  * the notebook fleet stopped dead after run #1678.

Nothing warned anyone. The first signal was a user saying they could not log in,
three days later. This script is that missing warning.

It projects the current consumption period's compute usage to the end of the
period and fails the workflow while there is still time to act. A failing
scheduled workflow emails the repo owner, so no extra alerting is needed.

Neon bills COMPUTE TIME: how long an endpoint stays awake, not how many queries
run. An endpoint suspends after `suspend_timeout_seconds` of idle, so anything
touching the database more often than that timeout pins it awake permanently.
That is the failure mode this guards against — see docs/NEON_BUDGET.md.

Env:
  NEON_API_KEY          required; read-only use. Skips (exit 0) if unset.
  NEON_PROJECT_ID       required.
  NEON_BUDGET_CU_HOURS  monthly ceiling to stay under (default 100).
  NEON_WARN_FRACTION    projected/budget ratio that fails the job (default 0.8).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

API = "https://console.neon.tech/api/v2"


def _iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def fetch_project(project_id: str, api_key: str) -> dict:
    req = urllib.request.Request(
        f"{API}/projects/{project_id}",
        headers={"Authorization": f"Bearer {api_key}",
                 "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())["project"]
    except urllib.error.HTTPError as e:
        sys.exit(f"Neon API returned HTTP {e.code}: {e.read()[:200]!r}")
    except urllib.error.URLError as e:
        sys.exit(f"Could not reach the Neon API: {e.reason}")


def main() -> int:
    api_key = os.environ.get("NEON_API_KEY", "").strip()
    project_id = os.environ.get("NEON_PROJECT_ID", "").strip()
    if not api_key:
        print("NEON_API_KEY not set — skipping budget check (not a failure).")
        print("Set the secret to enable early warning of quota exhaustion.")
        return 0
    if not project_id:
        sys.exit("NEON_PROJECT_ID is required when NEON_API_KEY is set")

    p = fetch_project(project_id, api_key)
    budget = float(os.environ.get("NEON_BUDGET_CU_HOURS", "100"))
    warn_at = float(os.environ.get("NEON_WARN_FRACTION", "0.8"))

    used_h = p.get("compute_time_seconds", 0) / 3600
    awake_h = p.get("active_time_seconds", 0) / 3600
    start, end = _iso(p["consumption_period_start"]), _iso(p["consumption_period_end"])
    now = datetime.now(timezone.utc)

    total = (end - start).total_seconds()
    elapsed = max((now - start).total_seconds(), 1.0)
    frac = min(elapsed / total, 1.0)
    projected = used_h / frac                      # linear run-rate projection
    days_left = max((end - now).total_seconds(), 0) / 86400

    print(f"project           : {p.get('name')} ({project_id})")
    print(f"plan              : {p.get('owner', {}).get('subscription_type', '?')}")
    print(f"period            : {start:%Y-%m-%d} -> {end:%Y-%m-%d} "
          f"({frac*100:.0f}% elapsed, {days_left:.1f}d left)")
    print(f"compute used      : {used_h:.1f} CU-hours  (endpoint awake {awake_h:.0f} h)")
    print(f"projected at end  : {projected:.1f} CU-hours")
    print(f"budget            : {budget:.0f} CU-hours  (fail above "
          f"{budget*warn_at:.0f} projected)")

    # Already dead — the quota is gone and the platform is down right now.
    if used_h >= budget:
        print("\nFAIL: the compute budget is ALREADY exhausted for this period.")
        print("Every database connection is being refused, so logins and the "
              "notebook fleet are down until the period resets.")
        return 1

    if projected > budget * warn_at:
        over = projected / budget * 100
        print(f"\nFAIL: on the current run-rate this period ends at {over:.0f}% "
              f"of budget.")
        print("Something is holding a Neon endpoint awake. Check, in order:")
        print("  1. the heartbeat tick interval vs the endpoint suspend timeout")
        print("     (a tick shorter than the timeout pins the endpoint awake),")
        print("  2. dashboard polling (NotebooksTable refresh cadence),")
        print("  3. any new job that opens a connection on a short loop.")
        print("See docs/NEON_BUDGET.md.")
        return 1

    print(f"\nOK: projected {projected:.1f} CU-h is within budget.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
