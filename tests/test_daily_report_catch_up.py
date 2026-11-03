"""A daily report that fails, or whose send time passes while the service is
down, still goes out that day.

The defect (review C6): every run, failed or not, scheduled the next one for
the next day's send time, so a NetWorker or SMTP hiccup at 08:00 cost the whole
day's report. The run state (next_run_at, last_run) was never saved, so a
restart rescheduled from scratch: a service down over the send time lost that
day, and every restart pushed each alert check a whole interval away.
"""
import json
import tempfile
import time
import unittest
from datetime import datetime
from http import HTTPStatus
from pathlib import Path
from unittest import mock

from nwdash import emailer, models, snapshots
from nwdash.models import AlertAutomation

DASHBOARD = {
    "ok": True,
    "generatedAt": "28-10-2026 08:00:00 UTC",
    "summary": {"totalJobs": 4, "failedJobs": 0, "health": "ok", "range": "24h", "rangeLabel": "Last 24 Hours"},
    "sources": {},
    "tables": {"jobs": [], "failedJobs": [], "alerts": []},
}
HOUR = 3600


class _FrozenClock(datetime):
    """emailer's datetime, pinned to a settable local wall-clock time."""

    current = datetime(2026, 10, 28, 8, 0)

    @classmethod
    def now(cls, tz=None):
        return cls.current if tz is None else cls.current.astimezone(tz)


def _automation(schedule_type="daily_report", **overrides):
    fields = {
        "automation_id": f"c6-catch-up:{schedule_type}", "session_id": "c" * 32,
        "smtp_host": "smtp.example.com", "smtp_port": 25, "smtp_username": "", "encrypted_smtp_password": "",
        "smtp_from": "dashboard@example.com", "recipients": ["catch-up@example.com"],
        "smtp_security": "none", "interval_minutes": 60, "trigger": "all",
        "schedule_type": schedule_type, "report_time": "08:00", "created_at": 1.0,
        # A profile schedule: the config-driven reconcile at restore leaves it alone.
        "profile_name": "c6",
    }
    fields.update(overrides)
    return AlertAutomation(**fields)


class _ClockCase(unittest.TestCase):
    def setUp(self):
        _FrozenClock.current = datetime(2026, 10, 28, 8, 0)
        patchers = [
            mock.patch.object(emailer, "datetime", _FrozenClock),
            mock.patch.dict(models.ALERT_AUTOMATIONS, clear=True),
            mock.patch.dict(emailer._LAST_SENT_BY_IDENTITY, clear=True),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def at(self, hour, minute=0, day=28):
        _FrozenClock.current = datetime(2026, 10, day, hour, minute)

    def assert_due_in(self, automation, seconds):
        self.assertAlmostEqual(automation.next_run_at - time.time(), seconds, delta=5)


class TestAFailedDailyRunIsRetriedThatDay(_ClockCase):
    def setUp(self):
        super().setUp()
        self.automation = _automation()
        models.ALERT_AUTOMATIONS[self.automation.automation_id] = self.automation
        self.build = (HTTPStatus.BAD_GATEWAY, {"ok": False, "error": "NetWorker did not answer"})
        self.session_ready = True
        self.sent = []
        for name, value in (
            ("_ensure_automation_session", lambda _automation: self.session_ready),
            ("build_dashboard_from_session", lambda *_args, **_kwargs: self.build),
            ("scheduled_dashboard_email_payload", lambda _dashboard: ("report", "<p>report</p>", {})),
            ("load_ui_theme", lambda: "default"),
            ("send_smtp_email", lambda *_args, **_kwargs: self.sent.append(1) or {"host": "smtp", "port": 25}),
            ("persist_automations", lambda: None),
        ):
            patcher = mock.patch.object(emailer, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def fire(self):
        emailer.run_alert_automation(self.automation.automation_id)

    def test_a_failed_build_is_retried_within_minutes(self):
        self.fire()
        self.assert_due_in(self.automation, 300)

    def test_an_unreachable_session_is_retried_too(self):
        self.session_ready = False
        self.fire()
        self.assert_due_in(self.automation, 300)

    def test_an_smtp_failure_is_retried_too(self):
        self.build = (HTTPStatus.OK, dict(DASHBOARD))

        def _refuse(*_args, **_kwargs):
            raise models.SmtpDeliveryError("connect", "connection refused", {})

        with mock.patch.object(emailer, "send_smtp_email", _refuse):
            self.fire()
        self.assert_due_in(self.automation, 300)

    def test_the_retries_back_off_to_hourly(self):
        waits = []
        for hour, minute in ((8, 0), (8, 5), (8, 20), (8, 50), (9, 50)):
            self.at(hour, minute)
            self.fire()
            waits.append(round((self.automation.next_run_at - time.time()) / 60))
        self.assertEqual(waits, [5, 15, 30, 60, 60])

    def test_a_retry_never_runs_into_the_next_day(self):
        # Past midnight it would send the next day's report, early and under its date.
        self.at(23, 58)
        self.fire()
        self.assert_due_in(self.automation, 8 * HOUR + 2 * 60)

    def test_once_sent_it_waits_for_tomorrow_and_the_backoff_starts_over(self):
        self.fire()
        self.build = (HTTPStatus.OK, dict(DASHBOARD))
        self.at(8, 5)
        self.fire()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.automation.failures, 0)
        self.assert_due_in(self.automation, 24 * HOUR - 5 * 60)

    def test_a_paused_schedule_is_not_retried(self):
        self.automation.enabled = False
        self.fire()
        self.assert_due_in(self.automation, 24 * HOUR)

    def test_an_alert_check_keeps_its_interval(self):
        self.automation.schedule_type = "alert"
        self.automation.interval_minutes = 120
        self.fire()
        self.assert_due_in(self.automation, 120 * 60)


class TestRunStateSurvivesARestart(_ClockCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(prefix="nwdash-c6-")
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name)
        self.automations_file = self.data_dir / "automations.json"
        for target, name, value in (
            (emailer, "DATA_DIR", self.data_dir),
            (emailer, "AUTOMATIONS_FILE", self.automations_file),
            (emailer, "WMI_CIPHER", object()),  # persistence is off without a key
            (snapshots, "EMAIL_CONFIG_FILE", self.data_dir / "email_config.json"),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def restart_with(self, automation, **saved):
        """Save the schedule as the last process left it, then restore it."""
        record = emailer._automation_to_dict(automation)
        record.update(saved)
        self.automations_file.write_text(json.dumps({automation.automation_id: record}), encoding="utf-8")
        models.ALERT_AUTOMATIONS.clear()
        self.assertEqual(emailer.restore_automations_from_disk(), 1)
        return models.ALERT_AUTOMATIONS[automation.automation_id]

    def test_the_run_state_is_saved(self):
        automation = _automation(next_run_at=1_900_000_000.0, last_run=1_800_000_000.0, last_result="Sent")
        models.ALERT_AUTOMATIONS[automation.automation_id] = automation
        emailer.persist_automations()
        record = json.loads(self.automations_file.read_text(encoding="utf-8"))[automation.automation_id]
        self.assertEqual(
            (record.get("next_run_at"), record.get("last_run"), record.get("last_result")),
            (1_900_000_000.0, 1_800_000_000.0, "Sent"),
        )

    def test_last_run_and_result_come_back(self):
        restored = self.restart_with(_automation(last_run=1_800_000_000.0, last_result="Sent daily report"))
        self.assertEqual((restored.last_run, restored.last_result), (1_800_000_000.0, "Sent daily report"))

    def test_a_restart_over_the_send_time_sends_todays_report_now(self):
        self.at(8, 30)
        restored = self.restart_with(_automation(last_signature="daily:2026-10-27:24h|Last 24 Hours"))
        self.assertLessEqual(restored.next_run_at, time.time())

    def test_todays_report_already_sent_is_not_sent_again(self):
        self.at(8, 30)
        restored = self.restart_with(_automation(last_signature="daily:2026-10-28:24h|Last 24 Hours"))
        self.assert_due_in(restored, 23 * HOUR + 30 * 60)

    def test_before_the_send_time_it_waits_for_it(self):
        self.at(7, 0)
        restored = self.restart_with(_automation(last_signature="daily:2026-10-26:24h|Last 24 Hours"))
        self.assert_due_in(restored, HOUR)

    def test_a_schedule_armed_after_the_send_time_starts_tomorrow(self):
        self.at(10, 0)
        armed_at = datetime(2026, 10, 28, 9, 0).astimezone().timestamp()
        restored = self.restart_with(_automation(created_at=armed_at))
        self.assert_due_in(restored, 22 * HOUR)

    def test_an_alert_keeps_its_next_check(self):
        due = time.time() + 600
        restored = self.restart_with(_automation("alert"), next_run_at=due)
        self.assertEqual(restored.next_run_at, due)

    def test_an_alert_check_missed_while_down_runs_at_once(self):
        restored = self.restart_with(_automation("alert"), next_run_at=time.time() - 600)
        self.assertLessEqual(restored.next_run_at, time.time())


if __name__ == "__main__":
    unittest.main()
