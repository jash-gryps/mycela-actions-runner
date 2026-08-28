"""Guard the Neon compute-budget invariants in heartbeat.yml.

These are COST invariants, not style checks. On 2026-08-25 the Neon free-tier
compute quota was exhausted because the heartbeat ticked every ~2.5 min while
the database endpoint's suspend timeout was 300s. Every tick opens a connection
(schedule_dispatch.load_notebooks), so a tick shorter than the suspend timeout
means the endpoint NEVER suspends and is billed as if it ran 24/7. That took
down every dashboard login and stopped the notebook fleet for days.

The fix was easy to make and just as easy to undo — "the scheduler feels slow,
let's drop the sleep back to 2 minutes" would silently reintroduce a multi-day
outage with no error anywhere. So the interval is pinned here: shortening it
fails CI and whoever does it has to read this and mean it.

See docs/NEON_BUDGET.md.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/heartbeat.yml"

# The endpoint suspend timeout we budget against (docs/NEON_BUDGET.md).
SUSPEND_TIMEOUT_SECONDS = 60
# The tick must stay well clear of it. 10 min gives ~10x margin and keeps the
# scheduler's share of the budget near ~1.6 awake-hours/day.
MIN_TICK_SECONDS = 600


@pytest.fixture(scope="module")
def workflow_text() -> str:
    assert WORKFLOW.is_file(), f"missing workflow: {WORKFLOW}"
    return WORKFLOW.read_text()


def test_tick_interval_is_above_the_suspend_timeout(workflow_text: str) -> None:
    """The `sleep` between ticks must be long enough for Neon to suspend."""
    sleeps = [int(m) for m in re.findall(r"^\s*sleep\s+(\d+)\s*$",
                                         workflow_text, re.MULTILINE)]
    assert sleeps, "no `sleep N` found in heartbeat.yml — has the tick loop changed?"
    tick = min(sleeps)

    assert tick > SUSPEND_TIMEOUT_SECONDS, (
        f"heartbeat tick is {tick}s but the Neon endpoint suspends after "
        f"{SUSPEND_TIMEOUT_SECONDS}s. A tick shorter than the suspend timeout "
        f"pins the database awake 24/7 and will exhaust the free compute quota "
        f"— that is the 2026-08-25 outage. See docs/NEON_BUDGET.md."
    )
    assert tick >= MIN_TICK_SECONDS, (
        f"heartbeat tick is {tick}s, below the {MIN_TICK_SECONDS}s floor. "
        f"Raise it, or change the floor deliberately AND re-do the budget "
        f"arithmetic in docs/NEON_BUDGET.md first."
    )


def test_job_timeout_outlives_a_tick(workflow_text: str) -> None:
    """A tick that is killed mid-sleep never re-spawns; the chain would stall."""
    sleeps = [int(m) for m in re.findall(r"^\s*sleep\s+(\d+)\s*$",
                                         workflow_text, re.MULTILINE)]
    timeouts = [int(m) for m in re.findall(r"timeout-minutes:\s*(\d+)", workflow_text)]
    assert sleeps and timeouts, "could not read sleep/timeout-minutes"
    assert min(timeouts) * 60 > max(sleeps), (
        f"timeout-minutes={min(timeouts)} does not outlast sleep={max(sleeps)}s; "
        f"the job would be killed before it re-spawns the next tick."
    )


def test_backup_schedule_does_not_cancel_healthy_ticks(workflow_text: str) -> None:
    """`cancel-in-progress` + a frequent cron kills ticks that are mid-sleep.

    The backup cron exists only to recover a dead chain. If it fires more often
    than a tick lasts, it cancels healthy in-flight ticks instead — which is
    what produced the long run of "cancelled" heartbeat runs.
    """
    if "cancel-in-progress: true" not in workflow_text:
        pytest.skip("no cancel-in-progress; frequent cron is harmless")

    crons = re.findall(r"cron:\s*'([^']+)'", workflow_text)
    assert crons, "no cron found in heartbeat.yml"
    minute_field = crons[0].split()[0]
    # Reject comma lists and short step intervals, e.g. '8,23,38,53' or '*/5'.
    fires_per_hour = (len(minute_field.split(","))
                      if "," in minute_field else
                      (60 // int(minute_field.split("/")[1]) if "/" in minute_field else 1))
    assert fires_per_hour <= 1, (
        f"backup cron {crons[0]!r} fires {fires_per_hour}x/hour, but a tick now "
        f"lasts minutes and cancel-in-progress would kill it. Keep it hourly."
    )
