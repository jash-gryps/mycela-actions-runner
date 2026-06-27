"""
notebooks/setup_cronjobs.py — Sync notebooks.yml to cron-job.org

Reads notebooks.yml (from private config repo at path NOTEBOOKS_CONFIG_PATH,
or config/notebooks.yml.example as fallback) and idempotently syncs
scheduled jobs to cron-job.org via its REST API.

Idempotency: matches existing jobs by title. Updates if found, creates if not,
deletes if removed from YAML.

Usage:
    CRONJOB_API_KEY=xxx python notebooks/setup_cronjobs.py
    NOTEBOOKS_CONFIG_PATH=/path/to/notebooks.yml python notebooks/setup_cronjobs.py --dry-run
"""

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

import requests
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.retry import check_http_status, RateLimitError, ServerError

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

CRONJOB_API_BASE = "https://api.cron-job.org"
RATE_LIMIT_DELAYS = [30, 60, 90]  # seconds to wait on successive 429s


# ── cron-job.org integer schedule format ─────────────────────────────────────
# Each field is either -1 (all/wildcard) or a list of integers.

def parse_cron(cron_expr: str) -> dict:
    """
    Convert a 5-field cron expression to the cron-job.org schedule dict format.

    cron-job.org uses:
      { "minutes": [...], "hours": [...], "mdays": [...], "months": [...], "wdays": [...] }

    -1 means "all values" (wildcard *).
    A list of integers means those specific values.

    Examples:
      "0 7 * * 1-5"  → {"minutes": [0], "hours": [7], "mdays": [-1], "months": [-1], "wdays": [1,2,3,4,5]}
      "*/15 * * * *"  → {"minutes": [0,15,30,45], "hours": [-1], "mdays": [-1], "months": [-1], "wdays": [-1]}
      "30 9 1 * *"   → {"minutes": [30], "hours": [9], "mdays": [1], "months": [-1], "wdays": [-1]}
    """
    parts = cron_expr.strip().split()
    if len(parts) != 5:
        raise ValueError(f"Expected 5-field cron expression, got: {cron_expr!r}")

    field_names = ["minutes", "hours", "mdays", "months", "wdays"]
    field_ranges = {
        "minutes": (0, 59),
        "hours": (0, 23),
        "mdays": (1, 31),
        "months": (1, 12),
        "wdays": (0, 7),  # 0 and 7 both mean Sunday on cron-job.org
    }

    result = {}
    for name, part in zip(field_names, parts):
        lo, hi = field_ranges[name]
        result[name] = _parse_cron_field(part, lo, hi)

    return result


def _parse_cron_field(field: str, lo: int, hi: int) -> list[int] | int:
    """Parse a single cron field into a list of ints or -1 for wildcard."""
    if field == "*":
        return -1

    values = set()
    for part in field.split(","):
        if "/" in part:
            # Step: */15 or 1-30/5
            base, step_str = part.split("/", 1)
            step = int(step_str)
            if base == "*":
                start, end = lo, hi
            elif "-" in base:
                start, end = map(int, base.split("-", 1))
            else:
                start = int(base)
                end = hi
            values.update(range(start, end + 1, step))
        elif "-" in part:
            # Range: 1-5
            start, end = map(int, part.split("-", 1))
            values.update(range(start, end + 1))
        else:
            values.add(int(part))

    return sorted(values)


# ── cron-job.org API client ───────────────────────────────────────────────────

class CronJobClient:
    """Thin wrapper around the cron-job.org v1 REST API."""

    def __init__(self, api_key: str):
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    def list_jobs(self) -> list[dict]:
        return self._api_get("/jobs").get("jobs", [])

    def create_job(self, payload: dict) -> dict:
        return self._api_put("/jobs", payload)

    def update_job(self, job_id: int, payload: dict) -> dict:
        return self._api_patch(f"/jobs/{job_id}", payload)

    def delete_job(self, job_id: int) -> dict:
        return self._api_delete(f"/jobs/{job_id}")

    def _api_get(self, path: str) -> dict:
        return self._request("GET", path)

    def _api_put(self, path: str, payload: dict) -> dict:
        return self._request("PUT", path, json=payload)

    def _api_patch(self, path: str, payload: dict) -> dict:
        return self._request("PATCH", path, json=payload)

    def _api_delete(self, path: str) -> dict:
        return self._request("DELETE", path)

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{CRONJOB_API_BASE}{path}"
        for attempt, delay in enumerate([0] + RATE_LIMIT_DELAYS, start=1):
            if delay:
                logger.warning(f"[cronjobs] Rate limited — waiting {delay}s (attempt {attempt}/4)")
                time.sleep(delay)
            try:
                resp = requests.request(method, url, headers=self._headers,
                                        timeout=30, **kwargs)
                if resp.status_code == 429:
                    if attempt <= len(RATE_LIMIT_DELAYS):
                        continue  # retry with next delay
                    raise RateLimitError(f"Rate limited after {len(RATE_LIMIT_DELAYS)} retries")
                check_http_status(resp.status_code, resp.text)
                return resp.json() if resp.content else {}
            except RateLimitError:
                if attempt > len(RATE_LIMIT_DELAYS):
                    raise
                continue
        raise RateLimitError(f"Exhausted rate limit retries for {method} {path}")


# ── Sync logic ────────────────────────────────────────────────────────────────

DEFAULT_TIMEZONE = "America/New_York"


def _build_schedule(notebook: dict) -> dict:
    # Timezone must be in the schedule object — cron-job.org interprets cron
    # fields in that timezone.  Without it the API defaults to UTC, so a schedule
    # intended for 9 AM EST fires at 9 AM UTC (4–5 hours early).
    schedule = parse_cron(notebook["schedule"])
    schedule["timezone"] = notebook.get("timezone", DEFAULT_TIMEZONE)
    return schedule


def _build_create_payload(notebook: dict) -> dict:
    """Full payload for PUT (create) — includes timeout + headers."""
    return {
        "job": {
            "url": notebook.get("trigger_url", ""),
            "title": _job_title(notebook),
            "enabled": not notebook.get("paused", False),
            "schedule": _build_schedule(notebook),
            "requestTimeout": notebook.get("max_execution_minutes", 30) * 60,
            "extendedData": {"headers": {}, "body": ""},
        }
    }


def _build_update_payload(notebook: dict) -> dict:
    """Minimal payload for PATCH (update) — cron-job.org rejects requestTimeout
    and extendedData on PATCH for many plan tiers, causing HTTP 500."""
    return {
        "job": {
            "url": notebook.get("trigger_url", ""),
            "title": _job_title(notebook),
            "enabled": not notebook.get("paused", False),
            "schedule": _build_schedule(notebook),
        }
    }


def _job_title(notebook: dict) -> str:
    return f"mycela:{notebook['id']}"


def sync_notebooks(config_path: str, dry_run: bool = False):
    """
    Sync notebooks.yml to cron-job.org. Idempotent: match by title,
    update if exists, create if not, delete if removed from config.
    """
    api_key = os.environ.get("CRONJOB_API_KEY", "")
    if not api_key:
        raise EnvironmentError("CRONJOB_API_KEY environment variable is required")

    with open(config_path) as f:
        config = yaml.safe_load(f)

    notebooks = config.get("notebooks", [])
    if not notebooks:
        logger.info("[cronjobs] No notebooks in config — nothing to sync")
        return

    client = CronJobClient(api_key)

    logger.info("[cronjobs] Fetching existing jobs from cron-job.org")
    existing_jobs = client.list_jobs()
    existing_by_title = {j["title"]: j for j in existing_jobs if "title" in j}
    logger.info(f"[cronjobs] Found {len(existing_jobs)} existing job(s): "
                f"{sorted(existing_by_title.keys())}")

    desired_titles = {_job_title(nb) for nb in notebooks}

    errors = []
    # Create or update
    for notebook in notebooks:
        title = _job_title(notebook)

        if dry_run:
            action = "UPDATE" if title in existing_by_title else "CREATE"
            logger.info(f"[dry-run] {action} {title}")
            continue

        try:
            if title in existing_by_title:
                job_id = existing_by_title[title]["jobId"]
                client.update_job(job_id, _build_update_payload(notebook))
                logger.info(f"[cronjobs] Updated '{title}' (id={job_id})")
            else:
                client.create_job(_build_create_payload(notebook))
                logger.info(f"[cronjobs] Created '{title}'")
        except Exception as exc:
            logger.error(f"[cronjobs] FAILED {title}: {exc}")
            errors.append((title, str(exc)))

    # Delete jobs that are no longer in config (only mycela-owned jobs)
    for title, job in existing_by_title.items():
        if title.startswith("mycela:") and title not in desired_titles:
            if dry_run:
                logger.info(f"[dry-run] DELETE {title}")
                continue
            try:
                client.delete_job(job["jobId"])
                logger.info(f"[cronjobs] Deleted '{title}' (removed from config)")
            except Exception as exc:
                logger.error(f"[cronjobs] FAILED delete '{title}': {exc}")
                errors.append((title, str(exc)))

    if errors:
        logger.error(f"[cronjobs] {len(errors)} error(s): {errors}")
        sys.exit(1)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync notebooks.yml to cron-job.org")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would change without making API calls")
    parser.add_argument("--config", default=None,
                        help="Path to notebooks.yml (defaults to NOTEBOOKS_CONFIG_PATH env var "
                             "or config/notebooks.yml.example)")
    args = parser.parse_args()

    config_path = (
        args.config
        or os.environ.get("NOTEBOOKS_CONFIG_PATH")
        or str(Path(__file__).parent.parent / "config" / "notebooks.yml.example")
    )

    if not Path(config_path).exists():
        logger.error(f"Config file not found: {config_path}")
        sys.exit(1)

    logger.info(f"[cronjobs] Config: {config_path} | dry-run={args.dry_run}")
    sync_notebooks(config_path, dry_run=args.dry_run)
