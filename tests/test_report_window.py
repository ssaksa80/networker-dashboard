"""Which report window a job belongs to (review C7, and the window half of C23).

- C7: jobs were windowed by when they started, so a backup that started at
  22:00 and failed at 01:00 the next day was in neither daily report: the
  first was sent before it failed, and the second did not count it. A
  finished job now belongs to the window it ended in.
- C23: window ends were inclusive, so a job at exactly midnight was in two
  daily reports; and a NetWorker time with no UTC offset was read as UTC,
  while the custom date window is local time. Windows are now half-open,
  [start, end), and an offset-less time is local.

Fixture times are written with their UTC offset, from local wall-clock times,
so the tests mean the same on any host time zone.
"""
import os
import subprocess
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path

from collector_fixtures import NwuiServer, RestServer, config, iso, nwui_action, query_fields, rest_job

from nwdash import nwui

ROOT = Path(__file__).resolve().parent.parent


def local(day, hour, minute=0):
    """An October 2026 local wall-clock time, as epoch seconds."""
    return datetime(2026, 10, day, hour, minute).timestamp()


def daily(day, api_mode="rest"):
    """The custom one-day report window for that October day."""
    date = f"{day:02}-10-2026"
    return config(api_mode, "custom", date, date)


def overnight_failure():
    """A backup that starts at 22:00 on the 14th and fails at 01:00 on the 15th."""
    return rest_job(7101, "fs01.example.com", "Failed", start=iso(local(14, 22)), end=iso(local(15, 1)))


class TestRestMode(unittest.TestCase):
    def test_the_jobs_query_asks_for_the_end_time(self):
        server = RestServer()
        server.build()
        jobs_url = next(url for url in server.urls if "/global/jobs" in url and "q=" not in url)
        self.assertIn("endTime", query_fields(jobs_url))

    def test_an_overnight_failure_is_in_the_report_of_the_day_it_failed(self):
        summary = RestServer(jobs=[overnight_failure()]).build(daily(15))["summary"]
        self.assertEqual(summary["failedJobs"], 1)

    def test_an_overnight_failure_is_not_in_the_report_of_the_day_it_started(self):
        summary = RestServer(jobs=[overnight_failure()]).build(daily(14))["summary"]
        self.assertEqual(summary["failedJobs"], 0)

    def test_a_running_job_belongs_to_the_day_it_started(self):
        job = rest_job(7201, status=None, state="Active", start=iso(local(14, 23)))
        self.assertEqual(RestServer(jobs=[job]).build(daily(14))["summary"]["activeJobs"], 1)
        self.assertEqual(RestServer(jobs=[job]).build(daily(15))["summary"]["activeJobs"], 0)

    def test_a_job_with_no_end_time_goes_by_its_start(self):
        # A NetWorker that does not return endTime still reports its jobs.
        job = rest_job(7301, status="Succeeded", start=iso(local(14, 23)))
        self.assertEqual(RestServer(jobs=[job]).build(daily(14))["summary"]["successfulJobs"], 1)

    def test_a_job_at_midnight_is_in_one_daily_report(self):
        job = rest_job(7401, status=None, state="Active", start=iso(local(15, 0)))
        self.assertEqual(RestServer(jobs=[job]).build(daily(14))["summary"]["activeJobs"], 0)
        self.assertEqual(RestServer(jobs=[job]).build(daily(15))["summary"]["activeJobs"], 1)


class TestNwuiMode(unittest.TestCase):
    def test_an_overnight_failure_is_in_the_report_of_the_day_it_failed(self):
        body = NwuiServer(history=[overnight_failure()]).build(daily(15, "nwui"))
        self.assertEqual(body["summary"]["failedJobs"], 1)
        body = NwuiServer(history=[overnight_failure()]).build(daily(14, "nwui"))
        self.assertEqual(body["summary"]["failedJobs"], 0)

    def test_the_unwindowed_feed_keeps_an_action_that_finished_in_the_window(self):
        # The last NWUI paging strategy asks without a window and trims here.
        finished = nwui_action("Failed", start=iso(local(14, 22)), completionTime=iso(local(15, 1)))
        start_ts, end_ts = local(15, 0), local(16, 0)
        self.assertEqual(nwui.filter_items_to_report_window([finished], start_ts, end_ts), [finished])
        self.assertEqual(nwui.filter_items_to_report_window([finished], local(14, 0), start_ts), [])

    def test_the_unwindowed_feed_ends_its_window_before_midnight(self):
        running = nwui_action("Running", start=iso(local(15, 0)))
        self.assertEqual(nwui.filter_items_to_report_window([running], local(14, 0), local(15, 0)), [])


def _timestamp_in_zone(tz, text):
    """restapi.timestamp(text) in a Python whose local zone is `tz` (a POSIX TZ
    string, which the Windows C runtime reads too)."""
    env = {**os.environ, "TZ": tz}
    code = "import sys; from nwdash.restapi import timestamp; print(timestamp(sys.argv[1]))"
    result = subprocess.run(
        [sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(ROOT)!r}); {code}", text],
        capture_output=True, text=True, env=env, check=True, timeout=60,
    )
    return float(result.stdout.strip())


class TestTimesWithoutAnOffsetAreLocal(unittest.TestCase):
    def test_an_iso_time_with_no_offset_is_local(self):
        utc_10 = datetime(2026, 10, 15, 10, tzinfo=UTC).timestamp()
        self.assertEqual(_timestamp_in_zone("XXX-3", "2026-10-15T13:00:00"), utc_10)
        self.assertEqual(_timestamp_in_zone("XXX+5", "2026-10-15T05:00:00"), utc_10)

    def test_a_ctime_style_time_is_local(self):
        utc_10 = datetime(2026, 10, 15, 10, tzinfo=UTC).timestamp()
        self.assertEqual(_timestamp_in_zone("XXX-3", "Thu Oct 15 13:00:00 2026"), utc_10)

    def test_a_time_with_an_offset_is_unchanged(self):
        utc_10 = datetime(2026, 10, 15, 10, tzinfo=UTC).timestamp()
        self.assertEqual(_timestamp_in_zone("XXX-3", "2026-10-15T10:00:00Z"), utc_10)
        self.assertEqual(_timestamp_in_zone("XXX+5", "2026-10-15T13:00:00+03:00"), utc_10)


if __name__ == "__main__":
    unittest.main()
