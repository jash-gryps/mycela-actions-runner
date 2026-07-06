"""
scripts/schedule_dispatch.py — GitHub-native scheduler for the notebook pipelines.

Runs on a GitHub Actions `schedule` (every 5 min). Reads every notebook's cron
schedule from Neon (pipeline_display), figures out which ones are "due" in the
current window (evaluated in America/New_York, so schedules are US Eastern), and
dispatches run_notebook.yml for each — directly, using the workflow's own
GITHUB_TOKEN. No cron-job.org and no dashboard involved.

Why the GITHUB_TOKEN works here: `workflow_dispatch` is an explicit exception to
GitHub's recursion prevention, so a dispatch made with GITHUB_TOKEN *does* create
the run_notebook run (given `permissions: actions: write`).

Timeliness note: GitHub-hosted cron can drift (typically minutes, occasionally a
skipped tick under load). Running every 5 min with a 5-min due-window means a job
scheduled for 10:15 ET dispatches on the first tick at/after 10:15. It is not
millisecond-precise — see the caveat in scheduler.yml.

Env:
  DATABASE_URL       Neon connection string (read-only role is fine)
  GH_TOKEN           token with actions:write (the workflow's GITHUB_TOKEN)
  GH_REPO            "owner/repo" (github.repository)
  GH_REF             git ref to dispatch run_notebook.yml on (github.ref_name)
  WINDOW_MINUTES     due-window in minutes (default 5, match the schedule interval)
  PLAN_ONLY          "1" → log due notebooks + intended dispatch, make no API calls
  DISPATCH_DRY_RUN   "1" → dispatch run_notebook with dry_run=true (validation)

Usage:
  python scripts/schedule_dispatch.py
"""

import json
import logging
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from croniter import croniter

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

TZ = ZoneInfo("America/New_York")
GH_API = "https://api.github.com"


def is_due(schedule: str, now: datetime, window_minutes: int) -> bool:
    """True if `schedule` (5-field cron) fired within the last `window_minutes`.

    Evaluated in `now`'s timezone (America/New_York). Uses the most recent fire
    time <= now, so a schedule is caught exactly once by the first tick at/after
    its time (given ticks are ~window apart)."""
    # Base at now + 1s so a fire landing exactly on `now` counts as the previous
    # fire (croniter.get_prev is strict "<"; without this a tick that lands on the
    # fire minute — or a clean :15/:20 tick pair — would skip the job entirely).
    itr = croniter(schedule, now + timedelta(seconds=1))
    prev_fire = itr.get_prev(datetime)
    delta = now - prev_fire
    return timedelta(0) <= delta < timedelta(minutes=window_minutes)


def _gryps_url_secret(alias: str) -> str:
    # Mirrors dashboard dispatch.ts: GRYPS_URL_{ALIAS}. Aliases are codenames
    # (harbor, ridge, …) — never real client names.
    return f"GRYPS_URL_{alias.upper()}"


def dispatch_run_notebook(nb: dict, *, repo: str, ref: str, token: str,
                          dry_run: bool) -> None:
    """POST a workflow_dispatch for run_notebook.yml. Raises on HTTP error.

    pipeline_name is intentionally NOT sent — it holds real client identity and
    this repo is public (matches the dashboard's dispatch.ts)."""
    inputs = {
        "notebook_id": nb["notebook_id"],
        "alias": nb["tenant_alias"],
        "gryps_url_secret": _gryps_url_secret(nb["tenant_alias"]),
        "notebook_folder": nb.get("notebook_folder") or "",
        "notebook_file": nb.get("notebook_file") or "",
        "dry_run": "true" if dry_run else "false",
    }
    body = json.dumps({"ref": ref, "inputs": inputs}).encode()
    req = urllib.request.Request(
        f"{GH_API}/repos/{repo}/actions/workflows/run_notebook.yml/dispatches",
        data=body, method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status not in (201, 204):
            raise RuntimeError(f"dispatch returned HTTP {resp.status}")


def load_notebooks(db_url: str) -> list[dict]:
    import psycopg2  # imported here so unit tests don't need the driver
    conn = psycopg2.connect(db_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT notebook_id, tenant_alias, notebook_folder, notebook_file, "
                "schedule, paused FROM pipeline_display WHERE schedule IS NOT NULL "
                "ORDER BY notebook_id"
            )
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def main() -> None:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        sys.exit("DATABASE_URL is required")
    repo = os.environ.get("GH_REPO", "")
    ref = os.environ.get("GH_REF", "main")
    token = os.environ.get("GH_TOKEN", "")
    window = int(os.environ.get("WINDOW_MINUTES", "5"))
    plan_only = os.environ.get("PLAN_ONLY") == "1"
    dispatch_dry = os.environ.get("DISPATCH_DRY_RUN") == "1"

    if not plan_only and not token:
        sys.exit("GH_TOKEN is required unless PLAN_ONLY=1")

    now = datetime.now(TZ)
    logger.info(f"Scheduler tick at {now.isoformat()} (America/New_York), "
                f"window={window}m plan_only={plan_only} dispatch_dry={dispatch_dry}")

    notebooks = load_notebooks(db_url)
    logger.info(f"{len(notebooks)} scheduled notebook(s) in config")

    # Validation hook: dispatch ONE named notebook (dry_run) regardless of schedule,
    # to prove the GITHUB_TOKEN → run_notebook dispatch path works.
    force_id = os.environ.get("FORCE_NOTEBOOK")
    if force_id:
        nb = next((n for n in notebooks if n["notebook_id"] == force_id), None)
        if not nb:
            sys.exit(f"FORCE_NOTEBOOK {force_id!r} not found in pipeline_display")
        dispatch_run_notebook(nb, repo=repo, ref=ref, token=token, dry_run=True)
        logger.info(f"Force-dispatched {force_id} (dry_run=true) — plumbing OK")
        return

    due, errors = [], []
    for nb in notebooks:
        if nb.get("paused"):
            continue
        try:
            if not is_due(nb["schedule"], now, window):
                continue
        except Exception as exc:                       # bad cron string → skip, log
            logger.error(f"[{nb['notebook_id']}] invalid schedule {nb['schedule']!r}: {exc}")
            errors.append((nb["notebook_id"], str(exc)))
            continue

        due.append(nb["notebook_id"])
        if plan_only:
            logger.info(f"[due] {nb['notebook_id']} (alias={nb['tenant_alias']}, "
                        f"schedule={nb['schedule']}) — would dispatch")
            continue
        try:
            dispatch_run_notebook(nb, repo=repo, ref=ref, token=token, dry_run=dispatch_dry)
            logger.info(f"Dispatched run_notebook for {nb['notebook_id']} "
                        f"(dry_run={dispatch_dry})")
        except (urllib.error.HTTPError, urllib.error.URLError, RuntimeError) as exc:
            detail = exc.read().decode()[:200] if isinstance(exc, urllib.error.HTTPError) else str(exc)
            logger.error(f"FAILED dispatch {nb['notebook_id']}: {detail}")
            errors.append((nb["notebook_id"], detail))

    logger.info(f"Due this tick: {len(due)} — {due}")
    if errors:
        logger.error(f"{len(errors)} dispatch error(s): {errors}")
        sys.exit(1)


if __name__ == "__main__":
    main()
