"""A failed job-history fetch shows as degraded, never as an empty, healthy range.

The defect (review C10): in NWUI mode the completed jobs come from a second
fetch of the NetWorker jobs database. When it failed (the 64 MB response cap,
a timeout, or a saved password that could not be decrypted, which skipped the
fetch without a word) the source was marked diagnosticOnly, so the dashboard
and the daily email showed zero completed jobs, a 0% SLA and health OK.

Now the collectors record whether the history loaded (summary.historyAvailable),
the failure is a visible warning source, health is at best "warning", the SLA
is N/A, and the email carries a "Data incomplete" banner.

The collectors are exercised at their data boundary: the NetWorker calls are
replaced by recorded-shape payloads (scrubbed placeholders only).
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from nwdash import nwui, reports, restapi
from nwdash.models import ApiConfig, RestApiError, job_history_notice
from nwdash.wmi_health import unavailable_server_health

ASSETS = Path(__file__).resolve().parent.parent / "nwdash" / "assets"
NODE = shutil.which("node")

TOO_LARGE = RestApiError(502, "REST API response exceeded dashboard safety limit.")

# One running action, the shape /nwui/api/monitoringactions returns.
LIVE_ACTION = {
    "actionName": "backup",
    "workflowName": "client-a.example.com",
    "policyName": "Gold",
    "status": "Running",
    "startTime": "2026-11-03T06:00:00+00:00",
    "jobData": {"runningInputCount": 1},
}

# A finished job as cached_nwui_job_history hands it over (rest_job_as_nwui_action shape).
HISTORY_ACTION = {
    "actionName": "backup",
    "workflowName": "client-b.example.com",
    "policyName": "Gold",
    "status": "Succeeded",
    "startTime": "2026-11-02T22:00:00+00:00",
    "duration": 60000,
    "jobData": {"successfulInputCount": 1},
}


def _config(api_mode="nwui", password="secret"):
    return ApiConfig(
        rest_api_host="nw.example.com", rest_api_port=9090,
        backup_server_host="nw.example.com", backup_server_port=9001,
        username="administrator", password=password, api_mode=api_mode, api_version="v3",
        report_range="24h", custom_start_date="", custom_end_date="",
        use_wmi_health=False, wmi_username="", wmi_password="",
        timeout_seconds=30, verify_tls=True, use_authc_header=False,
    )


def _build_nwui(config, history):
    """build_dashboard_nwui with the NetWorker calls replaced; `history` is a
    list of items or the exception cached_nwui_job_history raises."""
    def monitoring(_config, _opener, _headers, endpoint_name, **_kw):
        return [dict(LIVE_ACTION)] if endpoint_name == "monitoringactions" else []

    def fetch_history(_config, _context):
        if isinstance(history, Exception):
            raise history
        return [dict(item) for item in history], "https://nw.example.com:9090/nwrestapi/v3/global/jobs", False

    with mock.patch.object(nwui, "nwui_login", return_value=({"Authorization": "Bearer t"}, {"status": "ok"})), \
            mock.patch.object(nwui, "nwui_monitoring_all_pages", side_effect=monitoring), \
            mock.patch.object(nwui, "cached_nwui_job_history", side_effect=fetch_history), \
            mock.patch.object(nwui, "load_server_health_nwui", return_value=unavailable_server_health()):
        return nwui.build_dashboard_nwui(config, create_session=False)


class TestNwuiHistoryFailureIsDegraded(unittest.TestCase):
    def test_a_capped_history_fetch_is_not_healthy(self):
        status, body = _build_nwui(_config(), TOO_LARGE)
        self.assertEqual(status, 200, "live activity still loads")
        summary = body["summary"]
        self.assertNotEqual(summary["health"], "ok")
        self.assertIs(summary["historyAvailable"], False)
        self.assertIs(summary["slaAvailable"], False)
        self.assertEqual(summary["activeJobs"], 1, "the live job is still shown")

    def test_the_failure_is_a_visible_warning_source(self):
        _, body = _build_nwui(_config(), TOO_LARGE)
        source = body["sources"]["monitoringActionsHistory"]
        self.assertFalse(source["ok"])
        self.assertFalse(source.get("diagnosticOnly"))
        self.assertNotEqual(source.get("severity"), "info")
        self.assertIsNot(source.get("displayWarning"), False)
        self.assertIn("incomplete", source["userMessage"])

    def test_an_undecryptable_password_is_recorded_not_skipped(self):
        # The real cached_nwui_job_history: with no password the REST fallback refuses.
        with mock.patch.object(nwui, "nwui_login", return_value=({"Authorization": "Bearer t"}, {"status": "ok"})), \
                mock.patch.object(nwui, "nwui_monitoring_all_pages", return_value=[]), \
                mock.patch.object(nwui, "load_server_health_nwui", return_value=unavailable_server_health()):
            nwui._JOBS_HISTORY_CACHE.clear()
            _, body = nwui.build_dashboard_nwui(_config(password=""), auth_headers={"Authorization": "Bearer t"}, create_session=False)
        self.assertIn("monitoringActionsHistory", body["sources"])
        self.assertFalse(body["sources"]["monitoringActionsHistory"]["ok"])
        self.assertNotEqual(body["summary"]["health"], "ok")
        self.assertIs(body["summary"]["historyAvailable"], False)

    def test_a_loaded_history_stays_healthy(self):
        _, body = _build_nwui(_config(), [HISTORY_ACTION])
        summary = body["summary"]
        self.assertEqual(summary["health"], "ok")
        self.assertIs(summary["historyAvailable"], True)
        self.assertIs(summary["slaAvailable"], True)
        self.assertEqual(summary["successfulJobs"], 1)
        self.assertEqual(job_history_notice(body), "")


class TestRestJobsFailureIsDegraded(unittest.TestCase):
    def _build(self, fail_jobs):
        def fetch(url, headers, timeout, context, label, max_bytes=0):
            if label == "jobs" and fail_jobs:
                raise TOO_LARGE
            return {
                "clients": {"clients": [{"hostname": "client-a.example.com"}]},
                "jobs": {"jobs": []},
                # A failed-only query that still answers: the SLA must not become 0%.
                "failedJobs": {"jobs": []},
                "alerts": {"alerts": []},
                "policies": {"protectionPolicies": []},
            }[label]

        with mock.patch.object(restapi, "fetch_json", side_effect=fetch), \
                mock.patch.object(restapi, "load_server_health_rest", return_value=unavailable_server_health()):
            return restapi.build_dashboard_rest(_config(api_mode="rest"))

    def test_a_failed_jobs_fetch_flags_the_summary(self):
        _, body = self._build(fail_jobs=True)
        summary = body["summary"]
        self.assertNotEqual(summary["health"], "ok")
        self.assertIs(summary["historyAvailable"], False)
        self.assertIs(summary["slaAvailable"], False)
        self.assertIn("incomplete", body["sources"]["jobs"]["userMessage"])

    def test_an_empty_range_is_not_a_history_failure(self):
        _, body = self._build(fail_jobs=False)
        self.assertIs(body["summary"]["historyAvailable"], True)
        self.assertIs(body["summary"]["slaAvailable"], False, "no job to measure")


class TestDailyEmailSaysTheDataIsIncomplete(unittest.TestCase):
    def setUp(self):
        _, self.failed = _build_nwui(_config(), TOO_LARGE)
        _, self.loaded = _build_nwui(_config(), [HISTORY_ACTION])

    def test_the_email_carries_the_banner_and_no_sla(self):
        plain, html_body = reports.dashboard_report_email(self.failed)
        self.assertIn("Data incomplete", html_body)
        self.assertIn("Completed job history could not be loaded", html_body)
        self.assertIn("Job history: Completed job history could not be loaded", plain)
        self.assertIn("Backup SLA: N/A", plain)
        self.assertNotIn("Connection established", html_body, "the healthy label")

    def test_the_snapshot_image_carries_the_banner(self):
        snapshot = reports.dashboard_snapshot_html(self.failed)
        self.assertIn("Data incomplete", snapshot)
        self.assertIn(">N/A<", snapshot)

    def test_a_complete_report_has_no_banner(self):
        plain, html_body = reports.dashboard_report_email(self.loaded)
        self.assertNotIn("Data incomplete", html_body)
        self.assertNotIn("Job history:", plain)
        self.assertIn("Backup SLA: 100.0% (1 met / 1 total)", plain)

    def test_a_report_with_no_jobs_shows_the_sla_as_na(self):
        summary = {"slaTotalJobs": 0, "slaPercent": 0}
        self.assertEqual(reports.report_sla_text(summary), "N/A")


@unittest.skipIf(NODE is None, "node is not on PATH; skipping app.js logic tests")
class TestDashboardSlaPieSaysUnknown(unittest.TestCase):
    def _render(self, summary):
        source = (ASSETS / "app.js").read_text(encoding="utf-8")
        start = source.index("    function renderSlaPie(summary) {")
        end = source.index("\n    }\n", start) + len("\n    }\n")
        script = (
            "const numberValue = (v) => Number(v) || 0;\n"
            "const formatNumber = String; const percentage = () => 0; const conicGradient = () => '';\n"
            "const escapeHtml = String;\n"
            "const mgmtSlaPie = {className: '', textContent: '', innerHTML: ''};\n"
            "const mgmtSlaMeta = {textContent: ''};\n"
            + source[start:end]
            + f"renderSlaPie({json.dumps(summary)});\n"
            "process.stdout.write(JSON.stringify({text: mgmtSlaPie.textContent, html: mgmtSlaPie.innerHTML, meta: mgmtSlaMeta.textContent}));\n"
        )
        proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=30, check=False)
        if proc.returncode != 0:
            raise AssertionError(proc.stderr)
        return json.loads(proc.stdout)

    def test_no_history_is_not_no_jobs(self):
        out = self._render({"slaTotalJobs": 0, "slaMetJobs": 0, "activeJobs": 0, "historyAvailable": False})
        self.assertIn("Job history unavailable", out["text"])
        self.assertNotIn("No backup jobs ran", out["text"])

    def test_no_history_hides_a_partial_percentage(self):
        out = self._render({"slaTotalJobs": 3, "slaMetJobs": 0, "slaMissedJobs": 3, "slaPercent": 0, "historyAvailable": False})
        self.assertIn("SLA unknown", out["text"])
        self.assertEqual(out["html"], "")

    def test_a_quiet_range_still_says_no_jobs(self):
        out = self._render({"slaTotalJobs": 0, "slaMetJobs": 0, "activeJobs": 0, "historyAvailable": True})
        self.assertEqual(out["text"], "No backup jobs ran in this range")


if __name__ == "__main__":
    unittest.main()
