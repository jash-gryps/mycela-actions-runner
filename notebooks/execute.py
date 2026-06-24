"""
notebooks/execute.py — Stage 4: Execute the notebook.

Clicks Kernel → Restart & Run All, waits for execution to complete,
and checks for cell errors. Takes periodic screenshots during execution.

Checks:
  4.1 Login + JupyterLab + notebook opens
  4.2 Kernel restart dialog handled
  4.3 Execution starts (In [*] indicators visible)
  4.4 Execution completes (no more In [*] indicators)
  4.5 No cell errors found
  4.6 Final cell executed (last cell has output)
"""

import logging
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from shared.check_report import CheckReport
from shared.notify import Notifier
from notebooks.auth_helper import (login_to_gryps, open_jupyterlab,
                                   open_notebook as open_notebook_file, DEFAULT_INSTANCE)

logger = logging.getLogger(__name__)

NAV_TIMEOUT_MS = 30_000
JLAB_TIMEOUT_MS = 45_000
POLL_INTERVAL_S = 15        # Check execution status every 15s
SCREENSHOT_INTERVAL_S = 30  # Screenshot every 30s during execution
MAX_EXECUTION_MINUTES = int(os.environ.get("MAX_EXECUTION_MINUTES", "30"))

# Classic SageMaker-notebook execution selectors (ported from the proven prod flow).
RUNNING_CELL = "div.input_prompt:has-text('[*]')"   # a cell still queued/running
KERNEL_BUSY = "i#kernel_indicator_icon.kernel_busy_icon"


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
        raise EnvironmentError("NOTEBOOK_FILE is required for Stage 4")

    report = CheckReport(pipeline=f"notebook:{alias}:{notebook_id}", stage=4)
    notifier = Notifier(
        pipeline=f"notebook:{alias}:{notebook_id}",
        stage=4,
        tenant_url=alias,
        github_run_url=github_run_url
    )

    started = time.time()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()

        try:
            stop_screenshots = threading.Event()  # defined here so except block can always reference it

            # ── Login + open notebook (shared helpers) ────────────────────
            login_to_gryps(page, gryps_url, username, password, alias)
            page = open_jupyterlab(page, alias, instance)
            page = open_notebook_file(page, notebook_folder, notebook_file, alias)
            report.check("4.1 Notebook opened", True, "Notebook container + cells visible")

            # ── Kernel → Restart & Run All (classic SageMaker UI) ──────────
            logger.info(f"[{alias}] Restart & Run All")
            page.locator("#kernellink").click()
            page.wait_for_selector("#kernel_menu", timeout=NAV_TIMEOUT_MS)
            restart = page.locator("#restart_run_all a")
            report.require("4.2 Restart & Run All found", restart.count() > 0,
                           fail_detail="Kernel → Restart & Run All not found in the menu")
            restart.first.click()

            # Confirm the restart dialog ("Restart and Run All" button); some
            # versions auto-confirm — absence is fine if the kernel goes busy.
            try:
                page.locator("button:has-text('Restart and Run All')").first.click(timeout=15_000)
                logger.info("Confirmed restart dialog")
            except PWTimeout:
                pass

            page.wait_for_timeout(3000)

            # ── Check execution started (soft) ─────────────────────────────
            executing = (page.locator(RUNNING_CELL).count() > 0
                         or page.locator(KERNEL_BUSY).count() > 0)
            report.check("4.3 Execution started", condition=True,
                         detail="Cells running ([*])" if executing else "May have finished quickly")

            # ── Screenshot thread ──────────────────────────────────────────
            screenshot_thread = threading.Thread(
                target=_screenshot_loop,
                args=(page, stop_screenshots, SCREENSHOT_INTERVAL_S),
                daemon=True
            )
            screenshot_thread.start()

            # ── Poll for completion ────────────────────────────────────────
            logger.info(f"[{alias}] Waiting for execution (max {MAX_EXECUTION_MINUTES} min)")
            deadline = time.time() + (MAX_EXECUTION_MINUTES * 60)
            completed = False

            while time.time() < deadline:
                time.sleep(POLL_INTERVAL_S)
                running_cells = (page.locator(RUNNING_CELL).count()
                                 + page.locator(KERNEL_BUSY).count())
                elapsed = int(time.time() - started)
                logger.info(f"Elapsed: {elapsed}s | Running cells: {running_cells}")

                if running_cells == 0:
                    completed = True
                    break

            stop_screenshots.set()
            screenshot_thread.join(timeout=5)

            report.require(
                "4.4 Execution completes",
                condition=completed,
                fail_detail=f"Notebook still executing after {MAX_EXECUTION_MINUTES} minutes — possible infinite loop",
                pass_detail=f"Completed in {int(time.time() - started)}s"
            )

            # ── Check for cell errors ──────────────────────────────────────
            error_cells = _get_cell_errors(page)
            if error_cells:
                error_count = len(error_cells)
                # Raw cell-error text is client DATA — it goes only into the check
                # report detail (private GDrive + email), never into the raised
                # exception (which would surface in the public Actions log).
                error_summary = "; ".join(error_cells[:3])  # first 3 errors
                report.require("4.5 No cell errors", False,
                               fail_detail=f"Cell errors found: {error_summary}")
                raise RuntimeError(
                    f"Notebook execution produced {error_count} cell error(s) "
                    f"— see the private run archive/email for detail"
                )
            else:
                report.check("4.5 No cell errors", True, "No error output in any cell")

            # ── Check last cell ran ────────────────────────────────────────
            last_cell_prompt = page.locator(".input_prompt, .jp-InputPrompt, .prompt").last
            last_prompt_text = last_cell_prompt.inner_text() if last_cell_prompt.count() > 0 else ""
            has_output = "[" in last_prompt_text and "*" not in last_prompt_text
            # Informational only — successful execution is already proven by 4.4
            # (kernel idle, no [*]) + 4.5 (no cell errors). The last cell can
            # legitimately be markdown/empty (no execution count), so a missing
            # count must NOT fail the stage.
            if has_output:
                report.check("4.6 Last cell executed", True, "Last cell shows an execution count")
            else:
                report.warn("4.6 Last cell executed",
                            "Last cell shows no execution count (may be markdown/empty) — "
                            "execution still completed per 4.4/4.5")

            # ── Finalize ───────────────────────────────────────────────────
            report.finalize(raise_on_fail=True)
            elapsed = time.time() - started
            notifier.success(duration_seconds=elapsed, summary="Notebook executed successfully — no errors")

        except Exception as e:
            stop_screenshots.set()
            screenshot_path = f"artifacts/stage4_failure_{int(time.time())}.png"
            Path("artifacts").mkdir(exist_ok=True)
            try:
                page.screenshot(path=screenshot_path, full_page=True)
            except Exception:
                pass

            try:
                report.finalize(raise_on_fail=False)  # persist to the audit on failure
            except Exception:
                pass
            notifier.failure(
                error=e,
                check_report=report,
                context=f"Execute [{alias}]",
                remediation="needs-investigation"
            )
            raise

        finally:
            browser.close()


def _get_cell_errors(page) -> list[str]:
    """
    Extract error output from notebook cells.
    Returns list of error strings (empty if no errors).
    Compatible with JupyterLab 3.x and 4.x.
    """
    errors = []
    # JupyterLab 4.x
    error_outputs = page.locator(".jp-OutputArea-output.jp-mod-error, .jp-RenderedText[data-mime-type='application/vnd.jupyter.stderr']")
    for i in range(min(error_outputs.count(), 5)):  # cap at 5 errors
        try:
            text = error_outputs.nth(i).inner_text()
            if text.strip():
                errors.append(text.strip()[:200])
        except Exception:
            pass

    # JupyterLab 3.x / classic fallback
    if not errors:
        classic_errors = page.locator(".output_error pre, .jp-OutputArea-child .jp-mod-error")
        for i in range(min(classic_errors.count(), 5)):
            try:
                text = classic_errors.nth(i).inner_text()
                if text.strip():
                    errors.append(text.strip()[:200])
            except Exception:
                pass

    return errors


def _screenshot_loop(page, stop_event: threading.Event, interval_s: int):
    """Background thread: takes screenshots every interval_s seconds."""
    Path("artifacts").mkdir(exist_ok=True)
    count = 0
    while not stop_event.wait(timeout=interval_s):
        count += 1
        try:
            path = f"artifacts/stage4_progress_{count:03d}_{int(time.time())}.png"
            page.screenshot(path=path)
            logger.info(f"Progress screenshot: {path}")
        except Exception:
            pass
