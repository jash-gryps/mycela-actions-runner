"""
notebooks/main.py — Entry point for the notebooks pipeline.

Dispatches to the correct stage based on PIPELINE_STAGE env var.

ALIAS SYSTEM: The pipeline uses a TENANT_ALIAS (codename) in all logs,
notifications, and outputs. The real Gryps URL is resolved from a GitHub
Secret by the workflow before this script runs — it arrives as GRYPS_URL.
The alias is all that ever appears in any output visible to humans.

Environment variables required for all stages:
  PIPELINE_STAGE   1-4
  NOTEBOOK_ID      Opaque notebook identifier (e.g. harbor-cip)
  TENANT_ALIAS     Client codename for logs/notifications (e.g. harbor)
  GRYPS_URL        Real URL resolved from secret by workflow (masked in logs)
  GRYPS_USERNAME   Service account email
  GRYPS_PASSWORD   Service account password
  GITHUB_RUN_URL   Link back to this Actions run
"""

import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from shared.safe_log import safe_log

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger(__name__)

REQUIRED_ENV = [
    "PIPELINE_STAGE", "NOTEBOOK_ID", "TENANT_ALIAS",
    "GRYPS_URL", "GRYPS_USERNAME", "GRYPS_PASSWORD"
]


def validate_env():
    missing = [v for v in REQUIRED_ENV if not os.environ.get(v)]
    if missing:
        logger.error(f"Missing required environment variables: {missing}")
        logger.error("Check .env.example for the full list.")
        sys.exit(1)


def run_dry_stage(stage: int, alias: str, notebook_id: str):
    """
    Dry run: prove the runner can execute this stage without touching any
    real service. Validates env wiring, imports the stage module, launches
    a headless browser, and writes a check report — same artifact contract
    as a real stage.
    """
    from shared.check_report import CheckReport

    report = CheckReport(pipeline=f"notebook:{alias}:{notebook_id}", stage=stage)
    report.check("0.1 Env wiring", True, "All required variables present")

    stage_modules = {1: "login", 2: "jupyterlab", 3: "open_notebook", 4: "execute"}
    module_name = stage_modules[stage]
    try:
        import importlib
        importlib.import_module(f"notebooks.{module_name}")
        report.check("0.2 Stage module imports", True, f"notebooks/{module_name}.py")
    except Exception as e:
        report.require("0.2 Stage module imports", False,
                       fail_detail=f"Import failed: {e}")

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto("about:blank")
            browser.close()
        report.check("0.3 Browser launches", True, "Chromium headless OK")
    except Exception as e:
        report.require("0.3 Browser launches", False,
                       fail_detail=f"Playwright/Chromium failed: {e}")

    report.finalize(raise_on_fail=True)
    logger.info(f"[{alias}] DRY RUN stage {stage} passed — no external services contacted")


def main():
    validate_env()

    stage = int(os.environ["PIPELINE_STAGE"])
    notebook_id = os.environ["NOTEBOOK_ID"]
    alias = os.environ["TENANT_ALIAS"]   # codename — safe to log
    dry_run = os.environ.get("DRY_RUN", "").lower() in ("true", "1", "yes")

    # Log only the alias, never the real URL
    logger.info("=== Mycela Notebooks Pipeline ===")
    logger.info(f"Notebook:  {notebook_id}")
    logger.info(f"Client:    {alias}")           # codename only
    logger.info(f"Stage:     {stage}" + (" (DRY RUN)" if dry_run else ""))
    # Do NOT log GRYPS_URL — it is masked by the workflow but we avoid it anyway

    started = time.time()

    if dry_run:
        try:
            run_dry_stage(stage, alias, notebook_id)
            logger.info(f"Dry run stage {stage} completed in {time.time() - started:.0f}s")
            return
        except Exception as e:
            # Dry runs use placeholder identity, but redact as defence-in-depth.
            safe_log(logger.error, f"Dry run stage {stage} failed: {e}")
            sys.exit(1)

    if stage == 1:
        from notebooks.login import run_stage
    elif stage == 2:
        from notebooks.jupyterlab import run_stage
    elif stage == 3:
        from notebooks.open_notebook import run_stage
    elif stage == 4:
        from notebooks.execute import run_stage
    else:
        logger.error(f"Unknown PIPELINE_STAGE: {stage}. Must be 1-4.")
        sys.exit(1)

    try:
        run_stage()
        elapsed = time.time() - started
        logger.info(f"Stage {stage} completed in {elapsed:.0f}s")
    except Exception:
        elapsed = time.time() - started
        # Generic public message — the exception text may carry client DATA (notebook
        # output / error tracebacks). The failing stage already emailed the full detail
        # to jash@gryps.io via its Notifier; the run archive has the check report.
        logger.error(f"Stage {stage} failed after {elapsed:.0f}s "
                     f"— see the private email/run archive for detail")
        sys.exit(1)


if __name__ == "__main__":
    main()
