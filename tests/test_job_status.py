"""One job status classifier for both collectors (review C5).

REST mode and NWUI mode read NetWorker's status text with their own word
lists. A cancelled or abandoned job was nothing at all in REST mode and was
dropped from the NWUI history, "never started" counted as a running job in
REST mode, and REST mode never asked for a job's `state`, so a job that was
still running had no status at all. The SLA of the two modes could not be
compared. Both now use nwdash.job_status.
"""
import unittest

from collector_fixtures import (
    NwuiServer,
    RestServer,
    ago,
    nwui_action,
    query_fields,
    rest_job,
    server_protection_job,
)

from nwdash import nwui, restapi
from nwdash.job_status import classify_status, job_outcome

# Every status text the two collectors have been seen to get, and its outcome.
STATUSES = [
    ("Succeeded", "succeeded"),
    ("Completed", "succeeded"),
    ("successful", "succeeded"),
    ("Succeeded with errors", "succeeded"),
    ("Failed", "failed"),
    ("Error", "failed"),
    ("Aborted", "failed"),
    ("Cancelled", "failed"),
    ("Canceled", "failed"),
    ("Abandoned", "failed"),
    ("Warning", "warning"),
    ("Missed the schedule", "warning"),
    ("Skipped", "warning"),
    ("Interrupted", "warning"),
    ("Never started", "warning"),
    ("Not started", "warning"),
    ("Running", "running"),
    ("In progress", "running"),
    ("Active", "running"),
    ("SessionActive", "running"),
    ("Queued", "queued"),
    ("Waiting", "queued"),
    ("Pending", "queued"),
    ("Unknown", "unknown"),
    ("", "unknown"),
    (None, "unknown"),
]


class TestClassifier(unittest.TestCase):
    def test_every_status_seen_has_one_outcome(self):
        for raw, expected in STATUSES:
            with self.subTest(status=raw):
                self.assertEqual(classify_status(raw), expected)

    def test_both_collectors_read_a_status_the_same_way(self):
        for raw, expected in STATUSES:
            with self.subTest(status=raw):
                self.assertEqual(nwui.normalize_nwui_status(raw), expected)
                self.assertEqual(restapi.classify_job({"completionStatus": raw}), expected)

    def test_a_job_with_no_completion_status_yet_goes_by_its_state(self):
        self.assertEqual(job_outcome("", "Active"), "running")
        self.assertEqual(job_outcome("Unknown", "Queued"), "queued")
        self.assertEqual(job_outcome("Failed", "Active"), "failed")

    def test_a_finished_state_alone_is_not_a_success(self):
        self.assertEqual(job_outcome("", "Completed"), "unknown")

    def test_a_success_with_failed_sessions_is_a_warning_in_nwui(self):
        self.assertEqual(nwui.normalize_nwui_status("Completed", failed_sessions=1), "warning")


def _mixed_jobs():
    """One success, one failure, one cancelled and one abandoned job."""
    return [
        rest_job(9101, "fs01.example.com", "Succeeded"),
        rest_job(9102, "fs02.example.com", "Failed", message="Unable to connect to the client."),
        rest_job(9103, "fs03.example.com", "Cancelled"),
        rest_job(9104, "fs04.example.com", "Abandoned"),
    ]


class TestRestMode(unittest.TestCase):
    def test_the_jobs_query_asks_for_the_state(self):
        server = RestServer()
        server.build()
        jobs_url = next(url for url in server.urls if "q=" not in url and "/global/jobs" in url)
        self.assertIn("state", query_fields(jobs_url))

    def test_a_running_job_is_active(self):
        summary = RestServer(jobs=[
            rest_job(9201, status=None, state="Active"),
            rest_job(9202, status="Unknown", state="Queued"),
        ]).build()["summary"]
        self.assertEqual(summary["activeJobs"], 2)

    def test_a_job_that_never_started_is_not_active(self):
        summary = RestServer(jobs=[rest_job(9301, status="Never started")]).build()["summary"]
        self.assertEqual(summary["activeJobs"], 0)

    def test_cancelled_and_abandoned_jobs_miss_the_sla(self):
        body = RestServer(jobs=_mixed_jobs()).build()
        summary = body["summary"]
        self.assertEqual((summary["successfulJobs"], summary["failedJobs"]), (1, 3))
        self.assertEqual(summary["slaPercent"], 25.0)
        self.assertEqual(summary["health"], "critical")
        clients = sorted(row["client"] for row in body["tables"]["failedJobs"])
        self.assertEqual(clients, ["fs02.example.com", "fs03.example.com", "fs04.example.com"])

    def test_a_failure_keeps_the_text_of_the_failed_query(self):
        rows = RestServer(jobs=_mixed_jobs()).build()["tables"]["failedJobs"]
        messages = {row["client"]: row["message"] for row in rows}
        self.assertEqual(messages["fs02.example.com"], "Unable to connect to the client.")

    def test_a_networker_that_rejects_a_field_still_loads_the_jobs(self):
        server = RestServer(jobs=[rest_job(9401, status=None, state="Active")], rejected={"state"})
        body = server.build()
        self.assertTrue(body["sources"]["jobs"]["ok"], body["sources"]["jobs"].get("error"))
        self.assertEqual(body["summary"]["totalJobs"], 1)


class TestNwuiMode(unittest.TestCase):
    def test_cancelled_and_abandoned_jobs_miss_the_sla(self):
        summary = NwuiServer(history=_mixed_jobs()).build()["summary"]
        self.assertEqual((summary["successfulJobs"], summary["failedJobs"]), (1, 3))
        self.assertEqual(summary["slaPercent"], 25.0)

    def test_a_cancelled_live_action_is_a_failure(self):
        action = nwui_action("Cancelled")
        action["jobData"] = {}
        summary = NwuiServer(live=[action]).build()["summary"]
        self.assertEqual(summary["failedJobs"], 1)


class TestBothModesAgree(unittest.TestCase):
    def test_the_same_jobs_give_the_same_sla(self):
        jobs = [*_mixed_jobs(), rest_job(9105, "fs05.example.com", "Never started")]
        rest = RestServer(jobs=jobs).build()["summary"]
        nwui_summary = NwuiServer(history=jobs).build()["summary"]
        for key in ("successfulJobs", "failedJobs", "slaTotalJobs", "slaPercent"):
            with self.subTest(key=key):
                self.assertEqual(rest[key], nwui_summary[key])


class TestServerProtectionStatus(unittest.TestCase):
    def test_a_cancelled_run_is_failed_not_unknown(self):
        protection = RestServer(jobs=[server_protection_job(9501, "Cancelled", start=ago(3))]).build()[
            "serverProtectionJob"
        ]
        self.assertEqual(protection["status"], "failed")
        self.assertEqual(protection["label"], "Failed")


if __name__ == "__main__":
    unittest.main()
