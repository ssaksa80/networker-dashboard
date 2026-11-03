"""A report says which window its job data really covers, and flags a short one.

The defect (review C13): NetWorker's /global/jobs has no server-side time
filter, so the dashboard downloads the whole retained jobs database and trims
it to the report window. That database keeps only its retention period, often
a few days, so "Last Week" and "Last Month" silently covered just those days
while the label, the totals and the SLA claimed the whole range.

Each collector now records the oldest job the database still holds, before
the window filter, as summary.coverage (restapi.report_coverage). When it is
more than an hour after the requested start, the dashboard, the daily email
and its snapshot show the window actually covered.

The NetWorker calls are replaced by recorded-shape payloads (scrubbed
placeholders only), so the collectors run from their data boundary.
"""
import json
import shutil
import subprocess
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from nwdash import nwui, reports, restapi
from nwdash.models import ApiConfig, coverage_notice
from nwdash.restapi import display_datetime, report_coverage
from nwdash.wmi_health import unavailable_server_health

ASSETS = Path(__file__).resolve().parent.parent / "nwdash" / "assets"
NODE = shutil.which("node")
DAY = 24 * 60 * 60


def _iso(ts):
    return datetime.fromtimestamp(ts, UTC).isoformat()


def _rest_job(ts, client="client-a.example.com", status="Succeeded"):
    """A /global/jobs record with the JOB_QUERY_FIELDS the dashboard asks for."""
    return {
        "clientHostname": client,
        "startTime": _iso(ts),
        "completionStatus": status,
        "name": f"{client}:/data",
        "policyActionName": "backup",
        "workflowName": "Gold",
        "level": "incr",
    }


def _retained_jobs(days_held, now):
    """A jobs database that holds `days_held` days: one job a day, the oldest first."""
    return [_rest_job(now - (day * DAY) - 600) for day in range(int(days_held), -1, -1)]


def _config(report_range, api_mode="rest", start="", end=""):
    return ApiConfig(
        rest_api_host="nw.example.com", rest_api_port=9090,
        backup_server_host="nw.example.com", backup_server_port=9001,
        username="administrator", password="secret", api_mode=api_mode, api_version="v3",
        report_range=report_range, custom_start_date=start, custom_end_date=end,
        use_wmi_health=False, wmi_username="", wmi_password="",
        timeout_seconds=30, verify_tls=True, use_authc_header=False,
    )


class TestReportCoverage(unittest.TestCase):
    def setUp(self):
        self.end = time.time()
        self.start = self.end - 30 * DAY

    def test_a_database_older_than_the_range_covers_it(self):
        coverage = report_coverage(self.start, self.end, self.start - DAY)
        self.assertFalse(coverage["short"])
        self.assertEqual(coverage["start"], display_datetime(self.start))
        self.assertEqual(coverage["end"], display_datetime(self.end))

    def test_a_three_day_database_covers_three_days_of_a_month(self):
        oldest = self.end - 3 * DAY
        coverage = report_coverage(self.start, self.end, oldest)
        self.assertTrue(coverage["short"])
        self.assertEqual(coverage["start"], display_datetime(oldest))
        self.assertEqual(coverage["requestedStart"], display_datetime(self.start))

    def test_a_job_just_after_the_start_is_not_a_gap(self):
        self.assertFalse(report_coverage(self.start, self.end, self.start + 30 * 60)["short"])

    def test_a_range_before_the_oldest_job_is_not_covered_at_all(self):
        coverage = report_coverage(self.start, self.end, self.end + DAY)
        self.assertTrue(coverage["short"])
        self.assertEqual(coverage["start"], "")

    def test_an_empty_database_flags_nothing(self):
        self.assertFalse(report_coverage(self.start, self.end, 0.0)["short"])


class TestRestDashboardRecordsCoverage(unittest.TestCase):
    def _build(self, report_range, days_held):
        jobs = _retained_jobs(days_held, time.time())

        def fetch(url, headers, timeout, context, label, max_bytes=0):
            return {
                "clients": {"clients": [{"hostname": "client-a.example.com"}]},
                "jobs": {"jobs": jobs},
                "failedJobs": {"jobs": []},
                "alerts": {"alerts": []},
                "policies": {"protectionPolicies": []},
            }[label]

        with mock.patch.object(restapi, "fetch_json", side_effect=fetch), \
                mock.patch.object(restapi, "load_server_health_rest", return_value=unavailable_server_health()):
            return restapi.build_dashboard_rest(_config(report_range))[1]

    def test_last_month_on_a_three_day_database_is_short(self):
        body = self._build("30d", days_held=3)
        coverage = body["summary"]["coverage"]
        self.assertTrue(coverage["short"])
        self.assertIn("Last Month was requested", coverage_notice(body))
        self.assertIn(coverage["start"], coverage_notice(body))

    def test_last_day_on_a_three_day_database_is_covered(self):
        body = self._build("24h", days_held=3)
        self.assertFalse(body["summary"]["coverage"]["short"])
        self.assertEqual(coverage_notice(body), "")


class TestNwuiDashboardRecordsCoverage(unittest.TestCase):
    """The NWUI history goes through the real cached_nwui_job_history and REST
    fallback, so the oldest job is taken before the report-window filter."""

    def _build(self, report_range, days_held):
        jobs = _retained_jobs(days_held, time.time())
        nwui._JOBS_HISTORY_CACHE.clear()
        with mock.patch.object(nwui, "nwui_login", return_value=({"Authorization": "Bearer t"}, {"status": "ok"})), \
                mock.patch.object(nwui, "nwui_monitoring_all_pages", return_value=[]), \
                mock.patch.object(nwui, "fetch_json", return_value={"jobs": jobs}), \
                mock.patch.object(nwui, "load_server_health_nwui", return_value=unavailable_server_health()):
            try:
                return nwui.build_dashboard_nwui(_config(report_range, api_mode="nwui"), create_session=False)[1]
            finally:
                nwui._JOBS_HISTORY_CACHE.clear()

    def test_last_week_on_a_two_day_database_is_short(self):
        body = self._build("7d", days_held=2)
        self.assertTrue(body["summary"]["historyAvailable"])
        self.assertTrue(body["summary"]["coverage"]["short"])
        self.assertEqual(body["summary"]["successfulJobs"], 3, "the retained jobs are still counted")

    def test_last_week_on_a_ten_day_database_is_covered(self):
        body = self._build("7d", days_held=10)
        self.assertFalse(body["summary"]["coverage"]["short"])
        self.assertEqual(body["summary"]["successfulJobs"], 7)

    def test_a_cached_history_keeps_its_coverage(self):
        jobs = _retained_jobs(2, time.time())
        nwui._JOBS_HISTORY_CACHE.clear()
        try:
            with mock.patch.object(nwui, "fetch_json", return_value={"jobs": jobs}) as fetch:
                first = nwui.cached_nwui_job_history(_config("30d", api_mode="nwui"), None)
                second = nwui.cached_nwui_job_history(_config("30d", api_mode="nwui"), None)
            self.assertEqual(fetch.call_count, 1)
            self.assertTrue(second[2], "served from the cache")
            self.assertEqual(first[3], second[3])
            self.assertGreater(second[3], 0)
        finally:
            nwui._JOBS_HISTORY_CACHE.clear()


class TestEmailShowsTheCoveredWindow(unittest.TestCase):
    def setUp(self):
        end = time.time()
        self.short = {
            "generatedAt": display_datetime(end),
            "summary": {
                "range": "30d", "rangeLabel": "Last Month", "health": "ok",
                "totalJobs": 3, "successfulJobs": 3, "slaTotalJobs": 3, "slaMetJobs": 3, "slaPercent": 100.0,
                "historyAvailable": True, "slaAvailable": True,
                "coverage": report_coverage(end - 30 * DAY, end, end - 3 * DAY),
            },
            "sources": {},
        }
        self.full = json.loads(json.dumps(self.short))
        self.full["summary"]["coverage"] = report_coverage(end - 30 * DAY, end, end - 40 * DAY)

    def test_a_short_range_is_flagged_in_the_email(self):
        plain, html_body = reports.dashboard_report_email(self.short)
        covered = self.short["summary"]["coverage"]["start"]
        self.assertIn("Shorter than requested", html_body)
        self.assertIn(f"Data covers: {covered} to ", plain)
        self.assertIn("Last Month was requested", plain)

    def test_a_short_range_is_flagged_in_the_snapshot(self):
        self.assertIn("Shorter than requested", reports.dashboard_snapshot_html(self.short))

    def test_a_covered_range_still_shows_its_window_without_a_flag(self):
        plain, html_body = reports.dashboard_report_email(self.full)
        self.assertNotIn("Shorter than requested", html_body)
        self.assertIn(f"Data covers: {self.full['summary']['coverage']['start']} to ", plain)

    def test_a_dashboard_from_before_coverage_has_no_row(self):
        old = json.loads(json.dumps(self.full))
        del old["summary"]["coverage"]
        plain, _ = reports.dashboard_report_email(old)
        self.assertNotIn("Data covers", plain)


@unittest.skipIf(NODE is None, "node is not on PATH; skipping app.js logic tests")
class TestDashboardCoverageNotice(unittest.TestCase):
    def _notice(self, summary):
        source = (ASSETS / "app.js").read_text(encoding="utf-8")
        start = source.index("    function coverageNotice(summary) {")
        end = source.index("\n    }\n", start) + len("\n    }\n")
        script = source[start:end] + f"process.stdout.write(JSON.stringify(coverageNotice({json.dumps(summary)})));\n"
        proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=30, check=False)
        if proc.returncode != 0:
            raise AssertionError(proc.stderr)
        return json.loads(proc.stdout)

    def test_a_short_range_says_what_it_covers(self):
        dashboard = {"summary": {"rangeLabel": "Last Week", "coverage": {
            "short": True, "start": "01-11-2026 08:00:00 UTC", "end": "03-11-2026 08:00:00 UTC", "oldestJob": "01-11-2026 08:00:00 UTC"}}}
        text = self._notice(dashboard["summary"])
        self.assertEqual(text, coverage_notice(dashboard), "the page and the email say the same")
        self.assertIn("covers 01-11-2026 08:00:00 UTC to 03-11-2026 08:00:00 UTC", text)

    def test_a_covered_range_says_nothing(self):
        self.assertEqual(self._notice({"rangeLabel": "Last Week", "coverage": {"short": False}}), "")
        self.assertEqual(self._notice({"rangeLabel": "Last Week"}), "")


if __name__ == "__main__":
    unittest.main()
