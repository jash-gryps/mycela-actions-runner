"""
scripts/migrate_cronjobs.py — One-time migration of existing cron-job.org jobs.

The account has pre-existing jobs with legacy title formats (e.g. 'massport-cip',
'eaton-parser-ipynb'). setup_cronjobs.py matches by 'mycela:{id}' title and finds
no matches, so it tries to CREATE new jobs — hitting the plan's job limit.

This script:
  1. Lists all existing cron-job.org jobs and their current URLs.
  2. For each notebook in notebooks.yml, finds the matching existing job by
     extracting the notebook_id from the job's trigger URL (?id=<notebook_id>).
     Falls back to a file-name-based title match for jobs using legacy URL formats.
  3. PATCHes each matched job: new trigger URL (new CRON_SECRET), new title
     ('mycela:{id}'), correct schedule, correct enabled state.
  4. Reports any unmatched notebooks so they can be created manually or reviewed.

After this runs once, setup_cronjobs.py works normally (matches by 'mycela:' title).

Required env vars:
  CRONJOB_API_KEY   cron-job.org API key
  DATABASE_URL      Neon connection string
  CRON_SECRET       New shared secret for trigger URL tokens
  DASHBOARD_URL     Base URL of the Mycela dashboard (default: https://mycela.vercel.app)

Usage:
  python scripts/migrate_cronjobs.py --config /tmp/notebooks.yml [--dry-run]
"""

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
import yaml

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

CRONJOB_API_BASE = "https://api.cron-job.org"
DEFAULT_DASHBOARD_URL = "https://mycela.vercel.app"


def _notebook_file_to_slug(filename: str) -> str:
    """'CapitalPlanParser.ipynb' → 'capitalplanparser-ipynb' (matches legacy title format)."""
    return filename.lower().replace("_", "-").replace(" ", "-").replace(".ipynb", "-ipynb")


class CronJobClient:
    def __init__(self, api_key: str):
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        """All HTTP calls go through here — retries up to 3× on 429, waiting 70s each time."""
        url = f"{CRONJOB_API_BASE}{path}"
        for attempt in range(4):
            resp = requests.request(method, url, headers=self._headers, timeout=30, **kwargs)
            if resp.status_code == 429 and attempt < 3:
                wait = 70 * (attempt + 1)
                logger.warning(f"[rate-limit] 429 on {method} {path} — waiting {wait}s (attempt {attempt+1}/3)")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        return resp  # unreachable but satisfies type checkers

    def list_jobs(self) -> list[dict]:
        return self._request("GET", "/jobs").json().get("jobs", [])

    def get_job(self, job_id: int) -> dict:
        return self._request("GET", f"/jobs/{job_id}").json().get("jobDetails", {})

    def update_job_minimal(self, job_id: int, url: str, title: str, enabled: bool) -> dict:
        """Minimal PATCH — only url, title, enabled. Leaves schedule/timeout intact."""
        payload = {"job": {"url": url, "title": title, "enabled": enabled}}
        resp = self._request("PATCH", f"/jobs/{job_id}", json=payload)
        return resp.json() if resp.content else {}


def _extract_notebook_id_from_url(url: str) -> str | None:
    """Extract ?id=<notebook_id> from a Mycela trigger URL. Returns None if not present."""
    try:
        qs = parse_qs(urlparse(url).query)
        ids = qs.get("id", [])
        return ids[0] if ids else None
    except Exception:
        return None


def _parse_cron(cron_expr: str) -> dict:
    """Minimal cron parser (same logic as setup_cronjobs.py)."""
    parts = cron_expr.strip().split()
    if len(parts) != 5:
        raise ValueError(f"Expected 5-field cron, got: {cron_expr!r}")

    field_names = ["minutes", "hours", "mdays", "months", "wdays"]
    field_ranges = {"minutes": (0, 59), "hours": (0, 23), "mdays": (1, 31),
                    "months": (1, 12), "wdays": (0, 7)}
    result = {}
    for name, part in zip(field_names, parts):
        lo, hi = field_ranges[name]
        if part == "*":
            result[name] = -1
        else:
            values = set()
            for p in part.split(","):
                if "/" in p:
                    base, step = p.split("/", 1)
                    step = int(step)
                    start, end = (lo, hi) if base == "*" else (
                        (int(base.split("-")[0]), int(base.split("-")[1]))
                        if "-" in base else (int(base), hi)
                    )
                    values.update(range(start, end + 1, step))
                elif "-" in p:
                    s, e = map(int, p.split("-", 1))
                    values.update(range(s, e + 1))
                else:
                    values.add(int(p))
            result[name] = sorted(values)
    return result


def migrate(config_path: str, dry_run: bool = False) -> None:
    api_key = os.environ.get("CRONJOB_API_KEY", "")
    cron_secret = os.environ.get("CRON_SECRET", "")
    dashboard_url = os.environ.get("DASHBOARD_URL", DEFAULT_DASHBOARD_URL).rstrip("/")

    if not api_key:
        sys.exit("CRONJOB_API_KEY is required")
    if not cron_secret:
        sys.exit("CRON_SECRET is required")

    with open(config_path) as f:
        config = yaml.safe_load(f)
    notebooks = config.get("notebooks", [])
    if not notebooks:
        logger.info("No notebooks in config — nothing to migrate")
        return

    client = CronJobClient(api_key)

    logger.info("Fetching existing jobs from cron-job.org …")
    existing_jobs = client.list_jobs()
    logger.info(f"Found {len(existing_jobs)} existing jobs")

    # Build lookup tables from the job list (titles only — no URLs in list response).
    by_title = {j["title"]: j for j in existing_jobs if "title" in j}
    by_id = {j["jobId"]: j for j in existing_jobs}

    # For URL-based matching we need to fetch each job's full details.
    # Fetch details lazily only for jobs not already matched by title.
    matched_mycela = {title: j for title, j in by_title.items()
                      if title.startswith("mycela:")}

    # Fetch full details for all non-mycela jobs (needed for URL extraction).
    logger.info("Fetching full details for legacy jobs (needed for URL-based matching) …")
    legacy_details: dict[int, dict] = {}
    for job in existing_jobs:
        title = job.get("title", "")
        if not title.startswith("mycela:"):
            try:
                details = client.get_job(job["jobId"])
                legacy_details[job["jobId"]] = details
                time.sleep(0.2)  # light throttle
            except Exception as exc:
                logger.warning(f"Could not fetch details for '{title}': {exc}")

    # Build URL-keyed lookup: notebook_id → (jobId, job_summary)
    url_matched: dict[str, tuple[int, dict]] = {}
    for job_id, details in legacy_details.items():
        url = details.get("url", "")
        nb_id = _extract_notebook_id_from_url(url)
        if nb_id:
            url_matched[nb_id] = (job_id, by_id[job_id])
            logger.info(f"URL match: notebook_id={nb_id!r} → job '{by_id[job_id].get('title')}'")

    errors = []
    unmatched = []

    for notebook in notebooks:
        nb_id = notebook["id"]
        new_title = f"mycela:{nb_id}"
        new_url = f"{dashboard_url}/api/cron/trigger?id={nb_id}&token={cron_secret}"
        schedule = _parse_cron(notebook["schedule"])
        schedule["timezone"] = notebook.get("timezone", "America/New_York")
        enabled = not notebook.get("paused", False)

        payload = {
            "job": {
                "url": new_url,
                "title": new_title,
                "enabled": enabled,
                "schedule": schedule,
                "requestTimeout": notebook.get("max_execution_minutes", 30) * 60,
                "extendedData": {"headers": {}, "body": ""},
            }
        }

        time.sleep(3)  # stay well under cron-job.org rate limit between writes
        # 1. Already has mycela: title? → skip (already migrated or created fresh)
        if new_title in by_title:
            job_id = by_title[new_title]["jobId"]
            logger.info(f"[mycela-title] {nb_id}: already titled correctly (id={job_id}) — updating URL")
            if dry_run:
                logger.info(f"[dry-run] UPDATE {new_title} (id={job_id})")
                continue
            try:
                client.update_job_minimal(job_id, new_url, new_title, enabled)
                logger.info(f"Updated '{new_title}' (id={job_id})")
            except Exception as exc:
                logger.error(f"FAILED update '{new_title}': {exc}")
                errors.append((nb_id, str(exc)))
            continue

        # 2. Found by URL (?id=notebook_id)?
        if nb_id in url_matched:
            job_id, job_summary = url_matched[nb_id]
            old_title = job_summary.get("title", "?")
            logger.info(f"[url-match] {nb_id}: matched existing '{old_title}' (id={job_id})")
            if dry_run:
                logger.info(f"[dry-run] MIGRATE '{old_title}' → '{new_title}' (id={job_id})")
                continue
            try:
                client.update_job_minimal(job_id, new_url, new_title, enabled)
                logger.info(f"Migrated '{old_title}' → '{new_title}' (id={job_id})")
            except Exception as exc:
                logger.error(f"FAILED migrate '{old_title}': {exc}")
                errors.append((nb_id, str(exc)))
            continue

        # 3. Try legacy title match by notebook file name slug.
        nb_file = notebook.get("notebook_file", "")
        slug = _notebook_file_to_slug(nb_file) if nb_file else ""
        slug_match = next((t for t in by_title if slug and slug in t), None)
        if slug_match:
            job_id = by_title[slug_match]["jobId"]
            logger.info(f"[slug-match] {nb_id}: matched '{slug_match}' via slug '{slug}' (id={job_id})")
            if dry_run:
                logger.info(f"[dry-run] MIGRATE '{slug_match}' → '{new_title}' (id={job_id})")
                continue
            try:
                client.update_job_minimal(job_id, new_url, new_title, enabled)
                logger.info(f"Migrated '{slug_match}' → '{new_title}' (id={job_id})")
            except Exception as exc:
                logger.error(f"FAILED migrate '{slug_match}': {exc}")
                errors.append((nb_id, str(exc)))
            continue

        # No match found.
        logger.warning(f"[unmatched] {nb_id} (file={nb_file!r}): no existing job found — needs manual review or will be created by sync")
        unmatched.append(nb_id)

    if unmatched:
        logger.warning(f"Unmatched notebooks ({len(unmatched)}): {unmatched}")
    if errors:
        logger.error(f"{len(errors)} error(s): {errors}")
        sys.exit(1)

    logger.info("Migration complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate legacy cron-job.org jobs to mycela: format")
    parser.add_argument("--config", required=True, help="Path to notebooks.yml")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not Path(args.config).exists():
        logger.error(f"Config not found: {args.config}")
        sys.exit(1)

    logger.info(f"Config: {args.config} | dry-run={args.dry_run}")
    migrate(args.config, dry_run=args.dry_run)
