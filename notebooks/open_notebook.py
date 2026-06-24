"""
notebooks/open_notebook.py — Stage 3: Open the target notebook.

Logs in, navigates to JupyterLab, and opens the notebook file
specified in NOTEBOOK_PATH. Handles the kernel restart dialog.

Checks:
  3.1 Login succeeds
  3.2 JupyterLab loads
  3.3 Notebook path found in file browser
  3.4 Notebook opens
  3.5 Cells visible (notebook content loaded)
  3.6 Kernel dialog handled
"""

import logging
import os
import sys
import time
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from shared.check_report import CheckReport
from shared.notify import Notifier
from notebooks.auth_helper import (login_to_gryps, open_jupyterlab,
                                    open_notebook as open_notebook_file, DEFAULT_INSTANCE)

logger = logging.getLogger(__name__)


def _classify_open_failure(report) -> str:
    """
    Decide the remediation label for a stage-3 failure. A failed "3.4 Notebook opens"
    check means the notebook URL did not resolve — almost always a stale config entry
    (the notebook was renamed/moved/deleted in the tenant), which is exactly the failure
    class that plagued the legacy system. Label it a config fix so it is corrected in
    notebooks.yml rather than retried blindly or mislabelled an infrastructure error.
    """
    for c in report.checks:
        if c.get("name", "").startswith("3.4") and c.get("status") == "FAIL":
            return ("fix-config: notebook path may not exist in the tenant "
                    "(renamed/moved/deleted) — verify the entry in notebooks.yml")
    return "needs-investigation"


TIMEOUT_MS = 45_000
NAV_TIMEOUT_MS = 30_000
NOTEBOOK_LOAD_TIMEOUT_MS = 60_000


def run_stage():
    notebook_id = os.environ["NOTEBOOK_ID"]
    gryps_url = os.environ["GRYPS_URL"].rstrip("/")
    alias = os.environ["TENANT_ALIAS"]
    notebook_folder = os.environ.get("NOTEBOOK_FOLDER", "")
    notebook_file = os.environ.get("NOTEBOOK_FILE", "")
    instance = os.environ.get("GRYPS_INSTANCE", DEFAULT_INSTANCE)
    username = os.environ["GRYPS_USERNAME"]
    password = os.environ["GRYPS_PASSWORD"]
    github_run_url = os.environ.get("GITHUB_RUN_URL", "")

    if not notebook_file:
        raise EnvironmentError("NOTEBOOK_FILE environment variable is required for Stage 3")

    report = CheckReport(pipeline=f"notebook:{alias}:{notebook_id}", stage=3)
    notifier = Notifier(
        pipeline=f"notebook:{alias}:{notebook_id}",
        stage=3,
        tenant_url=alias,
        github_run_url=github_run_url
    )

    started = time.time()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()

        try:
            # ── Login + JupyterLab (re-do on fresh machine) ────────────────
            login_to_gryps(page, gryps_url, username, password, alias)
            report.check("3.1 Login succeeds", True, "Dashboard loaded")
            page = open_jupyterlab(page, alias, instance)
            report.check("3.2-3.3 JupyterLab loads", True, "Notebook workspace visible")

            # ── Open the notebook (folder → file → new tab → classic UI) ────
            # open_notebook_file navigates the file browser and dismisses the
            # kernel dialog; it raises on timeout (notebook likely missing).
            try:
                page = open_notebook_file(page, notebook_folder, notebook_file, alias)
                report.require("3.4 Notebook opens", True, "",
                               pass_detail="Notebook container + cells visible")
            except PWTimeout:
                report.require("3.4 Notebook opens", False,
                               fail_detail="Notebook did not open — the folder/file may not "
                                          "exist in the tenant (stale config)")
                raise

            # ── Check cells visible ────────────────────────────────────────
            cell_count = page.locator("#notebook-container .cell, .jp-Cell").count()
            report.require(
                "3.5 Cells loaded",
                condition=cell_count > 0,
                fail_detail="No cells visible — notebook may be empty or failed to render",
                pass_detail=f"{cell_count} cells visible"
            )

            # ── Finalize ───────────────────────────────────────────────────
            report.finalize(raise_on_fail=True)
            elapsed = time.time() - started
            notifier.success(duration_seconds=elapsed,
                             summary=f"Notebook opened: {cell_count} cells")

        except Exception as e:
            screenshot_path = f"artifacts/stage3_failure_{int(time.time())}.png"
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
                context=f"Open notebook [{alias}]",
                remediation=_classify_open_failure(report)
            )
            raise

        finally:
            browser.close()


def _handle_kernel_dialog(page, report):
    """
    Handle the kernel restart/selection dialog that appears when opening
    a notebook that was previously interrupted or has no kernel attached.
    Tries to select python3 kernel or restart the existing kernel.
    """
    # Give the dialog a moment to appear
    page.wait_for_timeout(2000)

    # Check for kernel selection dialog (JupyterLab 3.x / 4.x)
    dialog = page.locator("[data-jp-kernel-selector-id], .jp-KernelSelector, [role='dialog']")
    if dialog.count() > 0 and dialog.first.is_visible():
        logger.info("Kernel dialog detected — selecting python3")
        try:
            # Try to find and click python3 option
            python3_option = page.locator("li:has-text('Python 3'), option:has-text('Python 3')").first
            if python3_option.count() > 0:
                python3_option.click()
                page.locator("button:has-text('Select'), button:has-text('OK')").first.click()
                report.check("3.6 Kernel selected", True, "python3 kernel selected from dialog")
            else:
                # Click the first available kernel
                page.locator("[data-jp-kernel-selector-id] li, .jp-KernelSelector li").first.click()
                page.locator("button:has-text('Select'), button:has-text('OK')").first.click()
                report.warn("3.6 Kernel selection", "python3 not found — selected first available kernel")
        except Exception as e:
            report.warn("3.6 Kernel dialog", f"Could not handle kernel dialog: {e}")
    else:
        # No dialog — kernel may already be attached
        report.check("3.6 No kernel dialog", True, "Kernel already attached or no dialog appeared")
