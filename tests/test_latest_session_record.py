"""latest_session_record(): prefer the newest LIVE session, else the newest
record persisted in sessions.json, else None.

Rewritten from a pytest-style module (bare test functions + monkeypatch) that
`python -m unittest discover` silently skipped and ruff rejected — as unittest
these actually run in CI.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nwdash import config, sessions


class _Cfg:
    rest_api_host = "10.0.0.9"
    rest_api_port = 9090
    backup_server_host = "bkp"
    backup_server_port = 9090
    username = "administrator"
    api_mode = "nwui"
    api_version = "auto"
    report_range = "24h"
    custom_start_date = ""
    custom_end_date = ""
    use_wmi_health = False
    wmi_username = ""
    timeout_seconds = 30
    verify_tls = False
    use_authc_header = False


class _S:
    def __init__(self, last_used):
        self.config = _Cfg()
        self.encrypted_networker_password = "enc"
        self.encrypted_wmi_password = ""
        self.created_at = 1.0
        self.last_used = last_used


class TestLatestSessionRecord(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="nwdash-lsr-")
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)

    def test_live_sessions_win_and_newest_picked(self):
        with mock.patch.object(
            sessions, "_session_items_snapshot",
            lambda: [("old", _S(10.0)), ("new", _S(99.0))],
        ):
            rec = sessions.latest_session_record()
        self.assertEqual(rec["session_id"], "new")
        self.assertEqual(rec["config"]["rest_api_host"], "10.0.0.9")

    def test_disk_fallback_when_no_live(self):
        persisted = self.tmp_path / "sessions.json"
        persisted.write_text(json.dumps({
            "a": {"session_id": "a", "last_used": 5.0, "encrypted_networker_password": "e",
                  "config": {"rest_api_host": "h1", "username": "u"}},
            "b": {"session_id": "b", "last_used": 50.0, "encrypted_networker_password": "e",
                  "config": {"rest_api_host": "h2", "username": "u"}},
        }), encoding="utf-8")
        with mock.patch.object(sessions, "_session_items_snapshot", lambda: []), \
                mock.patch.object(config, "SESSION_PERSISTENCE_FILE", persisted):
            rec = sessions.latest_session_record()
        self.assertEqual(rec["session_id"], "b")
        self.assertEqual(rec["config"]["rest_api_host"], "h2")

    def test_none_when_nothing_anywhere(self):
        with mock.patch.object(sessions, "_session_items_snapshot", lambda: []), \
                mock.patch.object(config, "SESSION_PERSISTENCE_FILE", self.tmp_path / "none.json"):
            self.assertIsNone(sessions.latest_session_record())


if __name__ == "__main__":
    unittest.main()
