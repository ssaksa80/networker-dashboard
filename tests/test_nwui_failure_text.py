"""A failed job in NWUI mode carries NetWorker's own failure text (review C9).

NWUI mode's completed jobs come from the jobs database's bulk query, which
leaves out `message` because it is many kilobytes of job log per record. So
a failed job showed only its session count ("1 sessions (0 ok, 1 failed,
...)"), never why it failed. The collector now runs the small
completionStatus:"Failed" query with `message`, as REST mode does, and joins
its text to each failed job by job id.

The fake /nwrestapi answers only the fields a query's `fl` asks for, so the
bulk history really has no text.
"""
import unittest

from collector_fixtures import NwuiServer, config, query_fields, rest_job

from nwdash.models import RestApiError

FAILED_TEXT = "Unable to connect to the client: connection refused."


def _failed(job_id, client="fs02.example.com", message=FAILED_TEXT, **kwargs):
    return rest_job(job_id, client, "Failed", message=message, **kwargs)


class TestFailureText(unittest.TestCase):
    def test_a_failed_job_shows_why_it_failed(self):
        server = NwuiServer(history=[rest_job(8101), _failed(8102)])
        rows = server.build()["tables"]["failedJobs"]
        self.assertEqual([row["message"] for row in rows], [FAILED_TEXT])

    def test_each_failure_gets_its_own_text(self):
        # Two save sets of one client, failed in the same second.
        started = "2026-10-15T22:00:07+00:00"
        server = NwuiServer(history=[
            _failed(8201, name="C:\\", message="Save set C:\\ was aborted.", start=started),
            _failed(8202, name="D:\\", message="Disk D:\\ is not mounted.", start=started),
        ])
        body = server.build(config("nwui", "custom", "15-10-2026", "16-10-2026"))
        messages = sorted(row["message"] for row in body["tables"]["failedJobs"])
        self.assertEqual(messages, ["Disk D:\\ is not mounted.", "Save set C:\\ was aborted."])

    def test_the_failed_query_is_skipped_when_nothing_failed(self):
        server = NwuiServer(history=[rest_job(8301), rest_job(8302)])
        server.build()
        self.assertEqual([url for url in server.urls_for("/global/jobs") if "q=" in url], [])

    def test_the_failed_query_asks_for_the_text_and_the_id(self):
        server = NwuiServer(history=[_failed(8401)])
        server.build()
        failed_url = next(url for url in server.urls_for("/global/jobs") if "q=" in url)
        self.assertTrue({"id", "message"} <= set(query_fields(failed_url)))

    def test_without_the_failed_query_the_history_still_counts(self):
        server = NwuiServer(history=[_failed(8501)], failed=RestApiError(502, "HTTP 502 from NetWorker"))
        body = server.build()
        self.assertEqual(body["summary"]["failedJobs"], 1)
        self.assertTrue(body["sources"]["monitoringActionsHistory"]["ok"])
        self.assertIn("1 failed", body["tables"]["failedJobs"][0]["message"])

    def test_a_failure_that_mentions_a_recover_stays_a_failed_backup(self):
        server = NwuiServer(history=[_failed(8601, message="Unable to recover the client file index.")])
        summary = server.build()["summary"]
        self.assertEqual((summary["failedJobs"], summary["recoveryJobs"]), (1, 0))


if __name__ == "__main__":
    unittest.main()
