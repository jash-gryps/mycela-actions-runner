"""
scripts/diagnose_cronjobs.py — Read-only snapshot of cron-job.org jobs.

Prints each job's title, enabled flag, schedule, and trigger URL with the
query string stripped (so the CRON_SECRET token never lands in logs). Useful
for confirming a sync/migrate landed correctly. Makes only GET calls.

Required env vars:
  CRONJOB_API_KEY   cron-job.org API key

Usage:
  python scripts/diagnose_cronjobs.py
"""

import os
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from notebooks.setup_cronjobs import CronJobClient

SCHED_FIELDS = ("minutes", "hours", "mdays", "months", "wdays", "timezone")


def _strip_query(url: str) -> str:
    """Drop the query string (which carries the secret token) for safe logging."""
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path, "", ""))


def main() -> None:
    api_key = os.environ.get("CRONJOB_API_KEY", "")
    if not api_key:
        sys.exit("CRONJOB_API_KEY is required")

    client = CronJobClient(api_key)
    jobs = client.list_jobs()
    print(f"cron-job.org has {len(jobs)} job(s)\n")

    mycela, legacy = [], []
    for job in sorted(jobs, key=lambda j: j.get("title", "")):
        job_id = job["jobId"]
        title = job.get("title", "?")
        details = client.get_job(job_id)
        sched = {k: details.get("schedule", {}).get(k) for k in SCHED_FIELDS}
        line = (f"[{job_id}] {title} | enabled={details.get('enabled')} | "
                f"{_strip_query(details.get('url', ''))} | {sched}")
        (mycela if title.startswith("mycela:") else legacy).append(line)

    print(f"── mycela: jobs ({len(mycela)}) ──")
    for line in mycela:
        print(line)
    print(f"\n── other/legacy jobs ({len(legacy)}) ──")
    for line in legacy:
        print(line)


if __name__ == "__main__":
    main()
