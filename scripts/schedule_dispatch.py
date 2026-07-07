"""
scripts/schedule_dispatch.py — reliable, catch-up scheduler for the notebook
pipelines. Driven by heartbeat.yml (a self-perpetuating loop) and, as a backup,
scheduler.yml's GitHub `schedule` trigger.

Design for reliability (not just punctuality):
- Reads every notebook's cron schedule from Neon (pipeline_display) and evaluates
  the most recent fire time <= now in America/New_York (US Eastern).
- CATCH-UP, not a fixed window: a notebook is dispatched if its latest fire time
  is recent (<= MAX_CATCHUP_MINUTES old) AND hasn't been dispatched yet. So a
  delayed or skipped heartbeat tick makes a job run slightly late — never
  silently dropped.
- DEDUP with no extra infra: "already dispatched" is read from the run_notebook
  run history itself — run_notebook stamps `"{notebook_id} @{fire_time} …"` into
  its run-name, and we parse those. No DB writes, no state table, no new secret.
- Dispatches run_notebook.yml directly via the workflow's GITHUB_TOKEN
  (workflow_dispatch is a documented recursion exception, so no PAT needed).

Env:
  DATABASE_URL        Neon connection string (read-only is fine)
  GH_TOKEN            token with actions:write (the workflow's GITHUB_TOKEN)
  GH_REPO             "owner/repo"
  GH_REF              git ref to dispatch run_notebook.yml on
  MAX_CATCHUP_MINUTES how stale a fire may be and still be caught up (default 120)
  PLAN_ONLY           "1" → log due notebooks, make no dispatch calls
  DISPATCH_DRY_RUN    "1" → dispatch run_notebook with dry_run=true
  FORCE_NOTEBOOK      dispatch ONE named notebook (dry-run) regardless of schedule
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


def most_recent_fire(schedule: str, now: datetime) -> datetime:
    """Most recent scheduled fire time <= now, in now's timezone.

    Base at now+1s so a fire landing exactly on `now` counts (croniter.get_prev
    is strict "<")."""
    return croniter(schedule, now + timedelta(seconds=1)).get_prev(datetime)


def is_due(schedule: str, now: datetime, window_minutes: int) -> bool:
    """True if `schedule` fired within the last `window_minutes` (legacy window
    check; catch-up mode below is what the heartbeat uses)."""
    delta = now - most_recent_fire(schedule, now)
    return timedelta(0) <= delta < timedelta(minutes=window_minutes)


def fire_key(notebook_id: str, fire: datetime) -> str:
    """Canonical dedup key = notebook_id@YYYY-MM-DDTHH:MM (minute precision)."""
    return f"{notebook_id}@{fire.strftime('%Y-%m-%dT%H:%M')}"


def _gryps_url_secret(alias: str) -> str:
    # Mirrors dashboard dispatch.ts: GRYPS_URL_{ALIAS}. Aliases are codenames.
    return f"GRYPS_URL_{alias.upper()}"


def _parse_run_name_key(title: str) -> str | None:
    """Extract 'notebook_id@fire' from a run_notebook run-name of the form
    '<notebook_id> @<fire> (#<n>)…'. Returns None if not a scheduled run."""
    head = title.split(" (#", 1)[0]
    if " @" not in head:
        return None
    nbid, fire = head.rsplit(" @", 1)
    nbid, fire = nbid.strip(), fire.strip()
    return f"{nbid}@{fire}" if nbid and fire else None


def recent_dispatched(repo: str, token: str) -> set[str]:
    """Set of fire_keys already dispatched, read from recent run_notebook runs
    (the run history is our dedup store — no DB needed)."""
    req = urllib.request.Request(
        f"{GH_API}/repos/{repo}/actions/workflows/run_notebook.yml/runs?per_page=100",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    keys = set()
    for run in data.get("workflow_runs", []):
        key = _parse_run_name_key(run.get("display_title") or run.get("name") or "")
        if key:
            keys.add(key)
    return keys


def dispatch_run_notebook(nb: dict, *, repo: str, ref: str, token: str,
                          dry_run: bool, fire_iso: str = "") -> None:
    """POST a workflow_dispatch for run_notebook.yml. Raises on HTTP error.
    pipeline_name is intentionally NOT sent (real client identity; public repo)."""
    inputs = {
        "notebook_id": nb["notebook_id"],
        "alias": nb["tenant_alias"],
        "gryps_url_secret": _gryps_url_secret(nb["tenant_alias"]),
        "notebook_folder": nb.get("notebook_folder") or "",
        "notebook_file": nb.get("notebook_file") or "",
        "dry_run": "true" if dry_run else "false",
        "fire_time": fire_iso,     # stamped into run-name for dedup
    }
    body = json.dumps({"ref": ref, "inputs": inputs}).encode()
    req = urllib.request.Request(
        f"{GH_API}/repos/{repo}/actions/workflows/run_notebook.yml/dispatches",
        data=body, method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28",
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status not in (201, 204):
            raise RuntimeError(f"dispatch returned HTTP {resp.status}")


def load_notebooks(db_url: str) -> list[dict]:
    import psycopg2  # local import so unit tests don't need the driver
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
    max_catchup = int(os.environ.get("MAX_CATCHUP_MINUTES", "120"))
    plan_only = os.environ.get("PLAN_ONLY") == "1"
    dispatch_dry = os.environ.get("DISPATCH_DRY_RUN") == "1"

    if not plan_only and not token:
        sys.exit("GH_TOKEN is required unless PLAN_ONLY=1")

    now = datetime.now(TZ)
    logger.info(f"Scheduler tick {now.isoformat()} (America/New_York) "
                f"catchup<={max_catchup}m plan_only={plan_only}")

    notebooks = load_notebooks(db_url)
    logger.info(f"{len(notebooks)} scheduled notebook(s) in config")

    # Validation hook: dispatch ONE named notebook (dry-run), ignore schedule.
    force_id = os.environ.get("FORCE_NOTEBOOK")
    if force_id:
        nb = next((n for n in notebooks if n["notebook_id"] == force_id), None)
        if not nb:
            sys.exit(f"FORCE_NOTEBOOK {force_id!r} not found")
        dispatch_run_notebook(nb, repo=repo, ref=ref, token=token, dry_run=True,
                              fire_iso=now.strftime('%Y-%m-%dT%H:%M'))
        logger.info(f"Force-dispatched {force_id} (dry_run=true) — plumbing OK")
        return

    already = recent_dispatched(repo, token) if token else set()
    dispatched, errors = [], []
    for nb in notebooks:
        if nb.get("paused"):
            continue
        try:
            fire = most_recent_fire(nb["schedule"], now)
        except Exception as exc:
            logger.error(f"[{nb['notebook_id']}] bad schedule {nb['schedule']!r}: {exc}")
            errors.append((nb["notebook_id"], str(exc)))
            continue

        age_min = (now - fire).total_seconds() / 60
        if age_min > max_catchup:
            continue                              # last fire too old — skip
        key = fire_key(nb["notebook_id"], fire)
        if key in already:
            continue                              # already dispatched this fire

        if plan_only:
            logger.info(f"[due] {key} (alias={nb['tenant_alias']}, {int(age_min)}m ago) — would dispatch")
            dispatched.append(key)
            continue
        try:
            dispatch_run_notebook(nb, repo=repo, ref=ref, token=token,
                                  dry_run=dispatch_dry,
                                  fire_iso=fire.strftime('%Y-%m-%dT%H:%M'))
            already.add(key)
            dispatched.append(key)
            logger.info(f"Dispatched {key} (dry_run={dispatch_dry})")
        except (urllib.error.HTTPError, urllib.error.URLError, RuntimeError) as exc:
            detail = exc.read().decode()[:200] if isinstance(exc, urllib.error.HTTPError) else str(exc)
            logger.error(f"FAILED dispatch {key}: {detail}")
            errors.append((key, detail))

    logger.info(f"Dispatched this tick: {len(dispatched)} — {dispatched}")
    if errors:
        logger.error(f"{len(errors)} error(s): {errors}")
        sys.exit(1)


if __name__ == "__main__":
    main()
