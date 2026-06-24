"""
shared/notify.py — Mycela Notification Module

Every pipeline must use this module for all notifications.
Failure emails go to jash@gryps.io with:
  1. What failed
  2. Why it failed (LLM root cause analysis)
  3. What Claude is doing about it
  4. Check report summary
  5. Raw error (truncated)

Usage:
    from shared.notify import Notifier

    notifier = Notifier(
        pipeline="notebook:acme-pipeline",
        stage=2,
        tenant_url="https://example.gryps.io",
        github_run_url=os.environ.get("GITHUB_RUN_URL", ""),
        recipient="jash@gryps.io"
    )

    notifier.success(duration_seconds=120, summary="4/4 checks passed")

    notifier.failure(
        error=e,
        check_report=report,         # CheckReport object or None
        context="Opening JupyterLab",
        remediation="auto-retry"     # "auto-retry" | "needs-investigation" | "fix-pending:{branch}"
    )
"""

import os
import smtplib
import traceback
import logging
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

REQUIRED_ENV = ["GMAIL_APP_PASSWORD", "FROM_EMAIL"]
NOTIFY_EMAIL = "jash@gryps.io"
MAX_ERROR_LEN = 1000
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465


# ── Notifier ──────────────────────────────────────────────────────────────────

class Notifier:
    """
    Handles all pipeline notifications. One Notifier per pipeline run.
    Thread-safe — each call to success() or failure() is independent.
    """

    def __init__(self, pipeline: str, stage: int, tenant_url: str,
                 github_run_url: str = "", recipient: str = NOTIFY_EMAIL):
        self.pipeline = pipeline
        self.stage = stage
        self.tenant_url = tenant_url
        self.github_run_url = github_run_url
        self.recipient = recipient
        self.timestamp = datetime.now(timezone.utc)
        self._validate_env()

    def _validate_env(self):
        missing = [v for v in REQUIRED_ENV if not os.environ.get(v)]
        if missing:
            # Log but don't crash — a missing env var should not prevent pipeline from running
            logger.error(f"[notify] Missing environment variables: {missing}. "
                         f"Notifications will fail. Check .env.example.")

    # ── Public interface ───────────────────────────────────────────────────────

    def success(self, duration_seconds: float, summary: str = "",
                stage_breakdown: list[str] | None = None,
                gdrive_link: str = ""):
        """
        Send a brief success notification.

        Args:
            duration_seconds: Total run duration
            summary: One-line result summary
            stage_breakdown: Optional per-stage lines, e.g. ["✓ Login 12s", "✓ JupyterLab 8s"]
            gdrive_link: Optional Drive folder URL for the run archive
        """
        subject = f"[MYCELA ✓] {self.pipeline} — Stage {self.stage} completed"
        lines = [
            f"✓ {self.pipeline} · Stage {self.stage} · {self.tenant_url}",
            f"Completed in {duration_seconds:.0f}s" + (f" — {summary}" if summary else ""),
        ]
        if stage_breakdown:
            lines.append("")
            lines.extend(stage_breakdown)
        if gdrive_link:
            lines.append("")
            lines.append(f"Archive: {gdrive_link}")
        if self.github_run_url:
            lines.append(self.github_run_url)
        self._send(subject, "\n".join(lines), is_html=False)

    def failure(self, error: Exception, check_report=None,
                context: str = "", remediation: str = "needs-investigation"):
        """
        Send a full failure notification with LLM root cause analysis.

        Args:
            error: The exception that caused the failure
            check_report: CheckReport object (optional)
            context: What was happening when the failure occurred
            remediation: "auto-retry" | "needs-investigation" | "fix-pending:{branch}"
        """
        subject = (
            f"[MYCELA FAILURE] {self.pipeline} — Stage {self.stage}"
            + (f" — {context}" if context else "")
        )

        error_text = _truncate(traceback.format_exc(), MAX_ERROR_LEN)
        root_cause = _explain_failure(error, error_text, check_report)
        remediation_text = _remediation_message(remediation)
        check_table = _format_check_report(check_report)
        first_fail = _first_failed_check(check_report)
        failure_streak = _consecutive_failures(self.pipeline)

        body = _build_failure_html(
            pipeline=self.pipeline,
            stage=self.stage,
            tenant_url=self.tenant_url,
            github_run_url=self.github_run_url,
            timestamp=self.timestamp,
            context=context,
            root_cause=root_cause,
            remediation_text=remediation_text,
            check_table=check_table,
            error_text=error_text,
            first_fail=first_fail,
            failure_streak=failure_streak,
        )

        # Every failure email must reach jash@gryps.io (CC'd even if the To is a
        # different pipeline owner). Golden Rule 5.
        self._send(subject, body, is_html=True, cc=[NOTIFY_EMAIL])

    def warning(self, check_name: str, detail: str):
        """Send an abbreviated warning for a WARN-level check result."""
        subject = f"[MYCELA ⚠] {self.pipeline} — Stage {self.stage} — Warning: {check_name}"
        body = (
            f"Warning in {self.pipeline} · Stage {self.stage} · {self.tenant_url}\n\n"
            f"Check: {check_name}\nDetail: {detail}\n\n"
            f"The pipeline completed but this condition should be investigated.\n"
            f"{self.github_run_url}"
        )
        self._send(subject, body, is_html=False)

    # ── Internal ───────────────────────────────────────────────────────────────

    def _send(self, subject: str, body: str, is_html: bool = False,
              cc: list[str] | None = None):
        """Send email via Gmail SMTP SSL. Logs error on failure — does not raise."""
        try:
            app_password = os.environ.get("GMAIL_APP_PASSWORD", "")
            from_email = os.environ.get("FROM_EMAIL", "")

            if not app_password or not from_email:
                logger.error("[notify] Cannot send — GMAIL_APP_PASSWORD or FROM_EMAIL not set")
                return

            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = from_email
            msg["To"] = self.recipient

            recipients = [self.recipient]
            # Add CC addresses not already on the recipient list (dedup).
            extra_cc = [a for a in (cc or []) if a and a not in recipients]
            if extra_cc:
                msg["Cc"] = ", ".join(extra_cc)
                recipients.extend(extra_cc)

            content_type = "html" if is_html else "plain"
            msg.attach(MIMEText(body, content_type))

            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
                server.login(from_email, app_password)
                server.sendmail(from_email, recipients, msg.as_string())

            logger.info(f"[notify] Sent '{subject}' to {self.recipient}")

        except Exception as e:
            # Notification failure must never mask the original pipeline error
            logger.error(f"[notify] Failed to send notification: {e}")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _explain_failure(error: Exception, error_text: str, check_report) -> str:
    """
    Generate plain-English root cause explanation.
    Tries Claude Haiku → Groq Llama → rule-based template.
    """
    context_for_llm = f"Error: {type(error).__name__}: {error}\n\nTraceback:\n{error_text}"
    if check_report:
        failed = [c for c in getattr(check_report, "checks", []) if c.get("status") == "FAIL"]
        if failed:
            context_for_llm += f"\n\nFailed checks: {failed}"

    prompt = (
        "You are analyzing a pipeline failure for an internal automation system. "
        "In 2-5 sentences, explain: (1) what likely caused this failure, "
        "(2) whether it appears transient or persistent, "
        "(3) what should be investigated first. "
        "Be specific. Do not restate the error — explain the root cause. "
        "Use 'likely' or 'possibly' if you are not certain of the cause.\n\n"
        f"{context_for_llm}"
    )

    # Try Claude Haiku
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text.strip()
    except Exception:
        pass

    # Try Groq Llama
    try:
        from groq import Groq
        client = Groq(api_key=os.environ.get("GROQ_API_KEY", ""))
        response = client.chat.completions.create(
            model="llama-3.1-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300
        )
        return response.choices[0].message.content.strip()
    except Exception:
        pass

    # Rule-based fallback
    error_type = type(error).__name__
    if "timeout" in str(error).lower():
        return (f"The operation timed out ({error_type}). This is likely a transient issue "
                f"caused by slow network, a slow-loading page, or a temporarily unavailable service. "
                f"The next scheduled run will retry automatically.")
    if "auth" in str(error).lower() or "401" in str(error) or "403" in str(error):
        return (f"Authentication failed ({error_type}). The credentials may have expired or been "
                f"revoked. This requires immediate investigation — the pipeline will not succeed "
                f"on retry until credentials are verified.")
    return (f"An unexpected error occurred ({error_type}: {error}). "
            f"The root cause could not be automatically determined. "
            f"Review the raw error and check report below for more detail.")


def _first_failed_check(check_report) -> dict | None:
    """Return {'name', 'detail'} of the first FAIL check, or None."""
    if not check_report:
        return None
    for c in getattr(check_report, "checks", []):
        if c.get("status") == "FAIL":
            return {"name": c.get("name", ""), "detail": c.get("detail", "")}
    return None


def _consecutive_failures(pipeline: str) -> int:
    """
    Count consecutive recent failures for this pipeline's notebook from the DB.
    Returns 0 if the DB is unavailable or DATABASE_URL is unset — the email
    is sent either way; this is enrichment only.
    """
    if not os.environ.get("DATABASE_URL", ""):
        return 0
    try:
        from shared.db import get_db
        # pipeline format: "notebook:{alias}:{notebook_id}" — last segment is the id
        notebook_id = pipeline.split(":")[-1]
        rows = get_db().fetch_all(
            "SELECT status FROM notebook_runs WHERE notebook_id = %s "
            "ORDER BY started_at DESC LIMIT 5",
            (notebook_id,)
        )
        streak = 0
        for row in rows:
            if row.get("status") == "failure":
                streak += 1
            else:
                break
        return streak
    except Exception as e:
        logger.warning(f"[notify] Could not check failure history (continuing): {e}")
        return 0


def _remediation_message(remediation: str) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if remediation == "auto-retry":
        return ("This failure appears transient. The pipeline will automatically retry "
                "at the next scheduled run. No action required unless this failure repeats.")
    if remediation == "needs-investigation":
        return ("This failure requires investigation before the next run can succeed. "
                "Claude cannot auto-fix this without your input. "
                f"Please review the error below and reply with instructions. ({now})")
    if remediation.startswith("fix-pending:"):
        branch = remediation.split(":", 1)[1]
        return (f"Claude has identified a fix and applied it to branch '{branch}'. "
                f"It is awaiting your approval before deployment. "
                f"Reply 'approve fix' to deploy, or review the branch first. ({now})")
    return remediation


def _format_check_report(check_report) -> str:
    if not check_report:
        return "<p><em>No check report available.</em></p>"
    checks = getattr(check_report, "checks", [])
    if not checks:
        return "<p><em>Check report is empty.</em></p>"

    status_color = {"PASS": "#1B5E20", "FAIL": "#B71C1C", "WARN": "#E65100", "SKIP": "#757575"}
    rows = ""
    for c in checks:
        color = status_color.get(c.get("status", ""), "#000")
        rows += (
            f"<tr>"
            f"<td style='padding:6px 12px;border-bottom:1px solid #eee'>{c.get('name','')}</td>"
            f"<td style='padding:6px 12px;border-bottom:1px solid #eee;color:{color};font-weight:bold'>"
            f"{c.get('status','')}</td>"
            f"<td style='padding:6px 12px;border-bottom:1px solid #eee;color:#555'>"
            f"{c.get('detail','')}</td>"
            f"</tr>"
        )
    return (
        "<table style='border-collapse:collapse;width:100%;font-family:Arial,sans-serif;font-size:14px'>"
        "<tr style='background:#1F2D3D;color:white'>"
        "<th style='padding:8px 12px;text-align:left'>Check</th>"
        "<th style='padding:8px 12px;text-align:left'>Status</th>"
        "<th style='padding:8px 12px;text-align:left'>Detail</th>"
        "</tr>"
        f"{rows}</table>"
    )


def _build_failure_html(pipeline, stage, tenant_url, github_run_url, timestamp,
                         context, root_cause, remediation_text, check_table, error_text,
                         first_fail: dict | None = None, failure_streak: int = 0) -> str:
    first_fail_row = ""
    if first_fail:
        first_fail_row = (
            f'<tr><td style="padding:4px 0;color:#555">Failed check</td>'
            f'<td style="padding:4px 0;color:#B71C1C;font-weight:bold">'
            f'{first_fail["name"]} — {first_fail["detail"]}</td></tr>'
        )
    streak_banner = ""
    if failure_streak >= 2:
        # streak counts previous runs; this failure makes it streak + 1
        streak_banner = (
            f'<p style="background:#B71C1C;color:white;padding:10px 14px;border-radius:3px;'
            f'font-weight:bold;margin:0 0 16px">⚠ {failure_streak + 1} consecutive failures '
            f'for this notebook — this is not a one-off.</p>'
        )
    elif failure_streak == 0:
        streak_banner = (
            '<p style="color:#555;font-size:13px;margin:0 0 16px">'
            'First failure after recent successes (or no run history available).</p>'
        )
    return f"""
<html><body style="font-family:Arial,sans-serif;max-width:700px;margin:0 auto;color:#222">
  <div style="background:#B71C1C;padding:20px 24px;border-radius:4px 4px 0 0">
    <h2 style="margin:0;color:white;font-size:20px">⚠ Mycela Pipeline Failure</h2>
    <p style="margin:8px 0 0;color:#ffcdd2;font-size:14px">{timestamp.strftime('%Y-%m-%d %H:%M UTC')}</p>
  </div>

  <div style="border:1px solid #ddd;border-top:none;padding:20px 24px">

    {streak_banner}

    <table style="width:100%;border-collapse:collapse;margin-bottom:20px;font-size:14px">
      <tr><td style="padding:4px 0;color:#555;width:140px">Pipeline</td>
          <td style="padding:4px 0;font-weight:bold">{pipeline}</td></tr>
      <tr><td style="padding:4px 0;color:#555">Stage</td>
          <td style="padding:4px 0">{stage}{f" — {context}" if context else ""}</td></tr>
      <tr><td style="padding:4px 0;color:#555">Tenant</td>
          <td style="padding:4px 0">{tenant_url}</td></tr>
      <tr><td style="padding:4px 0;color:#555">GitHub run</td>
          <td style="padding:4px 0"><a href="{github_run_url}">{github_run_url or "N/A"}</a></td></tr>
      {first_fail_row}
    </table>

    <h3 style="color:#1F2D3D;border-bottom:2px solid #eee;padding-bottom:8px">Why It Failed</h3>
    <p style="line-height:1.6;background:#FFF3E0;padding:14px;border-left:4px solid #E65100;
              border-radius:2px">{root_cause}</p>

    <h3 style="color:#1F2D3D;border-bottom:2px solid #eee;padding-bottom:8px">What's Being Done</h3>
    <p style="line-height:1.6">{remediation_text}</p>

    <h3 style="color:#1F2D3D;border-bottom:2px solid #eee;padding-bottom:8px">Check Report</h3>
    {check_table}

    <h3 style="color:#1F2D3D;border-bottom:2px solid #eee;padding-bottom:8px;margin-top:20px">
      Raw Error</h3>
    <pre style="background:#1E1E1E;color:#D4D4D4;padding:14px;border-radius:4px;
                font-size:12px;overflow-x:auto;white-space:pre-wrap">{error_text}</pre>

  </div>
  <div style="background:#f5f5f5;padding:12px 24px;font-size:12px;color:#888;
              border:1px solid #ddd;border-top:none;border-radius:0 0 4px 4px">
    Mycela Automation · Reply to jash@gryps.io with any instructions
  </div>
</body></html>
"""


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + f"\n... (truncated, {len(text) - max_len} chars omitted)"
