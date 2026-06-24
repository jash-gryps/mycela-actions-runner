"""
notebooks/report.py — Record a stage's check report to the audit table (Neon).

Runs at the end of every stage job (success or failure). Each GitHub Actions stage
runs on a fresh machine, so the DB is the shared, durable audit store between jobs and
across the 6-month retention window — replacing the old Google Drive archive (service
accounts have no Drive quota; the DB is queryable and pruned by db_cleanup).

Writes:
  - upserts the run row in notebook_runs (status 'running' until finalize sets the result)
  - upserts this stage's row in notebook_stage_results (result + full check_report JSONB)

Alias-only: tenant_alias is the codename; no real client name/URL is ever stored.
Never raises — an audit-write failure must not affect the pipeline result.
"""

import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logger = logging.getLogger(__name__)

_UPSERT_RUN = """
    INSERT INTO notebook_runs
        (notebook_id, tenant_alias, github_run_id, github_run_number, status, started_at)
    VALUES (%s, %s, %s, %s, 'running', NOW())
    ON CONFLICT (notebook_id, github_run_id)
    DO UPDATE SET tenant_alias = EXCLUDED.tenant_alias
"""

_UPSERT_STAGE = """
    INSERT INTO notebook_stage_results
        (run_id, stage, result, check_report, started_at, finished_at)
    VALUES (%s, %s, %s, %s::jsonb, NOW(), NOW())
    ON CONFLICT (run_id, stage)
    DO UPDATE SET result = EXCLUDED.result,
                  check_report = EXCLUDED.check_report,
                  finished_at = NOW()
"""


def record_stage_result():
    stage = int(os.environ.get("PIPELINE_STAGE", 0))
    alias = os.environ.get("TENANT_ALIAS", "unknown")
    notebook_id = os.environ.get("NOTEBOOK_ID", "unknown")

    if not os.environ.get("DATABASE_URL", ""):
        logger.warning("[report] DATABASE_URL not set — skipping audit write")
        return

    try:
        run_id_int = int(os.environ.get("GITHUB_RUN_ID", "0"))
        run_number_int = int(os.environ.get("GITHUB_RUN_NUMBER", "0"))
    except ValueError:
        logger.warning("[report] Non-numeric run identifiers — skipping audit write")
        return

    report_path = Path(f"artifacts/check_report_stage{stage}.json")
    if not report_path.exists():
        logger.warning(f"[report] Report file not found: {report_path}")
        return

    try:
        report = json.loads(report_path.read_text())
    except Exception as e:
        logger.error(f"[report] Could not parse check report: {e}")
        return

    result = report.get("result", "running")

    try:
        from shared.db import get_db
        db = get_db()
        # Upsert the run row (creates it on the first stage), then read its id.
        db.execute(_UPSERT_RUN, (notebook_id, alias, run_id_int, run_number_int))
        row = db.fetch_one(
            "SELECT id FROM notebook_runs WHERE notebook_id = %s AND github_run_id = %s",
            (notebook_id, run_id_int),
        )
        if not row:
            logger.error("[report] Could not resolve run id — skipping stage write")
            return
        db.execute(_UPSERT_STAGE, (row["id"], stage, result, json.dumps(report)))
        logger.info(f"[report] Recorded stage {stage} result ({result}) to audit table")
    except Exception as e:
        # Auditing must never break the pipeline.
        logger.error(f"[report] Audit write failed (continuing): {e}")


if __name__ == "__main__":
    record_stage_result()
