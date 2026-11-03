"""NWUI job history: every job counted, and the newest first.

Two review findings (2026-10-25), both in the NWUI collector, where completed
jobs come from the NetWorker jobs database (/nwrestapi .../global/jobs) and are
merged with the live monitoringactions feed, which lists running actions only:

- C1: the merge keyed records on (client, action, start second). Several save
  sets of one client start in the same second, so they collapsed into one and
  a failed save set could vanish behind a successful one.
- C2: jobs were sorted by their "dd-mm-YYYY HH:MM:SS" display string, so the
  "latest" job was the one with the highest day of the month. Server
  Protection detection and the 80-row tables read the wrong end of the list.

The collector runs for real here. Only the network is replaced, by answers
shaped like recorded NWUI and REST responses (placeholder hostnames only).
"""
import json
import unittest
from http import HTTPStatus
from http.cookiejar import CookieJar
from unittest import mock
from urllib.parse import parse_qs, urlparse

from nwdash import nwui
from nwdash.models import ApiConfig


def _config():
    # A custom window across a month end: the only place a day-first display
    # string sorts differently from time.
    return ApiConfig(
        rest_api_host="nw.example.com", rest_api_port=9090,
        backup_server_host="nw.example.com", backup_server_port=9090,
        username="administrator", password="history-test-pw", api_mode="nwui", api_version="v3",
        report_range="custom", custom_start_date="20-09-2026", custom_end_date="10-10-2026",
        use_wmi_health=False, wmi_username="", wmi_password="",
        timeout_seconds=30, verify_tls=True, use_authc_header=False,
    )


def _iso(day, hour=12, minute=0, second=0):
    """A /global/jobs startTime. Noon UTC keeps each job on its calendar day in
    every time zone the display string could use."""
    return f"2026-{day}T{hour:02}:{minute:02}:{second:02}+00:00"


def _rest_job(job_id, client, name, status, start, action="backup", workflow="Gold-Filesystems"):
    """One /global/jobs record, with the fields the dashboard asks for. An
    action job names no client host (client=None)."""
    job = {
        "startTime": start,
        "completionStatus": status,
        "name": name,
        "policyActionName": action,
        "workflowName": workflow,
        "level": "incr",
    }
    if client is not None:
        job["clientHostname"] = client
    if job_id is not None:
        job["id"] = job_id
    return job


def _server_protection_job(job_id, status, start):
    return _rest_job(job_id, "nw.example.com", "bootstrap", status, start,
                     action="server db backup", workflow="Server backup")


def _live_action(status="Running", start="2026-10-05T21:00:00+00:00", workflow="Gold-Filesystems"):
    """One /nwui/api/monitoringactions entry."""
    return {
        "workflowName": workflow,
        "actionName": "backup",
        "policyName": "Gold",
        "status": status,
        "startTime": start,
        "duration": 120000,
        "jobCmd": f"nsrworkflow -p Gold -w {workflow} -A backup -s nw.example.com",
        "jobData": {
            "successfulInputCount": 3, "failedInputCount": 0, "waitingInputCount": 2,
            "runningInputCount": 1, "canceledInputCount": 0,
        },
    }


def _jobs_answer(jobs):
    return {"count": len(jobs), "jobs": jobs}


class _CollectorCase(unittest.TestCase):
    """build_dashboard_nwui with the NWUI pages and the jobs database answered
    from self.live and self.history (a /global/jobs body, or an error)."""

    def setUp(self):
        self.live = []
        self.history = _jobs_answer([])
        self.history_urls = []

        def _pages(_config, _opener, _headers, endpoint_name, start_ts=None, end_ts=None, paging=None):
            return list(self.live) if endpoint_name == "monitoringactions" else []

        def _fetch(url, _headers, _timeout, _context, _label, max_bytes=0):
            self.history_urls.append(url)
            if isinstance(self.history, Exception):
                raise self.history
            return json.loads(json.dumps(self.history))

        for name, value in (
            ("nwui_monitoring_all_pages", _pages),
            ("fetch_json", _fetch),
            ("load_server_health_nwui", lambda *_args: {"status": "ok", "label": "Healthy"}),
        ):
            patcher = mock.patch.object(nwui, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.dict(nwui._JOBS_HISTORY_CACHE, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def build(self, config=None):
        status, body = nwui.build_dashboard_nwui(
            config or _config(), cookie_jar=CookieJar(),
            auth_headers={"Authorization": "Bearer nwui-test"}, create_session=False,
        )
        self.assertEqual(status, HTTPStatus.OK, body.get("error"))
        return body


class TestEverySaveSetIsCounted(_CollectorCase):
    """C1: jobs database records are told apart by their NetWorker job id."""

    def two_save_sets_in_one_second(self, with_ids=True):
        start = _iso("10-05", 22, 0, 7)
        self.history = _jobs_answer([
            _rest_job(410211 if with_ids else None, "fs01.example.com", "C:\\", "Succeeded", start),
            _rest_job(410212 if with_ids else None, "fs01.example.com", "D:\\", "Failed", start),
        ])

    def test_a_failed_save_set_beside_a_good_one_is_counted(self):
        self.two_save_sets_in_one_second()
        summary = self.build()["summary"]
        self.assertEqual(summary["completedJobs"], 2)
        self.assertEqual(summary["failedJobs"], 1)
        self.assertEqual(summary["health"], "critical")

    def test_the_failure_reaches_the_failed_jobs_table(self):
        self.two_save_sets_in_one_second()
        failed = self.build()["tables"]["failedJobs"]
        self.assertEqual([row["client"] for row in failed], ["fs01.example.com"])

    def test_the_jobs_database_is_asked_for_the_job_id(self):
        self.build()
        self.assertEqual(len(self.history_urls), 1)
        fields = parse_qs(urlparse(self.history_urls[0]).query)["fl"][0].split(",")
        self.assertIn("id", fields)

    def test_the_same_job_listed_twice_is_counted_once(self):
        start = _iso("10-05", 22, 0, 7)
        record = _rest_job(410211, "fs01.example.com", "C:\\", "Failed", start)
        self.history = _jobs_answer([record, dict(record)])
        self.assertEqual(self.build()["summary"]["completedJobs"], 1)

    def test_records_without_an_id_merge_as_before(self):
        # A NetWorker that refuses the field gets it stripped from the query;
        # the merge then has only (client, action, start second) to go on.
        self.two_save_sets_in_one_second(with_ids=False)
        self.assertEqual(self.build()["summary"]["completedJobs"], 1)


class TestMergeKeepsLiveRunsPaired(unittest.TestCase):
    """merge_action_history still replaces a running live record with the
    finished record of the same run, and only that one record."""

    @staticmethod
    def history_item(job_id, status, start="2026-10-05T21:00:00+00:00"):
        # The action job of the live run: with no client host, its workflow
        # name fills the field the merge keys on.
        return nwui.rest_job_as_nwui_action(_rest_job(job_id, None, "backup", status, start))

    def test_a_finished_record_replaces_its_running_live_twin(self):
        merged = nwui.merge_action_history([_live_action()], [self.history_item(5001, "Succeeded")])
        self.assertEqual([item["status"] for item in merged], ["Succeeded"])

    def test_the_live_twin_absorbs_one_record_only(self):
        merged = nwui.merge_action_history(
            [_live_action()], [self.history_item(5001, "Succeeded"), self.history_item(5002, "Failed")]
        )
        self.assertEqual(sorted(item["status"] for item in merged), ["Failed", "Succeeded"])

    def test_the_rest_fallback_feed_does_not_count_its_jobs_twice(self):
        # When monitoringactions fails, the live list is the jobs database
        # itself, and the history merged into it holds the same records.
        start = _iso("10-05", 22, 0, 7)
        jobs = [
            nwui.rest_job_as_nwui_action(_rest_job(410211, "fs01.example.com", "C:\\", "Succeeded", start)),
            nwui.rest_job_as_nwui_action(_rest_job(410212, "fs01.example.com", "D:\\", "Failed", start)),
        ]
        merged = nwui.merge_action_history(jobs, [dict(job) for job in jobs])
        self.assertEqual(len(merged), 2)


class TestNewestJobsFirst(_CollectorCase):
    """C2: "latest" is the newest start time, not the highest day of the month."""

    def test_server_protection_reports_the_newest_run(self):
        self.history = _jobs_answer([
            _server_protection_job(700001, "Failed", _iso("09-29")),
            _server_protection_job(700002, "Succeeded", _iso("10-02")),
        ])
        protection = self.build()["serverProtectionJob"]
        self.assertEqual(protection["status"], "succeeded", protection["detail"])

    def test_the_jobs_table_keeps_the_newest_rows(self):
        # 100 jobs, 80 rows: the 50 October jobs must all be kept, newest first.
        september = [
            _rest_job(800000 + n, f"sep{n:02}.example.com", "C:\\", "Succeeded", _iso("09-29", 12, n))
            for n in range(50)
        ]
        october = [
            _rest_job(810000 + n, f"oct{n:02}.example.com", "C:\\", "Succeeded", _iso("10-02", 12, n))
            for n in range(50)
        ]
        self.history = _jobs_answer(september + october)
        rows = self.build()["tables"]["jobs"]
        self.assertEqual(len(rows), 80)
        self.assertEqual([row["client"] for row in rows[:50]], [f"oct{n:02}.example.com" for n in reversed(range(50))])

    def test_the_server_protection_refresh_reports_the_newest_run(self):
        # The Server Protection refresh reads the live feed on its own.
        self.live = [
            _live_action("Failed", "2026-09-29T12:00:00+00:00", workflow="Server backup"),
            _live_action("Succeeded", "2026-10-02T12:00:00+00:00", workflow="Server backup"),
        ]
        status = nwui.refresh_server_protection_job_nwui(_config(), CookieJar(), {"Authorization": "Bearer t"})
        self.assertEqual(status["status"], "succeeded", status["detail"])

    def test_a_policy_shows_its_newest_run(self):
        jobs = [
            nwui.project_nwui_job(_live_action("Failed", "2026-09-29T12:00:00+00:00")),
            nwui.project_nwui_job(_live_action("Succeeded", "2026-10-02T12:00:00+00:00")),
        ]
        rows = nwui.build_nwui_policies([], jobs)
        self.assertEqual([row["severity"] for row in rows], ["succeeded"])


if __name__ == "__main__":
    unittest.main()
