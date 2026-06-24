"""
notebooks/auth_helper.py — Shared login + navigation for pipeline stages 2-4.

Each GitHub Actions job runs on a fresh machine, so stages 2-4 must
re-authenticate before doing their real work. This module holds that
repeated flow in one place.

Stage 1 (login.py) intentionally keeps its own inline version — its entire
purpose is to validate each sub-step of the login with individual checks.

The username/password are filled into the page but never logged.
"""

import logging
from urllib.parse import quote

from playwright.sync_api import TimeoutError as PWTimeout

logger = logging.getLogger(__name__)

NAV_TIMEOUT_MS = 30_000
JLAB_TIMEOUT_MS = 45_000
LOGIN_TIMEOUT_MS = 60_000

# Selectors + success signal ported from the proven prod login flow.
SEL_EMAIL = ("input[type='email'], input[name='email'], input[name='username'], "
             "input[placeholder*='email' i], input[placeholder*='user' i]")
SEL_PASSWORD = "input[type='password']"
SEL_SUBMIT = ("button[type='submit'], input[type='submit'], "
              "button.login-btn, button.sign-in")
# The dashboard greets the user with this text once authenticated. It is the
# authoritative login-success signal (matched as a text selector, so no page
# content is ever read into a variable or logged).
LOGIN_SUCCESS_TEXT = "Welcome back"


def login_to_gryps(page, gryps_url: str, username: str, password: str,
                   alias: str = ""):
    """
    Navigate to the Gryps instance and log in. Confirms success by waiting for
    the dashboard greeting ("Welcome back"). Raises Playwright TimeoutError on
    failure. Never logs the URL or page content.
    """
    if alias:
        logger.info(f"[{alias}] Re-authenticating")
    page.goto(gryps_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    page.locator(SEL_EMAIL).first.fill(username)
    page.locator(SEL_PASSWORD).first.fill(password)
    page.locator(SEL_SUBMIT).first.click()
    page.wait_for_selector(f"text={LOGIN_SUCCESS_TEXT}", timeout=LOGIN_TIMEOUT_MS)


# Default Gryps instance (a generic product name, not a client identifier).
DEFAULT_INSTANCE = "Gryps-Analytics"
# JupyterLab / classic-notebook landing selectors (SageMaker), ported from prod.
JLAB_SELECTORS = ("#jp-top-panel, #jp-main-dock-panel, .jp-Launcher, "
                  "#jp-main-content-panel, #notebook_list, #site")


def open_jupyterlab(page, alias: str = "", instance: str = DEFAULT_INSTANCE):
    """
    From the Gryps dashboard, open the embedded JupyterLab/SageMaker notebook UI:
    click the "Notebooks" nav link, then the instance-scoped open-notebook button
    (falling back to the first one), which opens JupyterLab in a NEW TAB. Returns
    the page to use for subsequent work (the new tab if one opened, else the same
    page). Raises Playwright TimeoutError on failure.
    """
    if alias:
        logger.info(f"[{alias}] Opening JupyterLab")
    page.locator("a:has-text('Notebooks')").first.click()
    try:
        # Wait for the open-notebook button (the real requirement) — NOT the instance
        # label, which varies by tenant and was failing ridge/grove at Stage 2.
        page.wait_for_selector("button[data-event-name='open-notebook']",
                               timeout=NAV_TIMEOUT_MS)

        # Prefer the open-notebook button scoped to this instance; fall back to first.
        btn = None
        if instance:
            scoped = page.locator(
                f"xpath=//div[contains(@data-id, '/{instance}')]"
                "//button[@data-event-name='open-notebook']"
            )
            if scoped.count() > 0:
                btn = scoped.first
        if btn is None:
            btn = page.locator("button[data-event-name='open-notebook']").first

        # The button opens JupyterLab in a new tab; capture it if it appears.
        ctx = page.context
        try:
            with ctx.expect_page(timeout=15_000) as new_info:
                btn.click()
            page = new_info.value
        except PWTimeout:
            pass  # opened in the same tab — the click already fired

        page.wait_for_selector(JLAB_SELECTORS, timeout=JLAB_TIMEOUT_MS)
    except PWTimeout:
        _log_jlab_diagnostics(page, instance, alias)
        raise
    return page


def _log_jlab_diagnostics(page, instance, alias=""):
    """Structural-only Stage-2 diagnostics (counts/presence, NO page content)."""
    try:
        diag = {
            "notebooks_links": page.locator("a:has-text('Notebooks')").count(),
            "instance_text_present": (page.locator(f"text={instance}").count() > 0
                                      if instance else "n/a"),
            "open_notebook_buttons": page.locator(
                "button[data-event-name='open-notebook']").count(),
            "has_jp_top_panel": page.locator("#jp-top-panel").count() > 0,
            "has_notebook_list": page.locator("#notebook_list").count() > 0,
            "has_site": page.locator("#site").count() > 0,
        }
        logger.error(f"[{alias}] jupyterlab nav diagnostics (structural only): {diag}")
    except Exception:
        pass


CLASSIC_KERNEL = "conda_gryps"   # SageMaker kernel name (generic, not a client identifier)
_DIALOG_ANCESTOR = "ancestor::*[contains(@class,'jp-Dialog') or contains(@class,'modal')]"


def open_notebook(page, notebook_folder: str, notebook_file: str, alias: str = ""):
    """
    From the JupyterLab/SageMaker file browser, open the target notebook: click into
    the folder (if any), click the notebook file (opens in a NEW TAB), wait for the
    classic notebook UI, and dismiss the kernel-selection dialog. Returns the page the
    notebook is in. Raises Playwright TimeoutError on failure.
    """
    if alias:
        logger.info(f"[{alias}] Opening notebook")
    folder_enc = quote(notebook_folder, safe="") if notebook_folder else ""
    file_enc = quote(notebook_file, safe="")
    file_sel = f"a.item_link[href*='{file_enc}']"
    ctx = page.context
    try:
        # Enter the folder, then wait for the target file link to appear.
        if notebook_folder:
            page.locator(
                f"a.item_link[href*='{folder_enc}'][href*='/tree/']"
            ).first.click()
            page.wait_for_selector(file_sel, timeout=JLAB_TIMEOUT_MS)

        # Clicking a notebook in classic Jupyter opens it in a NEW TAB. expect_page
        # can miss it, so poll context.pages and switch to the newly opened tab.
        pages_before = list(ctx.pages)
        page.locator(file_sel).first.click()
        notebook_page = page
        for _ in range(40):  # up to ~20s for the new tab to appear
            extra = [p for p in ctx.pages if p not in pages_before]
            if extra:
                notebook_page = extra[-1]
                break
            page.wait_for_timeout(500)
        page = notebook_page

        # Wait for the notebook UI (classic #notebook-container, or JupyterLab).
        page.wait_for_selector("#notebook-container, .jp-Notebook, .jp-NotebookPanel",
                               timeout=JLAB_TIMEOUT_MS)
        page.wait_for_selector("#notebook-container .cell, .jp-Notebook .jp-Cell",
                               timeout=JLAB_TIMEOUT_MS)
    except PWTimeout:
        _log_open_diagnostics(page, folder_enc, file_enc, alias)
        raise
    _handle_kernel_dialog(page)
    return page


def _log_open_diagnostics(page, folder_enc, file_enc, alias=""):
    """
    Log STRUCTURAL-ONLY diagnostics (element counts + presence booleans) to locate
    where notebook navigation breaks. Never logs folder/file names or any page
    content — safe for the public Actions log.
    """
    try:
        diag = {
            "item_links_total": page.locator("a.item_link").count(),
            "folder_link_matches": (page.locator(
                f"a.item_link[href*='{folder_enc}'][href*='/tree/']").count()
                if folder_enc else "n/a"),
            "file_link_matches": page.locator(f"a.item_link[href*='{file_enc}']").count(),
            "has_notebook_list": page.locator("#notebook_list").count() > 0,   # classic tree
            "has_jp_dirlisting": page.locator(".jp-DirListing").count() > 0,   # JupyterLab tree
            "has_notebook_container": page.locator("#notebook-container").count() > 0,
            "has_jp_notebook": page.locator(".jp-Notebook").count() > 0,
        }
        logger.error(f"[{alias}] open-notebook nav diagnostics (structural only): {diag}")
    except Exception:
        pass


def _handle_kernel_dialog(page):
    """
    Dismiss the kernel-selection dialog (JupyterLab 'Select' or classic 'Set Kernel'),
    choosing conda_gryps if a dropdown is present. Falls back to 'Continue Without
    Kernel'. No-op if no dialog appears. Never raises.
    """
    btn = page.locator(
        f"xpath=//button[normalize-space(.)='Select' or normalize-space(.)='Set Kernel']"
        f"[{_DIALOG_ANCESTOR}]"
    )
    try:
        btn.first.wait_for(timeout=5_000)
    except PWTimeout:
        cwk = page.locator("button:has-text('Continue Without Kernel')")
        if cwk.count() > 0:
            try:
                cwk.first.click()
                page.wait_for_timeout(1_000)
            except Exception:
                pass
        return
    select = page.locator(f"xpath=//select[{_DIALOG_ANCESTOR}]")
    if select.count() > 0:
        try:
            select.first.select_option(label=CLASSIC_KERNEL)
        except Exception:
            pass  # keep current selection
    try:
        btn.first.click()
        page.wait_for_timeout(2_000)
    except Exception:
        pass
