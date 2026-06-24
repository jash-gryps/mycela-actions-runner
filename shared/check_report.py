"""
shared/check_report.py — Structured pass/fail reporting for every pipeline step.

Every pipeline step creates a CheckReport, adds checks to it,
and calls finalize() at the end. The report is written to JSON
and uploaded to Google Drive.

Usage:
    report = CheckReport(pipeline="notebook:acme-pipeline", stage=2)

    report.require("2.1 JupyterLab loaded", lambda: page.is_visible(".jp-FileBrowser"),
                   "File browser not visible after 30s")

    report.check("2.2 Kernel available",
                 condition=kernel_ready,
                 detail="python3 kernel found" if kernel_ready else "no kernel")

    report.warn("2.3 Slow load", "Page took 45s to load — expected <20s")

    report.finalize()  # raises if any FAIL check exists
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_WARN = "WARN"
STATUS_SKIP = "SKIP"
STATUS_NOTIFY_JASH = "NOTIFY_JASH"


class CheckReport:
    """
    Structured check report for a single pipeline stage.

    Checks are added via:
      - require(name, condition, fail_detail) — FAIL aborts via finalize()
      - check(name, condition, detail) — PASS or FAIL, logged but not aborting
      - warn(name, detail) — always WARN
      - skip(name, reason) — always SKIP
      - notify_immediately(name, detail) — NOTIFY_JASH status; triggers notifier.warning() if set

    Call finalize() at the end of the stage. It:
      - Writes the report to disk as JSON
      - Raises RuntimeError if any FAIL check exists (unless raise_on_fail=False)
    """

    def __init__(self, pipeline: str, stage: int, output_dir: str = "artifacts",
                 notifier=None):
        self.pipeline = pipeline
        self.stage = stage
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.checks: list[dict] = []
        self.started_at = datetime.now(timezone.utc)
        self._result: Optional[str] = None
        self._notifier = notifier

    # ── Public interface ───────────────────────────────────────────────────────

    def require(self, name: str, condition: bool | Callable, fail_detail: str,
                pass_detail: str = "OK") -> bool:
        """
        Add a required check. If it fails, finalize() will raise.
        Use for checks that make continuing the stage impossible.
        """
        return self._add(name, condition, pass_detail, fail_detail, required=True)

    def check(self, name: str, condition: bool | Callable,
              detail: str = "", fail_detail: str = "") -> bool:
        """
        Add a non-required check. Records PASS or FAIL but does not abort.
        Use for checks where failure is notable but the stage can continue.
        """
        pass_detail = detail if condition else ""
        _fail_detail = fail_detail or detail
        return self._add(name, condition, pass_detail, _fail_detail, required=False)

    def warn(self, name: str, detail: str):
        """Add a WARN entry — always recorded as warning, never fails the stage."""
        self.checks.append({"name": name, "status": STATUS_WARN, "detail": detail})
        # Log name + status only — `detail` may contain client data and the Actions
        # log is a PUBLIC sink. Full detail lives in the JSON report (private GDrive).
        logger.warning(f"  ⚠  {name}: WARN")

    def skip(self, name: str, reason: str):
        """Add a SKIP entry — check was not evaluated."""
        self.checks.append({"name": name, "status": STATUS_SKIP, "detail": reason})
        logger.info(f"  –  {name}: SKIP")

    def set_notifier(self, notifier):
        """Attach a Notifier so notify_immediately() can send warnings in real time."""
        self._notifier = notifier

    def notify_immediately(self, name: str, detail: str):
        """
        Record a NOTIFY_JASH check and immediately email jash@gryps.io.
        Does not abort the stage — execution continues after this call.
        """
        self.checks.append({"name": name, "status": STATUS_NOTIFY_JASH, "detail": detail})
        logger.warning(f"  ⚡  {name} [NOTIFY_JASH]")
        if self._notifier:
            try:
                self._notifier.warning(name, detail)
            except Exception as e:
                logger.error(f"[check_report] notify_immediately failed to send: {e}")

    def finalize(self, raise_on_fail: bool = True) -> dict:
        """
        Write report to JSON. Raises RuntimeError if any FAIL check exists.
        Returns the report dict.
        """
        failed = [c for c in self.checks if c["status"] == STATUS_FAIL]
        warned = [c for c in self.checks if c["status"] == STATUS_WARN]
        self._result = STATUS_FAIL if failed else (STATUS_WARN if warned else STATUS_PASS)

        report = {
            "pipeline": self.pipeline,
            "stage": self.stage,
            "result": self._result,
            "started_at": self.started_at.isoformat(),
            "finalized_at": datetime.now(timezone.utc).isoformat(),
            "checks": self.checks,
            "summary": {
                "pass": len([c for c in self.checks if c["status"] == STATUS_PASS]),
                "fail": len(failed),
                "warn": len(warned),
                "skip": len([c for c in self.checks if c["status"] == STATUS_SKIP]),
            }
        }

        # Write to disk
        path = self.output_dir / f"check_report_stage{self.stage}.json"
        path.write_text(json.dumps(report, indent=2))
        logger.info(f"[check_report] Stage {self.stage}: {self._result} — "
                    f"{report['summary']['pass']} pass, {len(failed)} fail, {len(warned)} warn")

        # Write GitHub Actions step summary if available
        self._write_gha_summary(report)

        if raise_on_fail and failed:
            failed_names = ", ".join(c["name"] for c in failed)
            raise RuntimeError(f"Stage {self.stage} failed checks: {failed_names}")

        return report

    @property
    def result(self) -> Optional[str]:
        return self._result

    @property
    def has_failures(self) -> bool:
        return any(c["status"] == STATUS_FAIL for c in self.checks)

    # ── Internal ───────────────────────────────────────────────────────────────

    def _add(self, name: str, condition: bool | Callable, pass_detail: str,
             fail_detail: str, required: bool) -> bool:
        try:
            result = condition() if callable(condition) else bool(condition)
        except Exception as e:
            result = False
            fail_detail = f"Check raised exception: {e}"

        status = STATUS_PASS if result else STATUS_FAIL
        detail = pass_detail if result else fail_detail
        marker = "✓" if result else ("✗" if required else "✗")
        log_fn = logger.info if result else logger.error
        # Log name + status only — `detail` may contain client data (notebook output,
        # error text, table names) and the Actions log is a PUBLIC sink. The full
        # detail survives in the JSON report (private GDrive) and the failure email.
        log_fn(f"  {marker}  {name}: {status}")

        self.checks.append({"name": name, "status": status, "detail": detail})
        return result

    def _write_gha_summary(self, report: dict):
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if not summary_path:
            return
        try:
            # The step summary is a PUBLIC sink — no `detail` column. Check name +
            # status only; the detail lives in the private JSON report and email.
            lines = [
                f"## Stage {self.stage} — {report['result']}",
                f"| Check | Status |",
                f"|---|---|",
            ]
            for c in self.checks:
                icon = {"PASS": "✅", "FAIL": "❌", "WARN": "⚠️", "SKIP": "⏭️"}.get(c["status"], "")
                lines.append(f"| {c['name']} | {icon} {c['status']} |")
            with open(summary_path, "a") as f:
                f.write("\n".join(lines) + "\n\n")
        except Exception:
            pass

