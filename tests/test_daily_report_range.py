"""A scheduled daily report covers its own range, not the dashboard's last one.

The defect (review C12): a scheduled report rebuilt the dashboard with whatever
range its session last used. Interactive range changes are written back to the
session (sessions.build_dashboard_from_session), so after someone looked at
"Last Month", or at a custom range, every "daily" email covered 30 days or the
same frozen dates. The report automation now stores an explicit rolling range,
the daily window unless chosen otherwise, and builds with it at fire time
without changing the range the dashboard (and its shared view) shows.
"""
import json
import tempfile
import time
import unittest
from http import HTTPStatus
from http.cookiejar import CookieJar
from pathlib import Path
from unittest import mock

from nwdash import emailer, sessions, snapshots
from nwdash.models import (
    AlertAutomation,
    ApiConfig,
    BadRequest,
    DashboardSession,
    _get_automation,
    _get_session,
    _pop_automation,
    _pop_session,
    _put_automation,
    _put_session,
)
from nwdash.secrets import WMI_CIPHER

SESSION_ID = "c12" + "0" * 29
NEEDS_CIPHER = unittest.skipUnless(WMI_CIPHER, "persisting automations needs the optional cryptography package")


def _session_config(report_range="30d", start="", end=""):
    return ApiConfig(
        rest_api_host="nw.example.com", rest_api_port=9090,
        backup_server_host="nw.example.com", backup_server_port=9001,
        username="administrator", password="", api_mode="nwui", api_version="v3",
        report_range=report_range, custom_start_date=start, custom_end_date=end,
        use_wmi_health=False, wmi_username="", wmi_password="",
        timeout_seconds=30, verify_tls=True, use_authc_header=False,
    )


def _daily_automation(automation_id="c12-range:0000c12a"):
    return AlertAutomation(
        automation_id=automation_id, session_id=SESSION_ID,
        smtp_host="smtp.example.com", smtp_port=25, smtp_username="", encrypted_smtp_password="",
        smtp_from="dashboard@example.com", recipients=["daily-range@example.com"],
        smtp_security="none", interval_minutes=60, trigger="critical",
        schedule_type="daily_report", report_time="08:00", created_at=1.0,
    )


class TestScheduledReportBuildsItsOwnRange(unittest.TestCase):
    """Fires run_alert_automation through the real build_dashboard_from_session;
    only the NetWorker collector and the SMTP send are replaced."""

    def setUp(self):
        self.built = []  # (report_range, custom_start_date, custom_end_date) of every build
        self.sent = 0

        def _build(config, **_kwargs):
            self.built.append((config.report_range, config.custom_start_date, config.custom_end_date))
            return HTTPStatus.OK, {
                "ok": True,
                "summary": {"range": config.report_range, "rangeLabel": config.report_range, "health": "ok"},
                "sources": {},
                "tables": {"jobs": [], "failedJobs": [], "alerts": []},
            }

        def _send(*_args, **_kwargs):
            self.sent += 1
            return {"stage": "sent", "host": "smtp.example.com", "port": 25}

        for target, name, value in (
            (sessions, "build_dashboard_nwui", _build),
            (sessions, "persist_sessions", lambda: None),
            (emailer, "scheduled_dashboard_email_payload", lambda _dashboard: ("report", "<p>report</p>", {})),
            (emailer, "load_ui_theme", lambda: "default"),
            (emailer, "send_smtp_email", _send),
            (emailer, "persist_automations", lambda: None),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.dict(emailer._LAST_SENT_BY_IDENTITY, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.automation = _daily_automation()
        _put_automation(self.automation.automation_id, self.automation)
        self.addCleanup(_pop_automation, self.automation.automation_id)

    def open_session(self, config):
        now = time.time()
        _put_session(SESSION_ID, DashboardSession(
            config=config, cookie_jar=CookieJar(), auth_headers={},
            encrypted_networker_password="", encrypted_wmi_password="",
            created_at=now, last_used=now,
        ))
        self.addCleanup(_pop_session, SESSION_ID)

    def fire(self):
        emailer.run_alert_automation(self.automation.automation_id)
        self.assertEqual(self.sent, 1, f"last result: {self.automation.last_result}")

    def test_daily_report_ignores_the_range_the_dashboard_last_used(self):
        self.open_session(_session_config("30d"))  # someone looked at "Last Month"
        self.fire()
        self.assertEqual(self.built, [("24h", "", "")])

    def test_a_frozen_custom_range_is_not_reused(self):
        self.open_session(_session_config("custom", "01-10-2026", "07-10-2026"))
        self.fire()
        self.assertEqual(self.built, [("24h", "", "")])

    def test_the_automation_range_is_what_gets_built(self):
        self.open_session(_session_config("24h"))
        self.automation.report_range = "7d"
        self.fire()
        self.assertEqual(self.built, [("7d", "", "")])

    def test_the_dashboard_keeps_its_own_range(self):
        # The shared/TV view refreshes on the session's range; a scheduled
        # report must not reset it.
        self.open_session(_session_config("custom", "01-10-2026", "07-10-2026"))
        self.fire()
        config = _get_session(SESSION_ID).config
        self.assertEqual(
            (config.report_range, config.custom_start_date, config.custom_end_date),
            ("custom", "01-10-2026", "07-10-2026"),
        )

    def test_an_interactive_range_change_is_still_remembered(self):
        self.open_session(_session_config("24h"))
        status, _body = sessions.build_dashboard_from_session(SESSION_ID, "30d")
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(_get_session(SESSION_ID).config.report_range, "30d")


def _start_form(**overrides):
    payload = {
        "action": "start", "sessionId": SESSION_ID,
        "smtpHost": "smtp.example.com", "smtpPort": "25", "smtpSecurity": "none",
        "smtpUsername": "", "smtpPassword": "", "smtpFrom": "dashboard@example.com",
        "smtpTo": "daily-range@example.com", "scheduleType": "daily_report", "reportTime": "08:00",
    }
    payload.update(overrides)
    return payload


class TestReportRangeIsStoredOnTheAutomation(unittest.TestCase):
    """Arming, the email config, profiles and automations.json all carry it."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="nwdash-range-")
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name)
        for target, name, value in (
            (snapshots, "DATA_DIR", self.data_dir),
            (snapshots, "EMAIL_CONFIG_FILE", self.data_dir / "email_config.json"),
            (snapshots, "EMAIL_PROFILES_FILE", self.data_dir / "email_profiles.json"),
            (emailer, "persist_automations", lambda: None),
            (emailer, "connection_snapshot_for_session", lambda _sid: {}),
            (emailer, "latest_session_record", lambda: None),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def arm(self, **overrides):
        status, body = emailer.handle_alert_automation(_start_form(**overrides))
        self.assertEqual(status, HTTPStatus.OK)
        self.addCleanup(_pop_automation, body["automationId"])
        return _get_automation(body["automationId"])

    def test_a_new_schedule_defaults_to_the_daily_window(self):
        self.assertEqual(self.arm().report_range, "24h")

    def test_a_chosen_rolling_range_is_kept(self):
        self.assertEqual(self.arm(reportRange="7d").report_range, "7d")
        self.assertEqual(snapshots.load_email_config()["types"]["daily_report"]["report_range"], "7d")
        self.assertEqual(snapshots.email_config_public()["dailyReport"]["reportRange"], "7d")

    def test_a_custom_or_unknown_range_is_refused(self):
        for value in ("custom", "1y"):
            with self.subTest(value=value), self.assertRaises(BadRequest):
                emailer.handle_alert_automation(_start_form(reportRange=value))

    def test_config_sync_applies_the_saved_range(self):
        automation = _daily_automation("c12-range:0000sync")
        cfg = {
            "smtp": {"host": "smtp.example.com", "port": 25, "security": "none", "from": "dashboard@example.com"},
            "types": {"daily_report": {"recipients": ["daily-range@example.com"], "report_time": "08:00",
                                       "theme": "default", "report_range": "30d"}},
        }
        with mock.patch.object(emailer, "schedule_alert_automation"):
            self.assertTrue(emailer._sync_automation_from_email_config(automation, cfg))
        self.assertEqual(automation.report_range, "30d")

    def test_a_profile_arms_with_its_saved_range(self):
        snapshots.save_email_profile("weekly", _start_form(reportRange="7d"))
        profile = snapshots.get_email_profile_raw("weekly")
        self.assertEqual(profile["reportRange"], "7d")
        automation = emailer._arm_profile_automation("weekly", profile, SESSION_ID)
        self.addCleanup(_pop_automation, automation.automation_id)
        self.assertEqual(automation.report_range, "7d")

    @NEEDS_CIPHER
    def test_the_range_survives_a_restart_and_legacy_records_get_the_daily_window(self):
        stored = _daily_automation("c12-range:00stored")
        stored.report_range = "7d"
        legacy = emailer._automation_to_dict(_daily_automation("c12-range:00legacy"))
        legacy.pop("report_range", None)  # saved before the field existed
        legacy["recipients"] = ["legacy-range@example.com"]
        legacy["profile_name"] = "legacy"  # outside the config-driven singleton
        records = {
            stored.automation_id: dict(emailer._automation_to_dict(stored), profile_name="stored"),
            legacy["automation_id"]: legacy,
        }
        automations_file = self.data_dir / "automations.json"
        automations_file.write_text(json.dumps(records), encoding="utf-8")
        for automation_id in records:
            self.addCleanup(_pop_automation, automation_id)
        with mock.patch.object(emailer, "AUTOMATIONS_FILE", automations_file):
            self.assertEqual(emailer.restore_automations_from_disk(), 2)
        self.assertEqual(_get_automation(stored.automation_id).report_range, "7d")
        self.assertEqual(_get_automation(legacy["automation_id"]).report_range, "24h")


if __name__ == "__main__":
    unittest.main()
