"""Tests for the cron-job.org sync + migrate logic:
- cron expression parsing (both scripts),
- create vs update payload shape,
- sync idempotency (create / update / delete decisions),
- migrate URL/slug matching helpers.
"""

import textwrap

import pytest

from notebooks import setup_cronjobs
from scripts import migrate_cronjobs


# ── cron parsing ──────────────────────────────────────────────────────────────

def test_parse_cron_hourly():
    # "30 * * * *" → minute 30, every hour.
    assert setup_cronjobs.parse_cron("30 * * * *") == {
        "minutes": [30], "hours": -1, "mdays": -1, "months": -1, "wdays": -1,
    }


def test_parse_cron_daily_time():
    assert setup_cronjobs.parse_cron("0 10 * * *") == {
        "minutes": [0], "hours": [10], "mdays": -1, "months": -1, "wdays": -1,
    }


def test_parse_cron_weekday_range_and_hour_list():
    # "15 10,13,16,19 * * 1-5"
    result = setup_cronjobs.parse_cron("15 10,13,16,19 * * 1-5")
    assert result["minutes"] == [15]
    assert result["hours"] == [10, 13, 16, 19]
    assert result["wdays"] == [1, 2, 3, 4, 5]


def test_parse_cron_step():
    assert setup_cronjobs.parse_cron("*/15 * * * *")["minutes"] == [0, 15, 30, 45]


def test_parse_cron_rejects_bad_field_count():
    with pytest.raises(ValueError):
        setup_cronjobs.parse_cron("0 10 * *")


def test_migrate_parse_cron_matches_setup():
    # The two scripts must agree on schedule semantics.
    assert migrate_cronjobs._parse_cron("0 22 * * 0")["wdays"] == [0]


# ── payload shape ───────────────────────────────────────────────────────────

def test_create_payload_is_full():
    nb = {"id": "harbor-cip", "schedule": "30 * * * *",
          "trigger_url": "https://gryps-automation.vercel.app/api/cron/trigger?id=harbor-cip&token=t",
          "max_execution_minutes": 45}
    p = setup_cronjobs._build_create_payload(nb)["job"]
    assert p["title"] == "mycela:harbor-cip"
    assert p["url"].endswith("id=harbor-cip&token=t")
    assert p["requestTimeout"] == 45 * 60
    assert "extendedData" in p
    assert p["schedule"]["timezone"] == "America/New_York"


def test_update_payload_is_minimal():
    # PATCH must omit schedule/requestTimeout/extendedData — cron-job.org 500s
    # on them. Only url/title/enabled are allowed on PATCH.
    nb = {"id": "harbor-cip", "schedule": "30 * * * *", "trigger_url": "https://x/y"}
    p = setup_cronjobs._build_update_payload(nb)["job"]
    assert set(p.keys()) == {"url", "title", "enabled"}
    assert "schedule" not in p


def test_paused_notebook_disabled():
    nb = {"id": "x", "schedule": "0 0 * * *", "paused": True}
    assert setup_cronjobs._build_create_payload(nb)["job"]["enabled"] is False


# ── sync idempotency ──────────────────────────────────────────────────────────

class FakeClient:
    """Records the create/update/delete calls sync_notebooks makes."""
    instance = None

    def __init__(self, api_key):
        self.created, self.updated, self.deleted = [], [], []
        FakeClient.instance = self

    def list_jobs(self):
        return [
            {"title": "mycela:harbor-cip", "jobId": 1},   # in config → update
            {"title": "mycela:gone-nb", "jobId": 2},      # not in config → delete
            {"title": "legacy-raw-job", "jobId": 3},      # not ours → leave alone
        ]

    def create_job(self, payload):
        self.created.append(payload["job"]["title"])

    def update_job(self, job_id, payload):
        self.updated.append((job_id, payload["job"]["title"]))

    def delete_job(self, job_id):
        self.deleted.append(job_id)


@pytest.fixture
def config_file(tmp_path):
    p = tmp_path / "notebooks.yml"
    p.write_text(textwrap.dedent("""
        notebooks:
          - id: harbor-cip
            schedule: "30 * * * *"
            trigger_url: "https://gryps-automation.vercel.app/api/cron/trigger?id=harbor-cip&token=t"
          - id: ridge-new
            schedule: "0 10 * * *"
            trigger_url: "https://gryps-automation.vercel.app/api/cron/trigger?id=ridge-new&token=t"
    """))
    return str(p)


def test_sync_creates_updates_and_deletes(monkeypatch, config_file):
    monkeypatch.setenv("CRONJOB_API_KEY", "fake-key")
    monkeypatch.setattr(setup_cronjobs, "CronJobClient", FakeClient)
    monkeypatch.setattr(setup_cronjobs.time, "sleep", lambda s: None)

    setup_cronjobs.sync_notebooks(config_file, dry_run=False)
    fake = FakeClient.instance

    assert fake.updated == [(1, "mycela:harbor-cip")]      # existing → update
    assert fake.created == ["mycela:ridge-new"]            # new → create
    assert fake.deleted == [2]                             # mycela: orphan → delete
    # jobId 3 (legacy-raw-job) is NOT deleted — only mycela:-prefixed orphans.
    assert 3 not in fake.deleted


def test_sync_dry_run_makes_no_writes(monkeypatch, config_file):
    monkeypatch.setenv("CRONJOB_API_KEY", "fake-key")
    monkeypatch.setattr(setup_cronjobs, "CronJobClient", FakeClient)
    monkeypatch.setattr(setup_cronjobs.time, "sleep", lambda s: None)

    setup_cronjobs.sync_notebooks(config_file, dry_run=True)
    fake = FakeClient.instance
    assert fake.created == [] and fake.updated == [] and fake.deleted == []


# ── migrate matching helpers ──────────────────────────────────────────────────

def test_extract_notebook_id_from_url():
    url = "https://gryps-automation.vercel.app/api/cron/trigger?id=massport-cip&token=abc"
    assert migrate_cronjobs._extract_notebook_id_from_url(url) == "massport-cip"


def test_extract_notebook_id_absent():
    assert migrate_cronjobs._extract_notebook_id_from_url("https://x/y?foo=1") is None


def test_notebook_file_to_slug():
    assert migrate_cronjobs._notebook_file_to_slug("CapitalPlanParser.ipynb") == "capitalplanparser-ipynb"
    assert migrate_cronjobs._notebook_file_to_slug("CronJob_cip_fy26.ipynb") == "cronjob-cip-fy26-ipynb"
