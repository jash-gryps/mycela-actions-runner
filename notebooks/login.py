"""
notebooks/login.py — Stage 1: Login to Gryps instance.

Uses TENANT_ALIAS (codename) in all logs and notifications.
The real URL (GRYPS_URL) is never logged — it arrives pre-masked from the workflow.
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

logger = logging.getLogger(__name__)

TIMEOUT_MS = 30_000
LOGIN_TIMEOUT_MS = 60_000

# Login selectors + success/error signals — ported from the proven prod flow.
SEL_EMAIL = ("input[type='email'], input[name='email'], input[name='username'], "
             "input[placeholder*='email' i], input[placeholder*='user' i]")
SEL_PASSWORD = "input[type='password']"
SEL_SUBMIT = ("button[type='submit'], input[type='submit'], "
              "button.login-btn, button.sign-in")
LOGIN_SUCCESS_TEXT = "Welcome back"   # dashboard greeting = authenticated
# Auth-error words the login page shows on bad credentials. Matched as text
# selectors only — page content is never read into a variable or logged.
AUTH_ERROR_HINTS = ["invalid", "incorrect", "unauthorized", "no account", "try again"]


def run_stage():
    notebook_id = os.environ["NOTEBOOK_ID"]
    alias = os.environ["TENANT_ALIAS"]           # codename — safe to use everywhere
    gryps_url = os.environ["GRYPS_URL"].rstrip("/")   # real URL — never log this
    username = os.environ["GRYPS_USERNAME"]
    password = os.environ["GRYPS_PASSWORD"]
    github_run_url = os.environ.get("GITHUB_RUN_URL", "")

    # Use alias in pipeline identifier — appears in emails and check reports
    pipeline_id = f"notebook:{alias}:{notebook_id}"

    report = CheckReport(pipeline=pipeline_id, stage=1)
    notifier = Notifier(
        pipeline=pipeline_id,
        stage=1,
        tenant_url=alias,        # alias in email, not the real URL
        github_run_url=github_run_url
    )

    started = time.time()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()

        try:
            # ── Check 1.1: Page loads ──────────────────────────────────────
            logger.info(f"[{alias}] Connecting to Gryps instance")
            try:
                page.goto(gryps_url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
                title = page.title()
                report.require(
                    "1.1 Page loads",
                    condition="Gryps" in title or len(title) > 0,
                    fail_detail=f"Unexpected page title: '{title}'",
                    pass_detail=f"Loaded OK"
                )
            except PWTimeout:
                report.require("1.1 Page loads", False,
                               fail_detail=f"Timed out after {TIMEOUT_MS}ms")
                raise

            # ── Check 1.2: Login form ──────────────────────────────────────
            email_field = page.locator(SEL_EMAIL).first
            password_field = page.locator(SEL_PASSWORD).first
            report.require(
                "1.2 Login form visible",
                condition=email_field.is_visible() and password_field.is_visible(),
                fail_detail="Email or password field not found"
            )

            # ── Login ──────────────────────────────────────────────────────
            logger.info(f"[{alias}] Logging in")
            email_field.fill(username)
            password_field.fill(password)
            page.locator(SEL_SUBMIT).first.click()

            # ── Check 1.3: Login succeeds ──────────────────────────────────
            # Success = the dashboard greeting appears. Matched as a text selector
            # so no page content is read into a variable or logged.
            try:
                page.wait_for_selector(f"text={LOGIN_SUCCESS_TEXT}", timeout=LOGIN_TIMEOUT_MS)
                report.require("1.3 Login succeeds", True, "",
                               pass_detail="Dashboard greeting visible")
            except PWTimeout:
                report.require("1.3 Login succeeds", False,
                               fail_detail="Dashboard greeting did not appear — check credentials")
                raise

            # ── Check 1.4: No auth error ───────────────────────────────────
            # Detect known error words via case-insensitive text selectors; do not
            # read or log the page body (it may contain client data).
            error_hit = any(
                page.locator(f"text=/{hint}/i").count() > 0 for hint in AUTH_ERROR_HINTS
            )
            if error_hit:
                report.warn("1.4 No error message", "An auth-error message is visible after login")
            else:
                report.check("1.4 No error message", True, "No errors")

            report.finalize(raise_on_fail=True)
            elapsed = time.time() - started
            notifier.success(duration_seconds=elapsed, summary="3/3 required checks passed")

        except Exception as e:
            Path("artifacts").mkdir(exist_ok=True)
            try:
                page.screenshot(path=f"artifacts/stage1_failure_{int(time.time())}.png")
            except Exception:
                pass
            try:
                report.finalize(raise_on_fail=False)  # persist to the audit on failure
            except Exception:
                pass
            notifier.failure(
                error=e,
                check_report=report,
                context=f"Login [{alias}]",
                remediation="auto-retry" if "timeout" in str(e).lower() else "needs-investigation"
            )
            raise

        finally:
            browser.close()
