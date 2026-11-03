"""Restores are counted once, as restores (review C4).

NetWorker's jobs database lists restores beside backups. Both collectors
counted them in the backup totals and the backup SLA, and then again as
restores, so a busy restore day lowered or raised the backup SLA and the
report's activity figure (successful + failed + running + restores) counted
each restore twice. NWUI mode also listed a restore from its recoveries
monitor beside the same restore from the jobs database.
"""
import unittest
from unittest import mock

from collector_fixtures import NwuiServer, RestServer, ago, nwui_recovery, rest_job, restore_job

from nwdash import reports


def _day_with_restores():
    started = ago(3)
    return [
        rest_job(6101, "fs01.example.com", "Succeeded"),
        rest_job(6102, "fs02.example.com", "Failed"),
        restore_job(6103, "fs03.example.com", "Succeeded", start=started),
        restore_job(6104, "fs04.example.com", "Failed"),
    ]


class TestRestMode(unittest.TestCase):
    def test_restores_are_not_backups(self):
        summary = RestServer(jobs=_day_with_restores()).build()["summary"]
        self.assertEqual(summary["totalJobs"], 2)
        self.assertEqual((summary["successfulJobs"], summary["failedJobs"]), (1, 1))
        self.assertEqual((summary["slaTotalJobs"], summary["slaPercent"]), (2, 50.0))

    def test_restores_are_counted_as_restores(self):
        body = RestServer(jobs=_day_with_restores()).build()
        summary = body["summary"]
        self.assertEqual((summary["recoveryJobs"], summary["recoveryFailed"]), (2, 1))
        self.assertEqual(sorted(row["client"] for row in body["tables"]["recovery"]),
                         ["fs03.example.com", "fs04.example.com"])

    def test_a_failed_restore_is_not_a_failed_backup(self):
        body = RestServer(jobs=_day_with_restores()).build()
        self.assertEqual([row["client"] for row in body["tables"]["failedJobs"]], ["fs02.example.com"])

    def test_a_failed_restore_alone_still_makes_the_range_critical(self):
        summary = RestServer(jobs=[rest_job(6201), restore_job(6202, status="Failed")]).build()["summary"]
        self.assertEqual(summary["failedJobs"], 0)
        self.assertEqual(summary["health"], "critical")

    def test_the_report_counts_each_restore_once(self):
        dashboard = RestServer(jobs=_day_with_restores()).build()
        centres = {}
        real_card = reports.report_donut_card

        def card(title, center_value, *args, **kwargs):
            centres[title] = center_value
            return real_card(title, center_value, *args, **kwargs)

        with mock.patch.object(reports, "report_donut_card", card):
            reports.dashboard_report_email(dashboard)
        self.assertEqual(centres["Activity Mix"], "4")


class TestNwuiMode(unittest.TestCase):
    def test_restores_in_the_jobs_database_are_not_backups(self):
        summary = NwuiServer(history=_day_with_restores()).build()["summary"]
        self.assertEqual(summary["completedJobs"], 2)
        self.assertEqual((summary["successfulJobs"], summary["failedJobs"]), (1, 1))
        self.assertEqual((summary["recoveryJobs"], summary["recoveryFailed"]), (2, 1))

    def test_a_restore_both_sources_list_is_counted_once(self):
        history = _day_with_restores()
        same_restore = nwui_recovery("Succeeded", start=history[2]["startTime"], client="FS03.example.com")
        still_running = nwui_recovery("Running", start=ago(0.5), client="fs05.example.com")
        body = NwuiServer(history=history, recoveries=[same_restore, still_running]).build()
        summary = body["summary"]
        self.assertEqual((summary["recoveryJobs"], summary["recoveryRunning"]), (3, 1))
        self.assertEqual([row["client"] for row in body["tables"]["recovery"]],
                         ["fs05.example.com", "fs04.example.com", "fs03.example.com"])

    def test_the_monitor_alone_still_lists_its_restores(self):
        summary = NwuiServer(recoveries=[nwui_recovery("Failed")]).build()["summary"]
        self.assertEqual((summary["recoveryJobs"], summary["recoveryFailed"]), (1, 1))
        self.assertEqual(summary["health"], "critical")


if __name__ == "__main__":
    unittest.main()
