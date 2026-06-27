"""
scripts/gen_notebooks_yml.py — Generate notebooks.yml from Neon for cron-job.org sync.

Reads pipeline_display from Neon and writes a notebooks.yml consumed by
notebooks/setup_cronjobs.py, which syncs the schedule to cron-job.org.

Each cron-job.org job calls:
  {DASHBOARD_URL}/api/cron/trigger?id={notebook_id}&token={CRON_SECRET}

Required env vars:
  DATABASE_URL     Neon connection string
  CRON_SECRET      Shared secret checked by the dashboard trigger route
  DASHBOARD_URL    Base URL of the Mycela dashboard (default: https://mycela.vercel.app)

Usage:
  python scripts/gen_notebooks_yml.py /tmp/notebooks.yml
"""

import os
import sys

import psycopg2
import yaml


DEFAULT_DASHBOARD_URL = "https://mycela.vercel.app"
DEFAULT_MAX_EXECUTION_MINUTES = 30


def main(output_path: str) -> None:
    db_url = os.environ.get("DATABASE_URL")
    cron_secret = os.environ.get("CRON_SECRET")
    dashboard_url = os.environ.get("DASHBOARD_URL", DEFAULT_DASHBOARD_URL).rstrip("/")

    if not db_url:
        sys.exit("DATABASE_URL is required")
    if not cron_secret:
        sys.exit("CRON_SECRET is required")

    conn = psycopg2.connect(db_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT notebook_id, schedule, paused
                FROM pipeline_display
                WHERE schedule IS NOT NULL
                ORDER BY notebook_id
                """
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    notebooks = []
    for notebook_id, schedule, paused in rows:
        trigger_url = (
            f"{dashboard_url}/api/cron/trigger"
            f"?id={notebook_id}&token={cron_secret}"
        )
        notebooks.append({
            "id": notebook_id,
            "schedule": schedule,
            "paused": bool(paused),
            "trigger_url": trigger_url,
        })

    config = {"notebooks": notebooks}

    with open(output_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True)

    print(f"Wrote {len(notebooks)} notebook(s) to {output_path}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(f"Usage: {sys.argv[0]} <output_path>")
    main(sys.argv[1])
