"""
scripts/reconcile_cronjobs.py — Force every cron-job.org job to match config
EXACTLY: correct schedule, America/New_York (US Eastern) timezone, prod trigger
URL, enabled state — and remove stale duplicates.

Why this exists: cron-job.org returns HTTP 500 when a PATCH body carries a
`schedule` (which includes the timezone) on this plan, so the ONLY reliable way
to set/fix a job's timezone or schedule is at create (PUT) time. This script
therefore reconciles by delete+recreate, but only for jobs that are actually
wrong — it reads each job first and skips those already correct, to minimise
API writes.

Safety:
- Reads each candidate job and SKIPS it when timezone + schedule already match
  (no needless churn).
- Recreates create-then-delete so a mid-run abort leaves a (harmless) duplicate
  rather than a gap with no job.
- Throttles between calls and trips a circuit breaker after a few consecutive
  rate-limit failures, so it can never run away like an un-throttled sync.
- Idempotent: safe to re-run until it reports a clean pass.

Required env vars: CRONJOB_API_KEY
Usage:
  python scripts/reconcile_cronjobs.py --config /tmp/notebooks.yml [--dry-run]
"""

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from notebooks.setup_cronjobs import (
    CronJobClient, _build_create_payload, _build_schedule, _job_title,
)
from shared.retry import RateLimitError

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

THROTTLE_SECONDS = 4.0          # space out every API call
MAX_CONSECUTIVE_RATELIMIT = 2   # circuit breaker: bail fast under sustained 429


def _id_from_url(url: str) -> str | None:
    try:
        return (parse_qs(urlparse(url).query).get("id") or [None])[0]
    except Exception:
        return None


def _schedule_matches(current: dict, desired: dict) -> bool:
    """True when the live schedule already matches desired on the fields we set."""
    if not current:
        return False
    for key in ("minutes", "hours", "mdays", "months", "wdays", "timezone"):
        if current.get(key) != desired.get(key):
            return False
    return True


class _Breaker:
    def __init__(self):
        self.consecutive = 0

    def record(self, ok: bool):
        self.consecutive = 0 if ok else self.consecutive + 1
        if self.consecutive >= MAX_CONSECUTIVE_RATELIMIT:
            raise SystemExit(
                f"Aborting: {self.consecutive} consecutive rate-limit failures. "
                "cron-job.org is throttling — re-run this job in a few minutes "
                "(it is idempotent and will resume)."
            )


def reconcile(config_path: str, dry_run: bool = False) -> None:
    api_key = os.environ.get("CRONJOB_API_KEY", "")
    if not api_key:
        sys.exit("CRONJOB_API_KEY is required")

    with open(config_path) as f:
        notebooks = (yaml.safe_load(f) or {}).get("notebooks", [])
    if not notebooks:
        logger.info("No notebooks in config — nothing to reconcile")
        return

    client = CronJobClient(api_key)
    breaker = _Breaker()

    jobs = client.list_jobs()
    by_title: dict[str, list[dict]] = {}
    for j in jobs:
        by_title.setdefault(j.get("title", ""), []).append(j)
    desired_ids = {nb["id"] for nb in notebooks}

    recreated, skipped, deleted, errors = [], [], [], []

    for nb in notebooks:
        nb_id = nb["id"]
        title = _job_title(nb)
        desired_sched = _build_schedule(nb)          # includes timezone America/New_York
        existing = by_title.get(title, [])

        # Is there already exactly one correct job? Then skip (no churn).
        correct = None
        for job in existing:
            time.sleep(THROTTLE_SECONDS)
            try:
                details = client.get_job(job["jobId"])
                breaker.record(True)
            except RateLimitError:
                breaker.record(False)
                continue
            if (_schedule_matches(details.get("schedule", {}), desired_sched)
                    and _id_from_url(details.get("url", "")) == nb_id):
                correct = job["jobId"]
                break

        if correct is not None and len(existing) == 1:
            skipped.append(title)
            continue

        if dry_run:
            logger.info(f"[dry-run] RECREATE {title} (existing job ids: "
                        f"{[j['jobId'] for j in existing]})")
            continue

        # Create fresh (PUT sets schedule + timezone reliably) BEFORE deleting the
        # old ones, so there is never a window with no job for this notebook.
        time.sleep(THROTTLE_SECONDS)
        try:
            client.create_job(_build_create_payload(nb))
            breaker.record(True)
            logger.info(f"Created fresh '{title}' (tz={desired_sched['timezone']})")
        except Exception as exc:
            breaker.record(isinstance(exc, RateLimitError) is False)
            logger.error(f"FAILED create '{title}': {exc}")
            errors.append((title, str(exc)))
            continue

        for job in existing:            # delete the stale copies now
            time.sleep(THROTTLE_SECONDS)
            try:
                client.delete_job(job["jobId"])
                breaker.record(True)
                logger.info(f"Deleted stale '{title}' (id={job['jobId']})")
            except Exception as exc:
                breaker.record(isinstance(exc, RateLimitError) is False)
                logger.error(f"FAILED delete '{title}' id={job['jobId']}: {exc}")
                errors.append((title, str(exc)))
        recreated.append(title)

    # Remove stale DUPLICATES: non-mycela jobs whose trigger URL points at one of
    # our notebook ids (e.g. legacy 'massport-cip' → ?id=harbor-nb1). Agent jobs
    # and anything not pointing at a configured id are left untouched.
    for job in jobs:
        title = job.get("title", "")
        if title.startswith("mycela:"):
            continue
        time.sleep(THROTTLE_SECONDS)
        try:
            details = client.get_job(job["jobId"])
            breaker.record(True)
        except RateLimitError:
            breaker.record(False)
            continue
        dup_id = _id_from_url(details.get("url", ""))
        if dup_id in desired_ids:
            if dry_run:
                logger.info(f"[dry-run] DELETE duplicate '{title}' (→ id={dup_id})")
                continue
            time.sleep(THROTTLE_SECONDS)
            try:
                client.delete_job(job["jobId"])
                breaker.record(True)
                deleted.append(title)
                logger.info(f"Deleted duplicate '{title}' (pointed at id={dup_id})")
            except Exception as exc:
                breaker.record(isinstance(exc, RateLimitError) is False)
                logger.error(f"FAILED delete duplicate '{title}': {exc}")
                errors.append((title, str(exc)))

    logger.info(f"Reconcile summary: recreated={len(recreated)} skipped(correct)="
                f"{len(skipped)} duplicates_deleted={len(deleted)} errors={len(errors)}")
    if errors:
        logger.error(f"{len(errors)} error(s): {errors}")
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reconcile cron-job.org jobs to config (tz-safe)")
    parser.add_argument("--config", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not Path(args.config).exists():
        sys.exit(f"Config not found: {args.config}")
    logger.info(f"Config: {args.config} | dry-run={args.dry_run}")
    reconcile(args.config, dry_run=args.dry_run)
