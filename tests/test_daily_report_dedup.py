"""A scheduled daily report is sent once per day and window, whatever its content.

The defect (review C3): the daily report was deduplicated on a hash of the
dashboard's content, which holds no date. A stable, all-green estate produces
the same hash every morning, so after the first email every later daily report
was skipped as "no change since last successful report". Content dedup belongs
to alerts (the same alert must not repeat); a daily report is deduplicated on
(date, report window) instead.
"""
import copy
import unittest
from datetime import datetime
from http import HTTPStatus
from unittest import mock

from nwdash import emailer
from nwdash.models import AlertAutomation, _pop_automation, _put_automation

ALL_GREEN = {
    "ok": True,
    "generatedAt": "28-10-2026 08:00:00 UTC",
    "summary": {
        "totalJobs": 40, "successfulJobs": 40, "failedJobs": 0, "activeJobs": 0,
        "recoveryJobs": 0, "cloneJobs": 0, "totalAlerts": 0, "slaPercent": 100.0,
        "health": "ok", "range": "24h", "rangeLabel": "Last 24 Hours",
    },
    "sources": {},
    "tables": {"jobs": [], "failedJobs": [], "alerts": []},
}


class _FrozenClock(datetime):
    """emailer's datetime, pinned to a settable local wall-clock time."""

    current = datetime(2026, 10, 28, 8, 0)

    @classmethod
    def now(cls, tz=None):
        return cls.current if tz is None else cls.current.astimezone(tz)


class TestDailyReportDedup(unittest.TestCase):
    def setUp(self):
        self.automation = AlertAutomation(
            automation_id="daily-dedup:0000c3c3", session_id="d" * 32,
            smtp_host="smtp.example.com", smtp_port=25, smtp_username="", encrypted_smtp_password="",
            smtp_from="dashboard@example.com", recipients=["daily-dedup@example.com"],
            smtp_security="none", interval_minutes=60, trigger="critical",
            schedule_type="daily_report", report_time="08:00", created_at=1.0,
        )
        _put_automation(self.automation.automation_id, self.automation)
        self.addCleanup(_pop_automation, self.automation.automation_id)
        self.dashboard = copy.deepcopy(ALL_GREEN)
        self.sent = []
        _FrozenClock.current = datetime(2026, 10, 28, 8, 0)
        for name, value in (
            ("datetime", _FrozenClock),
            ("_ensure_automation_session", lambda _automation: True),
            ("build_dashboard_from_session",
             lambda *_args, **_kwargs: (HTTPStatus.OK, copy.deepcopy(self.dashboard))),
            ("scheduled_dashboard_email_payload", lambda _dashboard: ("report", "<p>report</p>", {})),
            ("load_ui_theme", lambda: "default"),
            ("send_smtp_email", self._record),
            ("persist_automations", lambda: None),
        ):
            patcher = mock.patch.object(emailer, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.dict(emailer._LAST_SENT_BY_IDENTITY, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _record(self, _automation, subject, *_args, **_kwargs):
        self.sent.append((_FrozenClock.current.date(), subject))
        return {"stage": "sent", "host": "smtp.example.com", "port": 25}

    def fire(self):
        emailer.run_alert_automation(self.automation.automation_id)

    def test_unchanged_all_green_report_is_sent_every_day(self):
        self.fire()
        _FrozenClock.current = datetime(2026, 10, 29, 8, 0)
        self.fire()
        _FrozenClock.current = datetime(2026, 10, 30, 8, 0)
        self.fire()
        self.assertEqual(
            [day.isoformat() for day, _subject in self.sent],
            ["2026-10-28", "2026-10-29", "2026-10-30"],
            f"last result: {self.automation.last_result}",
        )

    def test_second_run_on_the_same_day_and_window_is_skipped(self):
        self.fire()
        _FrozenClock.current = datetime(2026, 10, 28, 8, 30)
        self.dashboard["summary"]["failedJobs"] = 3  # content changes, day and window do not
        self.fire()
        self.assertEqual(len(self.sent), 1)
        self.assertIn("already sent", self.automation.last_result)

    def test_a_different_window_on_the_same_day_is_sent(self):
        self.fire()
        self.dashboard["summary"].update(range="7d", rangeLabel="Last Week")
        self.fire()
        self.assertEqual(len(self.sent), 2)

    def test_alerts_keep_content_dedup(self):
        self.automation.schedule_type = "alert"
        self.automation.trigger = "all"
        self.dashboard["summary"].update(health="critical", failedJobs=2)
        self.dashboard["tables"]["failedJobs"] = [{"name": "nightly", "client": "client-a", "status": "failed"}]
        with mock.patch.object(emailer, "within_quiet_hours", lambda *_args: False):
            self.fire()
            self.automation.last_run = 0.0  # past the cooldown: only content dedup can hold it back
            _FrozenClock.current = datetime(2026, 10, 29, 8, 0)
            self.fire()
        self.assertEqual(len(self.sent), 1, f"last result: {self.automation.last_result}")


if __name__ == "__main__":
    unittest.main()
