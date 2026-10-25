"""A stored SMTP password is only ever sent to the server it was saved for.

The defect (review S1): the email `test`/`start` actions fell back to the
replaced schedule's SMTP password, or to the one saved in email_config.json,
whatever smtpHost the request named. A caller could aim a test email at its own
server and receive the stored password in the SMTP AUTH exchange. Saving the
email settings or an email profile with a new server and a blank password kept
the stored password too, so it would then "match" the new server.

The binding mirrors connection profiles (profiles._resolve_profile_password):
host, port, username and security must all match.
"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from nwdash import emailer, snapshots
from nwdash.models import AlertAutomation, BadRequest, _pop_automation, _put_automation
from nwdash.secrets import WMI_CIPHER, decrypt_process_secret, encrypt_process_secret, encrypt_profile_secret

SECRET = "stored-smtp-secret"
SAVED_SMTP = {
    "host": "smtp.example.com",
    "port": 587,
    "security": "starttls",
    "username": "alerts@example.com",
    "from": "dashboard@example.com",
}
NEEDS_CIPHER = unittest.skipUnless(WMI_CIPHER, "stored SMTP secrets need the optional cryptography package")


def _form(**overrides):
    """An email form payload naming the saved server unless overridden."""
    payload = {
        "action": "test",
        "sessionId": "s" * 32,
        "smtpHost": "smtp.example.com",
        "smtpPort": "587",
        "smtpSecurity": "starttls",
        "smtpUsername": "alerts@example.com",
        "smtpPassword": "",
        "smtpFrom": "dashboard@example.com",
        "smtpTo": "ops@example.com",
        "scheduleType": "alert",
    }
    payload.update(overrides)
    return payload


class EmailStoreMixin:
    """Throwaway email_config.json / email_profiles.json, and a recording SMTP send."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="nwdash-smtp-")
        self.addCleanup(tmp.cleanup)
        data_dir = Path(tmp.name)
        for attr, value in (
            ("DATA_DIR", data_dir),
            ("EMAIL_CONFIG_FILE", data_dir / "email_config.json"),
            ("EMAIL_PROFILES_FILE", data_dir / "email_profiles.json"),
        ):
            patcher = mock.patch.object(snapshots, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.sent = []  # (host, port, username, password) of every attempted send

        def _record(automation, _subject, _body, smtp_password, *_args, **_kwargs):
            self.sent.append((automation.smtp_host, automation.smtp_port, automation.smtp_username, smtp_password))
            return {"stage": "sent"}

        patcher = mock.patch.object(emailer, "send_smtp_email", _record)
        patcher.start()
        self.addCleanup(patcher.stop)

    def save_config(self, password=SECRET, **smtp_overrides):
        smtp = dict(SAVED_SMTP, encrypted_password=encrypt_process_secret(password), **smtp_overrides)
        snapshots.EMAIL_CONFIG_FILE.write_text(json.dumps({"smtp": smtp, "types": {}}), encoding="utf-8")

    def passwords_sent(self):
        return [password for *_target, password in self.sent]


@NEEDS_CIPHER
class TestSavedConfigPasswordIsBound(EmailStoreMixin, unittest.TestCase):
    def test_matching_server_gets_the_saved_password(self):
        self.save_config()
        status, _body = emailer.handle_alert_automation(_form())
        self.assertEqual(status, 200)
        self.assertEqual(self.sent, [("smtp.example.com", 587, "alerts@example.com", SECRET)])

    def test_another_host_is_refused_and_nothing_is_sent(self):
        self.save_config()
        with self.assertRaises(BadRequest) as ctx:
            emailer.handle_alert_automation(_form(smtpHost="attacker.example"))
        self.assertIn("Enter the SMTP password", str(ctx.exception))
        self.assertNotIn(SECRET, str(ctx.exception))
        self.assertEqual(self.sent, [])

    def test_every_bound_field_is_enforced(self):
        self.save_config()
        for field, value in (
            ("smtpHost", "attacker.example"),
            ("smtpPort", "2525"),
            ("smtpUsername", "someone-else@example.com"),
            ("smtpSecurity", "none"),
        ):
            with self.subTest(field=field), self.assertRaises(BadRequest):
                emailer.handle_alert_automation(_form(**{field: value}))
        self.assertEqual(self.sent, [])

    def test_host_case_does_not_matter(self):
        self.save_config()
        emailer.handle_alert_automation(_form(smtpHost="SMTP.Example.COM"))
        self.assertEqual(self.passwords_sent(), [SECRET])

    def test_a_typed_password_may_target_any_server(self):
        self.save_config()
        emailer.handle_alert_automation(_form(smtpHost="other.example", smtpPassword="typed-password"))
        self.assertEqual(self.sent, [("other.example", 587, "alerts@example.com", "typed-password")])

    def test_no_username_means_no_password_and_no_refusal(self):
        # Without a username send_smtp_email never logs in, so there is nothing
        # to refuse — an open relay elsewhere still works.
        self.save_config()
        emailer.handle_alert_automation(_form(smtpHost="relay.example", smtpUsername=""))
        self.assertEqual(self.passwords_sent(), [""])

    def test_saving_a_new_server_without_a_password_drops_the_stored_one(self):
        self.save_config()
        snapshots.save_email_config_from_payload(_form(smtpHost="attacker.example"))
        smtp = snapshots.load_email_config()["smtp"]
        self.assertEqual(smtp["host"], "attacker.example")
        self.assertEqual(smtp["encrypted_password"], "")
        # ... so the follow-up test email has nothing to send there.
        emailer.handle_alert_automation(_form(smtpHost="attacker.example"))
        self.assertEqual(self.passwords_sent(), [""])

    def test_resaving_the_same_server_keeps_the_stored_password(self):
        self.save_config()
        snapshots.save_email_config_from_payload(_form(smtpHost="SMTP.example.com", smtpTo="new@example.com"))
        smtp = snapshots.load_email_config()["smtp"]
        self.assertEqual(decrypt_process_secret(smtp["encrypted_password"]), SECRET)


@NEEDS_CIPHER
class TestScheduleAndProfilePasswordsAreBound(EmailStoreMixin, unittest.TestCase):
    def _put_schedule(self, host="smtp.example.com"):
        automation = AlertAutomation(
            automation_id="s1-binding:schedule", session_id="s" * 32,
            smtp_host=host, smtp_port=587, smtp_username="alerts@example.com",
            encrypted_smtp_password=encrypt_process_secret(SECRET),
            smtp_from="dashboard@example.com", recipients=["ops@example.com"], smtp_security="starttls",
            interval_minutes=60, trigger="critical", schedule_type="alert", report_time="08:00",
            created_at=time.time(),
        )
        _put_automation(automation.automation_id, automation)
        self.addCleanup(_pop_automation, automation.automation_id)
        return automation

    def test_replaced_schedule_password_is_refused_for_another_host(self):
        schedule = self._put_schedule()
        with self.assertRaises(BadRequest):
            emailer.handle_alert_automation(_form(automationId=schedule.automation_id, smtpHost="attacker.example"))
        self.assertEqual(self.sent, [])

    def test_replaced_schedule_password_is_reused_for_its_own_server(self):
        schedule = self._put_schedule()
        emailer.handle_alert_automation(_form(automationId=schedule.automation_id))
        self.assertEqual(self.passwords_sent(), [SECRET])

    def _save_profile(self):
        record = {
            "smtpHost": "smtp.example.com", "smtpPort": 587, "smtpSecurity": "starttls",
            "smtpUsername": "alerts@example.com", "smtpFrom": "dashboard@example.com",
            "smtpTo": "ops@example.com", "scheduleType": "alert",
            "_enc_smtpPassword": encrypt_profile_secret(SECRET, name="ops", field="smtpPassword"),
        }
        snapshots.EMAIL_PROFILES_FILE.write_text(json.dumps({"ops": record}), encoding="utf-8")

    def test_profile_sentinel_is_refused_for_another_host(self):
        self._save_profile()
        with self.assertRaises(BadRequest):
            emailer.handle_alert_automation(
                _form(emailProfileName="ops", smtpPassword="(saved)", smtpHost="attacker.example")
            )
        self.assertEqual(self.sent, [])

    def test_profile_sentinel_is_substituted_for_its_own_server(self):
        self._save_profile()
        emailer.handle_alert_automation(_form(emailProfileName="ops", smtpPassword="(saved)"))
        self.assertEqual(self.passwords_sent(), [SECRET])

    def test_resaving_a_profile_with_a_new_server_drops_its_password(self):
        self._save_profile()
        masked = snapshots.save_email_profile("ops", _form(smtpHost="attacker.example", smtpPassword="(saved)"))
        self.assertEqual(masked["ops"]["smtpPassword"], "")
        self.assertEqual(snapshots.get_email_profile_raw("ops")["_enc_smtpPassword"], "")

    def test_resaving_a_profile_with_the_same_server_keeps_its_password(self):
        self._save_profile()
        masked = snapshots.save_email_profile("ops", _form(smtpPassword="(saved)", smtpTo="new@example.com"))
        self.assertEqual(masked["ops"]["smtpPassword"], "(saved)")

    def test_config_sync_does_not_carry_a_schedule_password_to_a_new_server(self):
        # Boot-time reconcile re-aims config-driven schedules at the config's
        # server; the schedule's own password must not go with it.
        schedule = self._put_schedule()
        cfg = {
            "smtp": dict(SAVED_SMTP, host="attacker.example", encrypted_password=""),
            "types": {"alert": {"recipients": ["ops@example.com"], "trigger": "critical", "interval_minutes": 60}},
        }
        with mock.patch.object(emailer, "schedule_alert_automation"):
            self.assertTrue(emailer._sync_automation_from_email_config(schedule, cfg))
        self.assertEqual(schedule.smtp_host, "attacker.example")
        self.assertEqual(decrypt_process_secret(schedule.encrypted_smtp_password), "")

    def test_config_sync_keeps_the_password_when_the_server_is_unchanged(self):
        schedule = self._put_schedule()
        cfg = {
            "smtp": dict(SAVED_SMTP, encrypted_password=""),
            "types": {"alert": {"recipients": ["new@example.com"], "trigger": "critical", "interval_minutes": 60}},
        }
        with mock.patch.object(emailer, "schedule_alert_automation"):
            emailer._sync_automation_from_email_config(schedule, cfg)
        self.assertEqual(decrypt_process_secret(schedule.encrypted_smtp_password), SECRET)


if __name__ == "__main__":
    unittest.main()
