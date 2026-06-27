"""
notebooks/finalize.py — Final stage: archive artifacts, record the run, notify.

Runs regardless of whether earlier stages succeeded or failed (if: always()).

1. Reads each stage's check report back from the private GDrive run folder (the
   per-stage uploader in report.py put it there — there is NO public-artifact
   transport on a public repo) and uploads the run summary to the same folder:
       {alias}/{YYYY-MM}/run-{number}-{date}/
           summary.json            ← written here
           check_report_stage1..4.json   ← uploaded per-stage by report.py
           screenshots/            ← uploaded per-stage by report.py (failures only)
2. Upserts the run record into the database (skipped gracefully if no DATABASE_URL).
3. Sends the final success or failure notification to jash@gryps.io.

GDrive and DB failures never abort finalize — the notification always goes out.
"""

import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.notify import Notifier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

STAGE_LABELS = {
    1: "Login",
    2: "JupyterLab",
    3: "Open Notebook",
    4: "Execute",
}

ARTIFACTS_DIR = Path("artifacts")


def _load_stage_reports(notebook_id: str, run_id: str) -> dict[int, dict]:
    """
    Read each stage's check report from the audit table (notebook_stage_results),
    written per-stage by notebooks/report.py. The DB is the shared store between the
    separate stage jobs. Returns {} if DATABASE_URL is unset/unreachable — finalize
    must still send the notification (built from the per-stage job results).
    """
    reports = {}
    if not os.environ.get("DATABASE_URL", ""):
        return reports
    try:
        run_id_int = int(run_id) if run_id else 0
    except ValueError:
        return reports
    try:
        from shared.db import get_db
        db = get_db()
        rows = db.fetch_all(
            "SELECT sr.stage, sr.check_report FROM notebook_stage_results sr "
            "JOIN notebook_runs r ON sr.run_id = r.id "
            "WHERE r.notebook_id = %s AND r.github_run_id = %s",
            (notebook_id, run_id_int),
        )
        for row in rows:
            cr = row["check_report"]
            if isinstance(cr, str):
                cr = json.loads(cr)
            if cr:
                reports[int(row["stage"])] = cr
    except Exception as e:
        logger.error(f"[finalize] Could not load stage reports from DB (continuing): {e}")
    return reports


def _stage_duration_seconds(report: dict) -> int | None:
    """Compute a stage's duration from its check report timestamps."""
    try:
        from datetime import datetime
        start = datetime.fromisoformat(report["started_at"])
        end = datetime.fromisoformat(report["finalized_at"])
        return int((end - start).total_seconds())
    except Exception:
        return None


def _build_stage_breakdown(stage_results: dict[int, str],
                           stage_reports: dict[int, dict]) -> list[str]:
    """One line per stage for the success/failure email, e.g. '✓ Login 12s'."""
    lines = []
    for stage in range(1, 5):
        label = STAGE_LABELS[stage]
        result = stage_results.get(stage, "unknown")
        icon = {"success": "✓", "failure": "✗", "skipped": "–"}.get(result, "?")
        duration = None
        if stage in stage_reports:
            duration = _stage_duration_seconds(stage_reports[stage])
        suffix = f" {duration}s" if duration is not None else ""
        lines.append(f"{icon} {label}{suffix}")
    return lines


def _detect_code_status(overall: str, stage_results: dict[int, str],
                        stage_reports: dict[int, dict]) -> str:
    """
    Classify the run outcome. Always returns a non-None value so code_status
    is never NULL in the DB.

      'success'    - all stages passed
      'code_error' - bot worked fine; a notebook cell raised a Python error
      'bot_error'  - the automation infrastructure itself failed (login, browser, etc.)
    """
    if overall != "FAIL":
        return "success"
    if any(stage_results.get(s) == "failure" for s in [1, 2, 3]):
        return "bot_error"
    if stage_results.get(4) != "failure":
        return "bot_error"
    for check in stage_reports.get(4, {}).get("checks", []):
        if "No cell errors" in check.get("name", "") and check.get("status") == "FAIL":
            return "code_error"
    # Stage 4 failed but stage_reports empty or check not found — treat as bot error
    return "bot_error"


def _record_run_in_db(notebook_id: str, alias: str, run_id: str, run_number: str,
                      github_run_url: str, overall: str, duration_seconds: int,
                      code_status: str | None = None):
    """
    Upsert the run record. Retries up to 3 times on DB failure (Neon cold-start,
    pool exhaustion, transient timeout). If all attempts fail, logs CRITICAL and
    prints to stderr — the row will remain stuck as status='running' and must be
    investigated. Never raises, so the notification step always executes.
    """
    if not os.environ.get("DATABASE_URL", ""):
        logger.warning("[finalize] DATABASE_URL not set — skipping DB record")
        return

    try:
        run_id_int = int(run_id) if run_id else 0
        run_number_int = int(run_number) if run_number else 0
    except ValueError:
        logger.warning("[finalize] Non-numeric run identifiers — skipping DB record")
        return

    status = "success" if overall == "PASS" else "failure"
    sql = """
        INSERT INTO notebook_runs
            (notebook_id, tenant_alias, github_run_id, github_run_number,
             github_run_url, status, code_status, started_at, finished_at, duration_seconds)
        VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(), NOW(), %s)
        ON CONFLICT (notebook_id, github_run_id)
        DO UPDATE SET
            status = EXCLUDED.status,
            code_status = EXCLUDED.code_status,
            finished_at = EXCLUDED.finished_at,
            duration_seconds = EXCLUDED.duration_seconds
    """
    params = (notebook_id, alias, run_id_int, run_number_int,
              github_run_url, status, code_status, duration_seconds)

    last_exc = None
    for attempt in range(1, 4):
        try:
            from shared.db import get_db
            db = get_db()
            # Log which Neon database we're connected to — helps verify correct DB target
            try:
                row = db.fetch_one("SELECT current_database() AS db, current_user AS usr")
                if row:
                    logger.info(f"[finalize] Connected to DB: db={row['db']} user={row['usr']}")
            except Exception:
                pass
            db.execute(sql, params)
            logger.info(
                f"[finalize] Recorded run in DB: {notebook_id} #{run_number} → {status}"
                + (f" [{code_status}]" if code_status else "")
            )
            return
        except Exception as e:
            last_exc = e
            logger.warning(f"[finalize] DB write attempt {attempt}/3 failed: {e}")
            if attempt < 3:
                time.sleep(5)

    # All retries exhausted — this row will stay stuck as status='running'.
    msg = (
        f"[finalize] CRITICAL: all 3 DB write attempts failed for "
        f"{notebook_id} run #{run_number} (github_run_id={run_id}). "
        f"Row will remain stuck as status='running'. "
        f"Last error: {last_exc}"
    )
    logger.critical(msg)
    print(msg, file=sys.stderr)


def finalize():
    notebook_id = os.environ.get("NOTEBOOK_ID", "unknown")
    alias = os.environ.get("TENANT_ALIAS", "unknown")
    run_number = os.environ.get("GITHUB_RUN_NUMBER", "0")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    github_run_url = f"https://github.com/{repo}/actions/runs/{run_id}" if run_id else ""

    # Determine overall result from stage results passed as env vars
    stage_results = {
        1: os.environ.get("JOB_1_RESULT", "unknown"),
        2: os.environ.get("JOB_2_RESULT", "unknown"),
        3: os.environ.get("JOB_3_RESULT", "unknown"),
        4: os.environ.get("JOB_4_RESULT", "unknown"),
    }

    failed_stages = [s for s, r in stage_results.items() if r == "failure"]
    overall = "FAIL" if failed_stages else "PASS"

    logger.info(f"=== Finalize: [{alias}] {notebook_id} run-{run_number} === {overall}")
    logger.info(f"Stage results: {stage_results}")

    ARTIFACTS_DIR.mkdir(exist_ok=True)

    # Read the per-stage check reports from the audit table (written per-stage by
    # report.py). The DB is the durable 6-month audit store — no Google Drive.
    stage_reports = _load_stage_reports(notebook_id, run_id)
    stage_breakdown = _build_stage_breakdown(stage_results, stage_reports)
    total_duration = sum(
        d for d in (_stage_duration_seconds(r) for r in stage_reports.values())
        if d is not None
    )

    # ── Record the final run result in the DB (never aborts) ──────────────────
    code_status = _detect_code_status(overall, stage_results, stage_reports)
    _record_run_in_db(notebook_id, alias, run_id, run_number,
                      github_run_url, overall, total_duration, code_status)
    gdrive_link = ""  # archive lives in the DB audit table now

    # ── 3. Send final notification (always) ───────────────────────────────────
    notifier = Notifier(
        pipeline=f"notebook:{alias}:{notebook_id}",
        stage=5,  # Finalize is stage 5
        tenant_url=alias,
        github_run_url=github_run_url
    )

    if overall == "PASS":
        notifier.success(
            duration_seconds=total_duration,
            summary=f"All 4 stages completed — run #{run_number}",
            stage_breakdown=stage_breakdown,
            gdrive_link=gdrive_link,
        )
    elif code_status == "code_error":
        # Bot succeeded (stages 1-4 mechanically fine); a cell raised a Python
        # error. Distinct from a bot failure so the email is actionable.
        stage4_report = stage_reports.get(4)
        notifier.failure(
            error=RuntimeError(
                "A cell in the notebook raised a Python error. "
                "The automation (login, JupyterLab, execution) completed "
                "successfully — the error is in the notebook's own code."
            ),
            check_report=_DictCheckReport(stage4_report) if stage4_report else None,
            context=f"Run #{run_number} — notebook code error (bot OK)",
            remediation="needs-investigation",
        )
    else:
        failed_names = [STAGE_LABELS.get(s, f"Stage {s}") for s in failed_stages]
        error_msg = f"Pipeline failed at: {', '.join(failed_names)}"
        # Attach the failed stage's check report so the email shows the failing check
        first_failed_report = stage_reports.get(failed_stages[0]) if failed_stages else None
        notifier.failure(
            error=RuntimeError(error_msg),
            check_report=_DictCheckReport(first_failed_report) if first_failed_report else None,
            context=f"Run #{run_number} — failed stages: {failed_names}",
            remediation="needs-investigation",
        )


class _DictCheckReport:
    """Adapter: lets a parsed check-report JSON dict satisfy the Notifier's
    check_report interface (it only reads .checks)."""

    def __init__(self, report_dict: dict):
        self.checks = report_dict.get("checks", [])


if __name__ == "__main__":
    finalize()
