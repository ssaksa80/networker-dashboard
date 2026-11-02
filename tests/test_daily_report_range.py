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

from nwdash import emailer, models, sessions, snapshots
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


def _dashboard(report_range, backup_data=True):
    return {
        "ok": True,
        "generatedAt": "01-11-2026 08:00:00 UTC",
        "summary": {"range": report_range, "rangeLabel": report_range, "health": "ok"},
        "sources": {"jobs": {"ok": backup_data, "path": "/jobs", "error": "" if backup_data else "gateway timeout"}},
        "tables": {"jobs": [], "failedJobs": [], "alerts": []},
    }


class _ReportFallbackCase(unittest.TestCase):
    """The real build_dashboard_from_session and cache lookups, over a collector
    that answers without backup job data, a temporary last-good file and an
    empty shared view. Only the NetWorker call and the SMTP send are replaced."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="nwdash-fallback-")
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name)
        self.backup_data = False
        self.built = []
        self.emailed = []  # the dashboard of every email

        def _build(config, **_kwargs):
            self.built.append(config.report_range)
            return HTTPStatus.OK, _dashboard(config.report_range, self.backup_data)

        def _payload(dashboard):
            self.emailed.append(dashboard)
            return "report", "<p>report</p>", {}

        for target, name, value in (
            (models, "DATA_DIR", self.data_dir),
            (models, "LAST_GOOD_DASHBOARD_FILE", self.data_dir / "last_good_dashboard.json"),
            (snapshots, "DATA_DIR", self.data_dir),
            (snapshots, "EMAIL_CONFIG_FILE", self.data_dir / "email_config.json"),
            (sessions, "build_dashboard_nwui", _build),
            (sessions, "persist_sessions", lambda: None),
            (sessions, "reauthenticate_dashboard_session", lambda _session, _config: False),
            (emailer, "scheduled_dashboard_email_payload", _payload),
            (emailer, "load_ui_theme", lambda: "default"),
            (emailer, "send_smtp_email", lambda *_args, **_kwargs: {"stage": "sent", "host": "smtp", "port": 25}),
            (emailer, "persist_automations", lambda: None),
            (emailer, "connection_snapshot_for_session", lambda _sid: {}),
            (emailer, "latest_session_record", lambda: None),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.dict(emailer._LAST_SENT_BY_IDENTITY, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.dict(models.SHARED_DASHBOARD_STATE, {"sessionId": "", "dashboard": None})
        patcher.start()
        self.addCleanup(patcher.stop)

        now = time.time()
        _put_session(SESSION_ID, DashboardSession(
            config=_session_config("24h"), cookie_jar=CookieJar(), auth_headers={},
            encrypted_networker_password="", encrypted_wmi_password="",
            created_at=now, last_used=now,
        ))
        self.addCleanup(_pop_session, SESSION_ID)
        self.automation = _daily_automation()
        _put_automation(self.automation.automation_id, self.automation)
        self.addCleanup(_pop_automation, self.automation.automation_id)

    @staticmethod
    def share(report_range):
        """The shared view's in-memory last good dashboard."""
        models.SHARED_DASHBOARD_STATE.update(sessionId=SESSION_ID, dashboard=_dashboard(report_range))

    @staticmethod
    def save_to_disk(report_range):
        """data/last_good_dashboard.json, all that is left after a restart."""
        models.persist_last_good_dashboard(SESSION_ID, _dashboard(report_range))

    def fire(self):
        emailer.run_alert_automation(self.automation.automation_id)


class TestScheduledReportCacheFallback(_ReportFallbackCase):
    """stale_dashboard_from_cache inside build_dashboard_from_session."""

    def assert_skipped_plainly(self, label):
        self.assertEqual(self.emailed, [])
        self.assertIn("Daily report skipped", self.automation.last_result)
        self.assertIn(f"no saved dashboard covers {label}", self.automation.last_result)
        self.assertIn("gateway timeout", self.automation.last_result)

    def test_the_shared_view_of_another_range_is_not_sent(self):
        self.share("30d")
        self.fire()
        self.assert_skipped_plainly("Last 24 Hours")

    def test_a_saved_dashboard_of_another_range_is_not_sent(self):
        self.save_to_disk("30d")
        self.fire()
        self.assert_skipped_plainly("Last 24 Hours")

    def test_a_custom_range_is_not_sent_for_a_rolling_one(self):
        self.automation.report_range = "7d"
        self.share("custom")
        self.save_to_disk("custom")
        self.fire()
        self.assert_skipped_plainly("Last Week")

    def test_a_cached_dashboard_of_its_own_range_is_sent_marked_stale(self):
        self.automation.report_range = "7d"
        self.share("7d")
        self.fire()
        self.assertEqual(self.built, ["7d"])
        self.assertEqual(len(self.emailed), 1, self.automation.last_result)
        report = self.emailed[0]
        self.assertEqual(report["summary"]["range"], "7d")
        self.assertTrue(report["stale"])
        self.assertIn("last successful dashboard snapshot", report["reportNotice"])

    def test_the_saved_dashboard_is_used_when_only_it_has_the_range(self):
        self.automation.report_range = "7d"
        self.share("30d")
        self.save_to_disk("7d")
        self.fire()
        self.assertEqual([report["summary"]["range"] for report in self.emailed], ["7d"])

    def test_the_shared_view_keeps_any_last_good_dashboard(self):
        self.share("30d")
        status, body = sessions.build_dashboard_from_session(SESSION_ID)
        self.assertEqual(status, HTTPStatus.OK)
        self.assertTrue(body["stale"])
        self.assertEqual(body["summary"]["range"], "30d")

    def test_an_interactive_range_change_keeps_any_last_good_dashboard(self):
        self.save_to_disk("30d")
        _status, body = sessions.build_dashboard_from_session(SESSION_ID, "7d")
        self.assertTrue(body["stale"])
        self.assertEqual(body["summary"]["range"], "30d")

    def test_an_alert_schedule_keeps_any_last_good_dashboard(self):
        # run_alert_automation builds an alert schedule on the session's range.
        self.share("30d")
        _status, body = sessions.build_dashboard_from_session(SESSION_ID, None, remember_range=False)
        self.assertTrue(body["stale"])
        self.assertEqual(body["summary"]["range"], "30d")


class TestScheduledReportEmailerFallback(_ReportFallbackCase):
    """shared_reliable_dashboard_for_session in run_alert_automation, reached
    when the build hands back a dashboard with no backup job data."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(
            emailer, "build_dashboard_from_session",
            lambda *_args, **_kwargs: (HTTPStatus.OK, _dashboard(self.automation.report_range, backup_data=False)),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_shared_view_of_another_range_is_not_sent(self):
        self.share("30d")
        self.fire()
        self.assertEqual(self.emailed, [])
        self.assertIn("no saved dashboard covers Last 24 Hours", self.automation.last_result)

    def test_the_shared_view_of_its_own_range_is_sent_with_a_notice(self):
        self.automation.report_range = "7d"
        self.share("7d")
        self.fire()
        self.assertEqual(len(self.emailed), 1, self.automation.last_result)
        self.assertEqual(self.emailed[0]["summary"]["range"], "7d")
        self.assertIn("last successful dashboard snapshot", self.emailed[0]["reportNotice"])
        self.assertIn("gateway timeout", self.emailed[0]["reportNotice"])


class TestTestEmailUsesTheReportRange(_ReportFallbackCase):
    """The dialog's Send test posts the dashboard on its screen."""

    def send_test(self, **overrides):
        status, body = emailer.handle_alert_automation(_start_form(action="test", **overrides))
        self.assertEqual(status, HTTPStatus.OK, body)

    def test_a_page_dashboard_of_another_range_is_not_used(self):
        self.backup_data = True
        self.send_test(reportRange="7d", dashboard=_dashboard("24h"))
        self.assertEqual(self.built, ["7d"])
        self.assertEqual(self.emailed[0]["summary"]["range"], "7d")

    def test_a_page_dashboard_of_the_same_range_is_used(self):
        self.send_test(reportRange="7d", dashboard=_dashboard("7d"))
        self.assertEqual(self.built, [])
        self.assertEqual(self.emailed[0]["summary"]["range"], "7d")

    def test_a_test_email_with_no_data_for_its_range_says_so(self):
        self.share("30d")
        self.send_test(reportRange="7d")
        report = self.emailed[0]
        self.assertEqual(report["summary"]["range"], "7d")
        self.assertIn("no saved dashboard covers Last Week", report["reportNotice"])


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

    def post(self, **overrides):
        status, body = emailer.handle_alert_automation(_start_form(**overrides))
        self.assertEqual(status, HTTPStatus.OK, body)
        return body

    def stored_range(self):
        return snapshots.load_email_config()["types"]["daily_report"]["report_range"]

    def test_saving_keeps_the_chosen_range(self):
        body = self.post(action="save", reportRange="30d")
        self.assertEqual(self.stored_range(), "30d")
        self.assertEqual(body["config"]["dailyReport"]["reportRange"], "30d")

    def test_saving_the_alert_settings_leaves_the_report_range_alone(self):
        # The admin Email tab saves the alert settings first, with no range.
        self.post(action="save", reportRange="7d")
        self.post(action="save", scheduleType="alert")
        self.assertEqual(self.stored_range(), "7d")
        self.assertEqual(snapshots.email_config_public()["dailyReport"]["reportRange"], "7d")

    def test_scheduling_answers_with_the_saved_config(self):
        # The dialog keeps this as its cache; without it, flipping the email
        # type refills the old range and the next save would store that.
        body = self.post(reportRange="30d")
        self.addCleanup(_pop_automation, body["automationId"])
        self.assertEqual(body["config"]["dailyReport"]["reportRange"], "30d")
        self.assertNotIn("encrypted_password", body["config"]["smtp"])

    def test_the_schedule_list_shows_the_range_for_editing(self):
        automation = self.arm(reportRange="7d")
        rows = {row["automationId"]: row for row in self.post(action="list")["schedules"]}
        self.assertEqual(rows[automation.automation_id]["reportRange"], "7d")

    def test_a_profile_round_trips_the_range_through_the_api(self):
        self.post(action="save-profile", profileName="weekly", reportRange="7d")
        self.assertEqual(self.post(action="get-profile", profileName="weekly")["profile"]["reportRange"], "7d")
        self.assertEqual(self.post(action="list-profiles")["profiles"]["weekly"]["reportRange"], "7d")
        body = self.post(action="toggle-profile", profileName="weekly", enabled=True)
        self.addCleanup(_pop_automation, body["automationId"])
        self.assertEqual(_get_automation(body["automationId"]).report_range, "7d")

    def test_resaving_a_profile_that_is_on_moves_its_schedule_to_the_new_range(self):
        self.post(action="save-profile", profileName="weekly", reportRange="7d")
        self.post(action="toggle-profile", profileName="weekly", enabled=True)
        self.post(action="save-profile", profileName="weekly", reportRange="30d")
        automation_id = self.post(action="list-profiles")["profiles"]["weekly"]["automationId"]
        self.addCleanup(_pop_automation, automation_id)
        self.assertEqual(_get_automation(automation_id).report_range, "30d")

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
