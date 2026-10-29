"""The one flaw the original engine had: a schedule without a session snapshot
went silently inert. These tests pin the self-healing behavior that fixes it.

Rewritten from a pytest-style module (bare test functions + monkeypatch) that
`python -m unittest discover` never collected — as unittest these run in CI.
"""
import unittest
from unittest import mock

from nwdash import emailer
from nwdash.models import AlertAutomation


def _rec(host="10.0.0.9"):
    return {"session_id": "s-live", "last_used": 9.0, "encrypted_networker_password": "enc",
            "config": {"rest_api_host": host, "username": "administrator"}}


def _automation(connection):
    return AlertAutomation(
        automation_id="auto1", session_id="dead-session", connection=connection,
        smtp_host="h", smtp_port=25, smtp_username="", encrypted_smtp_password="",
        smtp_from="r@example.com", recipients=["a@example.com"], smtp_security="none",
        interval_minutes=60, trigger="critical", schedule_type="alert",
        report_time="08:00", created_at=1.0, theme="default",
    )


class TestFireTimeSelfHeal(unittest.TestCase):
    """_ensure_automation_session(): a dead session is recreated from the
    automation's snapshot, or from the newest session record when that
    snapshot is missing or unreadable."""

    def setUp(self):
        for name, value in (
            ("_session_exists", lambda sid: False),
            ("persist_automations", lambda: None),
        ):
            patcher = mock.patch.object(emailer, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_adopts_latest_record_when_snapshot_empty(self):
        recreated = {}
        with mock.patch.object(emailer, "latest_session_record", lambda: _rec()), \
             mock.patch.object(emailer, "decrypt_process_secret", lambda s: "pw" if s else ""), \
             mock.patch.object(emailer, "recreate_session_from_snapshot",
                               lambda sid, snap: recreated.update(host=snap["config"]["rest_api_host"]) or True):
            a = _automation(connection={})
            self.assertIs(emailer._ensure_automation_session(a), True)
        self.assertEqual(recreated["host"], "10.0.0.9")
        self.assertEqual(a.connection.get("config", {}).get("rest_api_host"), "10.0.0.9")  # adopted + stored

    def test_adopts_when_snapshot_undecryptable(self):
        # The old snapshot decrypts to "" (dead key); the adopted record decrypts fine.
        with mock.patch.object(emailer, "latest_session_record", lambda: _rec("adopted-host")), \
             mock.patch.object(emailer, "decrypt_process_secret", lambda s: "pw" if s == "enc" else ""), \
             mock.patch.object(emailer, "recreate_session_from_snapshot", lambda sid, snap: True):
            a = _automation(connection={"encrypted_networker_password": "dead", "config": {"rest_api_host": "old"}})
            self.assertIs(emailer._ensure_automation_session(a), True)
        self.assertEqual(a.connection["config"]["rest_api_host"], "adopted-host")

    def test_still_waits_gracefully_when_nothing_anywhere(self):
        with mock.patch.object(emailer, "latest_session_record", lambda: None):
            a = _automation(connection={})
            self.assertIs(emailer._ensure_automation_session(a), False)
        self.assertIn("waiting", a.last_result.lower())


class TestArmTimeResolveConnection(unittest.TestCase):
    """_resolve_connection(): a schedule is never armed connectionless while
    any session record exists."""

    def test_start_adopts_when_no_live_session(self):
        with mock.patch.object(emailer, "latest_session_record", lambda: _rec()):
            resolved = emailer._resolve_connection({}, None)
        self.assertEqual(resolved["config"]["rest_api_host"], "10.0.0.9")

    def test_prefers_live_snapshot(self):
        live = {"session_id": "live", "config": {"rest_api_host": "live-host"}}
        with mock.patch.object(emailer, "latest_session_record", lambda: _rec("fallback")):
            resolved = emailer._resolve_connection(live, None)
        self.assertEqual(resolved["config"]["rest_api_host"], "live-host")


if __name__ == "__main__":
    unittest.main()
