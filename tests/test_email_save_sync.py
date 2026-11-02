"""A saved email configuration reaches the schedule already armed from it.

The defect: action=save only wrote email_config.json, so a running config-driven
schedule kept its old range, time, recipients and SMTP settings until the next
restart or until it was scheduled again, although the admin Email tab says
"Schedules use these settings". A save now runs the same reconcile as a restart.

Saving must still never start sending: it arms nothing, and a schedule armed
from a named profile follows its profile, not this file.
"""
import json
import tempfile
import time
import unittest
from dataclasses import asdict
from http import HTTPStatus
from pathlib import Path
from unittest import mock

from nwdash import emailer, models, snapshots
from nwdash.models import AlertAutomation, _put_automation
from nwdash.secrets import WMI_CIPHER, decrypt_process_secret, encrypt_process_secret

SECRET = "stored-smtp-secret"
SAVED_SMTP = {
    "host": "smtp.example.com",
    "port": 587,
    "security": "starttls",
    "username": "alerts@example.com",
    "from": "dashboard@example.com",
}
SAVED_TYPES = {
    "alert": {"recipients": ["alert@example.com"], "trigger": "critical", "interval_minutes": 60},
    "daily_report": {
        "recipients": ["daily@example.com"], "report_time": "08:00", "report_range": "7d", "theme": "default",
    },
}
NEEDS_CIPHER = unittest.skipUnless(WMI_CIPHER, "stored SMTP secrets need the optional cryptography package")


def _form(schedule_type="daily_report", **overrides):
    """A save of the saved settings, for either type, unless overridden."""
    payload = {
        "action": "save",
        "smtpHost": SAVED_SMTP["host"],
        "smtpPort": str(SAVED_SMTP["port"]),
        "smtpSecurity": SAVED_SMTP["security"],
        "smtpUsername": SAVED_SMTP["username"],
        "smtpPassword": "",
        "smtpFrom": SAVED_SMTP["from"],
        "smtpTo": ", ".join(SAVED_TYPES[schedule_type]["recipients"]),
        "scheduleType": schedule_type,
    }
    if schedule_type == "daily_report":
        payload.update(reportTime="08:00", reportRange="7d", theme="default")
    else:
        payload.update(trigger="critical", intervalMinutes="60")
    payload.update(overrides)
    return payload


class _EmailSaveCase(unittest.TestCase):
    """Real save and reconcile over a throwaway email_config.json, automations.json
    and schedule registry. Only the SMTP send and the scheduler's firing are replaced."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="nwdash-save-sync-")
        self.addCleanup(tmp.cleanup)
        data_dir = Path(tmp.name)
        self.automations_file = data_dir / "automations.json"
        self.sent = []
        self.fired = []

        def _send(settings, _subject, _body, smtp_password, *_args, **_kwargs):
            self.sent.append((settings.smtp_host, smtp_password))
            return {"stage": "sent", "host": settings.smtp_host, "port": settings.smtp_port}

        for target, name, value in (
            (snapshots, "DATA_DIR", data_dir),
            (snapshots, "EMAIL_CONFIG_FILE", data_dir / "email_config.json"),
            (emailer, "DATA_DIR", data_dir),
            (emailer, "AUTOMATIONS_FILE", self.automations_file),
            (emailer, "send_smtp_email", _send),
            (emailer, "_fire_automation_async", self.fired.append),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.dict(models.ALERT_AUTOMATIONS, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def save_config(self, password=SECRET, types=None, **smtp_overrides):
        smtp = dict(SAVED_SMTP, encrypted_password=encrypt_process_secret(password), **smtp_overrides)
        cfg = {"smtp": smtp, "types": SAVED_TYPES if types is None else types}
        snapshots.EMAIL_CONFIG_FILE.write_text(json.dumps(cfg), encoding="utf-8")

    def arm(self, schedule_type="daily_report", **overrides):
        """A schedule armed from the saved config (profile_name stays empty)."""
        saved = SAVED_TYPES[schedule_type]
        fields = {
            "automation_id": f"save-sync:{schedule_type}", "session_id": "s" * 32,
            "smtp_host": SAVED_SMTP["host"], "smtp_port": SAVED_SMTP["port"],
            "smtp_username": SAVED_SMTP["username"], "encrypted_smtp_password": encrypt_process_secret(SECRET),
            "smtp_from": SAVED_SMTP["from"], "recipients": list(saved["recipients"]),
            "smtp_security": SAVED_SMTP["security"], "interval_minutes": saved.get("interval_minutes", 60),
            "trigger": saved.get("trigger", "critical"), "schedule_type": schedule_type,
            "report_time": saved.get("report_time", "08:00"), "report_range": saved.get("report_range", "24h"),
            "created_at": 1.0,
        }
        fields.update(overrides)
        automation = AlertAutomation(**fields)
        _put_automation(automation.automation_id, automation)
        return automation

    def save(self, schedule_type="daily_report", **overrides):
        status, body = emailer.handle_alert_automation(_form(schedule_type, **overrides))
        self.assertEqual(status, HTTPStatus.OK)
        return body

    @staticmethod
    def password_of(automation):
        return decrypt_process_secret(automation.encrypted_smtp_password)


class TestSaveReachesTheArmedSchedule(_EmailSaveCase):
    def test_a_daily_report_takes_its_range_time_and_recipients(self):
        self.save_config()
        schedule = self.arm()
        body = self.save(reportRange="30d", reportTime="09:30", smtpTo="new@example.com")
        self.assertEqual(schedule.report_range, "30d")
        self.assertEqual(schedule.report_time, "09:30")
        self.assertEqual(schedule.recipients, ["new@example.com"])
        self.assertEqual(body["schedulesUpdated"], 1)
        self.assertIn("1 schedule(s) updated", body["message"])
        self.assertNotIn("Schedule selected report", body["message"])

    def test_an_alert_takes_its_trigger_and_interval(self):
        self.save_config()
        schedule = self.arm("alert")
        self.save("alert", trigger="warning", intervalMinutes="15")
        self.assertEqual((schedule.trigger, schedule.interval_minutes), ("warning", 15))

    def test_smtp_settings_reach_it(self):
        self.save_config()
        schedule = self.arm()
        self.save(smtpHost="mail.example.com", smtpPort="2525", smtpSecurity="ssl", smtpUsername="", smtpFrom="b@example.com")
        self.assertEqual(
            (schedule.smtp_host, schedule.smtp_port, schedule.smtp_security, schedule.smtp_username, schedule.smtp_from),
            ("mail.example.com", 2525, "ssl", "", "b@example.com"),
        )

    @NEEDS_CIPHER
    def test_automations_json_follows_at_once(self):
        # Seen in the browser: the tab saved 30d, this file stayed at 7d.
        self.save_config()
        schedule = self.arm()
        self.save(reportRange="30d")
        records = json.loads(self.automations_file.read_text(encoding="utf-8"))
        self.assertEqual(records[schedule.automation_id]["report_range"], "30d")

    def test_saving_one_type_moves_the_other_types_smtp_and_nothing_else(self):
        # The SMTP server is shared; the dialog only posts the selected type.
        self.save_config(types=dict(SAVED_TYPES, alert=dict(SAVED_TYPES["alert"], trigger="warning", interval_minutes=15)))
        alert = self.arm("alert", trigger="warning", interval_minutes=15)
        self.save("daily_report", smtpHost="mail.example.com", smtpTo="new@example.com")
        self.assertEqual(alert.smtp_host, "mail.example.com")
        self.assertEqual((alert.recipients, alert.trigger, alert.interval_minutes), (["alert@example.com"], "warning", 15))

    def test_a_type_with_no_saved_entry_is_kept(self):
        self.save_config(types={})
        alert = self.arm("alert")
        self.save("daily_report", smtpHost="mail.example.com")
        self.assertEqual(alert.smtp_host, SAVED_SMTP["host"])

    def test_fields_the_config_does_not_store_are_kept(self):
        self.save_config()
        schedule = self.arm("alert", quiet_start="22:00", quiet_end="06:00", digest=False)
        self.save("alert", trigger="warning")
        self.assertEqual((schedule.quiet_start, schedule.quiet_end, schedule.digest), ("22:00", "06:00", False))

    def test_the_last_signature_is_kept(self):
        # New recipients must not make a standing alert count as a new one.
        self.save_config()
        schedule = self.arm("alert", last_signature="critical|line", last_run=123.0)
        self.save("alert", smtpTo="new@example.com")
        self.assertEqual((schedule.last_signature, schedule.last_run), ("critical|line", 123.0))

    def test_the_same_settings_update_nothing(self):
        self.save_config()
        self.arm()
        with mock.patch.object(emailer, "persist_automations") as persist:
            body = self.save()
        persist.assert_not_called()
        self.assertEqual(body["schedulesUpdated"], 0)
        self.assertNotIn("updated", body["message"])
        self.assertNotIn("Schedule selected report", body["message"])

    def test_two_schedules_of_a_type_never_both_keep_sending(self):
        # Syncing both would give them one identity and send every report twice.
        self.save_config()
        self.arm(automation_id="save-sync:older", created_at=1.0)
        newest = self.arm(automation_id="save-sync:newest", created_at=2.0)
        self.save(reportRange="30d")
        self.assertEqual(list(models.ALERT_AUTOMATIONS), [newest.automation_id])
        self.assertEqual(newest.report_range, "30d")


class TestSaveNeverStartsSending(_EmailSaveCase):
    def test_nothing_armed_stays_nothing_armed(self):
        body = self.save()
        self.assertEqual(models.ALERT_AUTOMATIONS, {})
        self.assertFalse(self.automations_file.exists())
        self.assertEqual(self.sent, [])
        self.assertEqual(body["schedulesUpdated"], 0)
        self.assertEqual(body["warning"], "")
        self.assertIn("Schedule selected report", body["message"])

    def test_the_config_is_still_saved(self):
        self.save(reportRange="30d")
        self.assertEqual(snapshots.load_email_config()["types"]["daily_report"]["report_range"], "30d")

    def test_a_paused_schedule_stays_paused(self):
        self.save_config()
        schedule = self.arm(enabled=False)
        self.save(reportRange="30d", reportTime="09:30")
        self.assertFalse(schedule.enabled)
        self.assertEqual(schedule.report_range, "30d")

    def test_only_the_scheduler_ever_fires_it(self):
        self.save_config()
        self.arm(next_run_at=time.time() + 3600)
        self.save(reportRange="30d", reportTime="09:30")
        self.assertEqual(self.sent, [])
        self.assertEqual(self.fired, [])

    def test_saving_a_type_with_only_the_other_armed_does_not_claim_it_is_scheduled(self):
        self.save_config()
        self.arm("alert")
        body = self.save("daily_report")
        self.assertEqual(list(models.ALERT_AUTOMATIONS), ["save-sync:alert"])
        self.assertIn("Schedule selected report", body["message"])


class TestTheNextRunOnlyMovesWithTheCadence(_EmailSaveCase):
    def assert_due_in(self, schedule, seconds):
        self.assertAlmostEqual(schedule.next_run_at, time.time() + seconds, delta=5)

    def test_a_new_report_time_moves_it(self):
        self.save_config()
        schedule = self.arm(next_run_at=time.time() + 60)
        self.save(reportTime="09:30")
        self.assert_due_in(schedule, emailer.seconds_until_daily_report("09:30"))

    def test_a_new_interval_moves_it(self):
        self.save_config()
        schedule = self.arm("alert", next_run_at=time.time() + 60)
        self.save("alert", intervalMinutes="15")
        self.assert_due_in(schedule, 15 * 60)

    def test_other_changes_leave_it_alone(self):
        # Recomputing would push an alert's next check a whole interval away on
        # every save.
        self.save_config()
        due = time.time() + 60
        schedule = self.arm("alert", next_run_at=due)
        self.save("alert", smtpTo="new@example.com", smtpHost="mail.example.com")
        self.assertEqual(schedule.recipients, ["new@example.com"])
        self.assertEqual(schedule.next_run_at, due)


class TestProfileSchedulesAreLeftAlone(_EmailSaveCase):
    def test_a_profile_schedule_is_not_touched(self):
        self.save_config()
        schedule = self.arm(profile_name="ops", recipients=["profile@example.com"], report_range="24h")
        before = asdict(schedule)
        body = self.save(reportRange="30d", smtpHost="mail.example.com", smtpTo="new@example.com")
        self.assertEqual(asdict(schedule), before)
        self.assertEqual(body["schedulesUpdated"], 0)
        self.assertIn("Schedule selected report", body["message"])

    def test_only_the_config_driven_schedule_beside_it_moves(self):
        self.save_config()
        profile = self.arm(automation_id="save-sync:profile", profile_name="ops", created_at=9.0)
        config_driven = self.arm(automation_id="save-sync:form", created_at=1.0)
        before = asdict(profile)
        body = self.save(reportRange="30d")
        self.assertEqual(config_driven.report_range, "30d")
        self.assertEqual(asdict(profile), before)
        self.assertEqual(body["schedulesUpdated"], 1)
        self.assertEqual(len(models.ALERT_AUTOMATIONS), 2)

    def test_a_profile_schedule_without_a_password_is_not_warned_about(self):
        self.save_config(password="")
        self.arm(profile_name="ops", encrypted_smtp_password="")
        self.assertEqual(self.save()["warning"], "")


@NEEDS_CIPHER
class TestThePasswordFollowsTheServer(_EmailSaveCase):
    def test_a_new_server_and_no_password_leaves_it_without_one_and_warns(self):
        # The old server's password must never go to the new one.
        self.save_config()
        schedule = self.arm()
        body = self.save(smtpHost="mail.example.com")
        self.assertEqual(schedule.smtp_host, "mail.example.com")
        self.assertEqual(self.password_of(schedule), "")
        self.assertIn("Type the password", body["warning"])
        self.assertIn(body["warning"], body["message"])

    def test_every_bound_field_counts_as_a_new_server(self):
        for field, value in (
            ("smtpHost", "mail.example.com"),
            ("smtpPort", "2525"),
            ("smtpUsername", "someone-else@example.com"),
            ("smtpSecurity", "ssl"),
        ):
            with self.subTest(field=field):
                self.save_config()
                schedule = self.arm()
                self.save(**{field: value})
                self.assertEqual(self.password_of(schedule), "")

    def test_a_new_server_with_a_typed_password_gets_that_password(self):
        self.save_config()
        schedule = self.arm()
        body = self.save(smtpHost="mail.example.com", smtpPassword="new-secret")
        self.assertEqual(self.password_of(schedule), "new-secret")
        self.assertEqual(body["warning"], "")

    def test_the_same_server_keeps_it(self):
        self.save_config(password="")
        schedule = self.arm()
        body = self.save(smtpTo="new@example.com")
        self.assertEqual(self.password_of(schedule), SECRET)
        self.assertEqual(body["warning"], "")

    def test_a_rotated_password_reaches_it(self):
        # It kept the old one, even across a restart, and failed to log in.
        self.save_config()
        schedule = self.arm()
        body = self.save(smtpPassword="rotated-secret")
        self.assertEqual(self.password_of(schedule), "rotated-secret")
        self.assertEqual(body["schedulesUpdated"], 1)

    def test_a_rotated_password_reaches_the_other_type(self):
        self.save_config()
        alert = self.arm("alert")
        self.save("daily_report", smtpPassword="rotated-secret")
        self.assertEqual(self.password_of(alert), "rotated-secret")

    def test_it_takes_the_config_password_when_its_own_is_unreadable(self):
        self.save_config()
        schedule = self.arm(encrypted_smtp_password="unreadable")
        self.save()
        self.assertEqual(self.password_of(schedule), SECRET)

    def test_a_config_password_saved_for_another_server_is_not_taken(self):
        # The file moved on after `cfg` was read: its password is for its server.
        self.save_config(host="other.example.com")
        schedule = self.arm(smtp_host="old.example.com")
        cfg = {"smtp": dict(SAVED_SMTP, encrypted_password=""), "types": SAVED_TYPES}
        self.assertTrue(emailer._sync_automation_from_email_config(schedule, cfg))
        self.assertEqual(schedule.smtp_host, SAVED_SMTP["host"])
        self.assertEqual(self.password_of(schedule), "")

    def test_no_username_means_nothing_to_warn_about(self):
        # send_smtp_email never logs in without one: an open relay still works.
        self.save_config()
        self.arm()
        self.assertEqual(self.save(smtpHost="relay.example.com", smtpUsername="")["warning"], "")


@NEEDS_CIPHER
class TestASendInFlightKeepsItsServer(_EmailSaveCase):
    """A save re-aims the live schedule while a scheduler thread is sending. The
    password it read must not end up going to the server the save just named."""

    def setUp(self):
        super().setUp()
        self.during_send = []

        def _send(settings, _subject, _body, smtp_password, *_args, **_kwargs):
            self.save(smtpHost="mail.example.com", smtpPassword="new-secret")
            self.during_send.append((settings.smtp_host, smtp_password))
            return {"stage": "sent", "host": settings.smtp_host, "port": settings.smtp_port}

        dashboard = {"summary": {"range": "7d", "rangeLabel": "Last Week"}}
        for name, value in (
            ("send_smtp_email", _send),
            ("_ensure_automation_session", lambda _automation: True),
            ("build_dashboard_from_session", lambda *_args, **_kwargs: (HTTPStatus.OK, dict(dashboard))),
            ("dashboard_backup_source_available", lambda _dashboard: True),
            ("scheduled_dashboard_email_payload", lambda _dashboard: ("report", "<p>report</p>", {})),
            ("load_ui_theme", lambda: "default"),
            ("dashboard_alert_lines", lambda _dashboard: ("critical", ["a critical alert"])),
        ):
            patcher = mock.patch.object(emailer, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.dict(emailer._LAST_SENT_BY_IDENTITY, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def assert_send_kept_its_server(self, schedule):
        emailer.run_alert_automation(schedule.automation_id)
        self.assertEqual(self.during_send, [(SAVED_SMTP["host"], SECRET)], schedule.last_result)
        # ... and the schedule itself moved, for its next send.
        self.assertEqual(schedule.smtp_host, "mail.example.com")
        self.assertEqual(self.password_of(schedule), "new-secret")

    def test_a_daily_report(self):
        self.save_config()
        self.assert_send_kept_its_server(self.arm())

    def test_an_alert(self):
        self.save_config()
        self.assert_send_kept_its_server(self.arm("alert"))


class TestSchedulingMovesTheOtherTypesServer(_EmailSaveCase):
    """action=start saves the same shared SMTP block, and only replaces its own
    type's schedule."""

    def setUp(self):
        super().setUp()
        for name, value in (
            ("connection_snapshot_for_session", lambda _sid: {}),
            ("latest_session_record", lambda: None),
        ):
            patcher = mock.patch.object(emailer, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def start(self, **overrides):
        status, body = emailer.handle_alert_automation(
            _form("daily_report", action="start", sessionId="s" * 32, smtpHost="mail.example.com", **overrides)
        )
        self.assertEqual(status, HTTPStatus.OK)
        return body

    def test_the_other_type_follows_and_keeps_its_own_settings(self):
        self.save_config()
        alert = self.arm("alert")
        self.start(smtpPassword="new-secret")
        self.assertEqual(alert.smtp_host, "mail.example.com")
        self.assertEqual(alert.recipients, ["alert@example.com"])
        self.assertEqual(len(models.ALERT_AUTOMATIONS), 2)

    @NEEDS_CIPHER
    def test_its_password_goes_with_the_server(self):
        self.save_config()
        alert = self.arm("alert")
        self.start(smtpPassword="new-secret")
        self.assertEqual(self.password_of(alert), "new-secret")

    def test_the_new_schedule_keeps_what_it_was_given(self):
        self.save_config()
        body = self.start(smtpPassword="new-secret", reportRange="30d", smtpTo="new@example.com")
        schedule = models.ALERT_AUTOMATIONS[body["automationId"]]
        self.assertEqual((schedule.report_range, schedule.recipients), ("30d", ["new@example.com"]))

    def test_a_profile_schedule_is_not_touched(self):
        self.save_config()
        profile = self.arm("alert", profile_name="ops")
        before = asdict(profile)
        self.start(smtpPassword="new-secret")
        self.assertEqual(asdict(profile), before)


if __name__ == "__main__":
    unittest.main()
