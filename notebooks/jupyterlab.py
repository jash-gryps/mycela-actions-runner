"""
notebooks/jupyterlab.py — Stage 2: Navigate to JupyterLab.

Logs in to Gryps, navigates to the embedded JupyterLab interface,
and waits for the file browser to fully load.

Checks:
  2.1 Login succeeds
  2.2 JupyterLab link found in navigation
  2.3 JupyterLab page loads
  2.4 File browser visible
  2.5 Kernel available (WARN if not — execute stage will handle)
"""

import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from shared.check_report import CheckReport
from shared.notify import Notifier
from notebooks.auth_helper import login_to_gryps, open_jupyterlab, DEFAULT_INSTANCE

logger = logging.getLogger(__name__)

TIMEOUT_MS = 45_000       # JupyterLab is slow to load
NAV_TIMEOUT_MS = 30_000


def run_stage():
    notebook_id = os.environ["NOTEBOOK_ID"]
    gryps_url = os.environ["GRYPS_URL"].rstrip("/")
    alias = os.environ["TENANT_ALIAS"]
    username = os.environ["GRYPS_USERNAME"]
    password = os.environ["GRYPS_PASSWORD"]
    instance = os.environ.get("GRYPS_INSTANCE", DEFAULT_INSTANCE)
    github_run_url = os.environ.get("GITHUB_RUN_URL", "")

    report = CheckReport(pipeline=f"notebook:{alias}:{notebook_id}", stage=2)
    notifier = Notifier(
        pipeline=f"notebook:{alias}:{notebook_id}",
        stage=2,
        tenant_url=alias,
        github_run_url=github_run_url
    )

    started = time.time()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()

        try:
            # ── Login (re-do on fresh machine, shared helper) ──────────────
            login_to_gryps(page, gryps_url, username, password, alias)
            report.check("2.1 Login succeeds", True, "Dashboard loaded")

            # ── Navigate to JupyterLab (Notebooks → open-notebook → new tab) ─
            try:
                page = open_jupyterlab(page, alias, instance)
                report.require("2.2 JupyterLab opens", True, "",
                               pass_detail="JupyterLab/notebook UI visible")
            except PWTimeout:
                report.require("2.2 JupyterLab opens", False,
                               fail_detail=f"JupyterLab UI did not appear after {TIMEOUT_MS}ms — "
                                          "SageMaker may still be starting, or the open-notebook "
                                          "button/instance was not found")
                raise

            # ── Soft check: file list / launcher content present ───────────
            has_content = page.locator(
                "#notebook_list .item_link, .jp-DirListing-item, .jp-LauncherCard"
            ).count() > 0
            if has_content:
                report.check("2.3 Workspace content visible", True, "File list / launcher present")
            else:
                report.warn("2.3 Workspace content",
                            "No file list yet — SageMaker may still be mounting EFS")

            # ── Finalize ───────────────────────────────────────────────────
            report.finalize(raise_on_fail=True)
            elapsed = time.time() - started
            notifier.success(duration_seconds=elapsed, summary="JupyterLab loaded successfully")

        except Exception as e:
            screenshot_path = f"artifacts/stage2_failure_{int(time.time())}.png"
            Path("artifacts").mkdir(exist_ok=True)
            try:
                page.screenshot(path=screenshot_path)
            except Exception:
                pass

            try:
                report.finalize(raise_on_fail=False)  # persist to the audit on failure
            except Exception:
                pass
            notifier.failure(
                error=e,
                check_report=report,
                context=f"JupyterLab [{alias}]",
                remediation="auto-retry" if "timeout" in str(e).lower() else "needs-investigation"
            )
            raise

        finally:
            browser.close()
