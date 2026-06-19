import unittest
import networker_dashboard as nd


class AccountButtonContrastTests(unittest.TestCase):
    def test_topbar_collapse_toggle_has_explicit_light_style(self):
        html = nd.HTML_PAGE
        self.assertIn(".topbar .collapse-toggle", html)
        idx = html.index(".topbar .collapse-toggle")
        block = html[idx:idx + 220]
        self.assertIn("color: #ffffff", block)

    def test_collapse_toggle_uses_correct_surface_variable(self):
        html = nd.HTML_PAGE
        self.assertNotIn("var(--surface2)", html)


import time
from networker_dashboard import AlertAutomation


def _make_automation(session_id="sess1", schedule_type="alert", enabled=True):
    return AlertAutomation(
        automation_id="auto1",
        session_id=session_id,
        smtp_host="10.0.0.1",
        smtp_port=25,
        smtp_username="",
        encrypted_smtp_password=nd.encrypt_process_secret(""),
        smtp_from="dash@example.com",
        recipients=["ops@example.com"],
        smtp_security="none",
        interval_minutes=60,
        trigger="critical",
        schedule_type=schedule_type,
        report_time="08:00",
        created_at=time.time(),
        theme="default",
        enabled=enabled,
    )


class EnableDisableTests(unittest.TestCase):
    def setUp(self):
        nd.ALERT_AUTOMATIONS.clear()

    def tearDown(self):
        nd.ALERT_AUTOMATIONS.clear()

    def test_set_enabled_flips_flag(self):
        auto = _make_automation()
        nd._put_automation(auto.automation_id, auto)
        status, body = nd.handle_alert_automation(
            {"action": "set_enabled", "sessionId": "sess1",
             "automationId": "auto1", "enabled": False})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertFalse(body["enabled"])
        self.assertFalse(nd._get_automation("auto1").enabled)

    def test_set_enabled_rejects_other_session(self):
        auto = _make_automation(session_id="other")
        nd._put_automation(auto.automation_id, auto)
        nd.handle_alert_automation(
            {"action": "set_enabled", "sessionId": "sess1",
             "automationId": "auto1", "enabled": False})
        self.assertTrue(nd._get_automation("auto1").enabled)

    def test_list_returns_enabled(self):
        auto = _make_automation()
        nd._put_automation(auto.automation_id, auto)
        status, body = nd.handle_alert_automation(
            {"action": "list", "sessionId": "sess1"})
        self.assertEqual(body["schedules"][0]["enabled"], True)

    def test_disabled_automation_does_not_send(self):
        sent = []
        orig = nd.send_smtp_email
        nd.send_smtp_email = lambda *a, **k: sent.append(a) or {}
        orig_sched = nd.schedule_alert_automation
        nd.schedule_alert_automation = lambda a: None
        try:
            auto = _make_automation(enabled=False)
            nd._put_automation(auto.automation_id, auto)
            nd.run_alert_automation("auto1")
            self.assertEqual(sent, [])
            self.assertEqual(nd._get_automation("auto1").last_result, "Paused")
        finally:
            nd.send_smtp_email = orig
            nd.schedule_alert_automation = orig_sched


class QuietHoursTests(unittest.TestCase):
    def test_within_window_same_day(self):
        self.assertTrue(nd.within_quiet_hours("22:00", "23:00", hhmm="22:30"))
        self.assertFalse(nd.within_quiet_hours("22:00", "23:00", hhmm="21:30"))

    def test_within_window_wraps_midnight(self):
        self.assertTrue(nd.within_quiet_hours("22:00", "06:00", hhmm="02:00"))
        self.assertTrue(nd.within_quiet_hours("22:00", "06:00", hhmm="23:30"))
        self.assertFalse(nd.within_quiet_hours("22:00", "06:00", hhmm="12:00"))

    def test_empty_window_never_quiet(self):
        self.assertFalse(nd.within_quiet_hours("", "", hhmm="03:00"))

    def test_parse_smtp_settings_reads_quiet(self):
        s = nd.parse_smtp_settings({
            "smtpHost": "10.0.0.1", "smtpFrom": "a@b.com", "smtpTo": "c@d.com",
            "quietStart": "22:00", "quietEnd": "06:00"})
        self.assertEqual(s["quiet_start"], "22:00")
        self.assertEqual(s["quiet_end"], "06:00")


class DigestAndSeverityTests(unittest.TestCase):
    def test_parse_reads_digest(self):
        s = nd.parse_smtp_settings({
            "smtpHost": "10.0.0.1", "smtpFrom": "a@b.com", "smtpTo": "c@d.com",
            "digest": True})
        self.assertTrue(s["digest"])

    def test_parse_digest_defaults_false(self):
        s = nd.parse_smtp_settings({
            "smtpHost": "10.0.0.1", "smtpFrom": "a@b.com", "smtpTo": "c@d.com"})
        self.assertFalse(s["digest"])

    def test_alert_subject_for_digest(self):
        self.assertEqual(
            nd.alert_email_subject("critical", digest=True),
            "NetWorker dashboard alert: Critical")

    def test_alert_subject_for_single(self):
        self.assertEqual(
            nd.alert_email_subject("critical", digest=False),
            "NetWorker dashboard alert (single): Critical")

    def test_should_send_alert_respects_trigger(self):
        self.assertTrue(nd.should_send_alert("all", "warning"))
        self.assertTrue(nd.should_send_alert("critical", "critical"))
        self.assertFalse(nd.should_send_alert("critical", "warning"))

    def test_list_returns_trigger_and_digest(self):
        nd.ALERT_AUTOMATIONS.clear()
        auto = _make_automation()
        auto.digest = True
        nd._put_automation(auto.automation_id, auto)
        row = nd.handle_alert_automation(
            {"action": "list", "sessionId": "sess1"})[1]["schedules"][0]
        self.assertEqual(row["trigger"], "critical")
        self.assertTrue(row["digest"])
        nd.ALERT_AUTOMATIONS.clear()


if __name__ == "__main__":
    unittest.main()
