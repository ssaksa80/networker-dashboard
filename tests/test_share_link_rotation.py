"""Regenerate revokes the share link it replaces (review S12).

A new link already revoked the old one for the SAME dashboard session. But
the page's session changes under it (a reconnect, the automatic re-login), and
Regenerate then created a link for the new session while the old one stayed
valid for its 24 hours. The page had overwritten its only copy of the old
token, so only the admin console could still revoke it.

Boots the real app from a temp copy (tests/e2e_server.py). The prelude
stands in for two dashboard sessions, which would otherwise need a NetWorker
login.
"""
import json
import unittest

from e2e_server import AppServer

SESSIONS_PRELUDE = """
import nwdash.server as _server
_server._session_exists = lambda session_id: session_id in ("session-before", "session-after")
"""


class TestRegenerateRevokesTheLinkItReplaces(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = AppServer(prelude=SESSIONS_PRELUDE).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def create(self, session_id, replaces=None):
        body = {"action": "create", "sessionId": session_id}
        if replaces is not None:
            body["replaces"] = replaces
        status, raw, _headers = self.server.request("POST", "/api/share", body)
        self.assertEqual(status, 200, raw)
        return json.loads(raw)["token"]

    def view_status(self, token):
        return self.server.request("GET", f"/api/view/{token}")[0]

    def test_a_link_from_an_earlier_session_is_revoked(self):
        before = self.create("session-before")
        after = self.create("session-after", replaces=before)
        self.assertEqual(self.view_status(before), 410)
        self.assertEqual(self.view_status(after), 200)

    def test_without_replaces_other_sessions_links_are_kept(self):
        """Two dashboards may each have their own link."""
        first = self.create("session-before")
        second = self.create("session-after")
        self.assertEqual(self.view_status(first), 200)
        self.assertEqual(self.view_status(second), 200)

    def test_an_unknown_or_used_token_in_replaces_is_harmless(self):
        token = self.create("session-before", replaces="0" * 32)
        self.assertEqual(self.view_status(token), 200)


if __name__ == "__main__":
    unittest.main()
