"""Only the gateway's own login check answers 401, and it says so (review F2).

The dashboard's fetch wrapper reloaded the page on any /api 401. A NetWorker
server refusing its credentials and an expired dashboard session answered 401
too, so their errors never showed, and a page restored onto an expired session
reloaded each time its server-health timer fired.

Now the gateway's 401 carries X-NWDash-Login: required (the only 401 the
wrapper reloads on), an expired session answers 409 session_expired, and a
NetWorker 401 or 403 answers 502 networker_auth_failed. The client half is in
tests/test_ui_logic.py.
"""
import json
import unittest
from http import HTTPStatus

from e2e_server import AppServer

from nwdash.server import LOGIN_REQUIRED_HEADER, upstream_auth_failure
from nwdash.ui import dashboard_html

PASSWORD = "f2-regression-pass"

# Runs in the server process: the NetWorker login refuses every credential,
# with 403 for the user "no-access" and 401 for anyone else.
REFUSING_NETWORKER = """
import nwdash.nwui as _nwui
from nwdash.models import RestApiError

def _refuse(config, opener):
    status = 403 if config.username == "no-access" else 401
    raise RestApiError(status, "NWUI login failed. Check username/password and account access.")

_nwui.nwui_login = _refuse
"""


class TestUpstreamAuthFailure(unittest.TestCase):
    def test_other_statuses_pass_through(self):
        body = {"ok": False, "error": "x"}
        for status in (200, 400, 409, 502):
            with self.subTest(status=status):
                self.assertEqual(upstream_auth_failure(status, body), (status, body))

    def test_networker_401_and_403_become_502_with_a_code(self):
        for upstream in (401, 403):
            with self.subTest(upstream=upstream):
                status, body = upstream_auth_failure(upstream, {"ok": False, "error": "Login rejected.", "sources": {"a": 1}})
                self.assertEqual(status, HTTPStatus.BAD_GATEWAY)
                self.assertEqual(body["code"], "networker_auth_failed")
                self.assertEqual(body["upstreamStatus"], upstream)
                self.assertEqual(body["error"], f"The NetWorker server refused the credentials (HTTP {upstream}). Login rejected.")
                self.assertEqual(body["sources"], {"a": 1})

    def test_a_failure_without_a_message_still_explains(self):
        _, body = upstream_auth_failure(401, {"ok": False})
        self.assertIn("Check the user name, password and account access.", body["error"])


class TestDashboardReadsTheMarker(unittest.TestCase):
    def test_fetch_wrapper_checks_the_header_the_server_sends(self):
        self.assertIn(f'resp.headers.get("{LOGIN_REQUIRED_HEADER}") === "required"', dashboard_html())


class TestWhoAnswers401(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = AppServer(password=PASSWORD, prelude=REFUSING_NETWORKER).start()
        cls.cookie, cls.token = cls.server.login()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def _post(self, path, body):
        return self.server.request("POST", path, body, headers={"X-CSRF-Token": self.token}, cookie=self.cookie)

    def test_no_login_is_a_marked_401(self):
        for method, path, body in (("GET", "/api/status", None), ("POST", "/api/dashboard", {"sessionId": "x"})):
            with self.subTest(method=method, path=path):
                status, raw, headers = self.server.request(method, path, body)
                self.assertEqual(status, 401)
                self.assertEqual(headers.get(LOGIN_REQUIRED_HEADER), "required")
                self.assertEqual(json.loads(raw)["code"], "login_required")

    def test_expired_dashboard_session_is_409(self):
        for path in ("/api/dashboard", "/api/export", "/api/server-health"):
            with self.subTest(path=path):
                status, raw, headers = self._post(path, {"sessionId": "f" * 32})
                self.assertEqual(status, 409)
                self.assertIsNone(headers.get(LOGIN_REQUIRED_HEADER))
                body = json.loads(raw)
                self.assertEqual(body["code"], "session_expired")
                self.assertEqual(body["error"], "Dashboard session expired. Reconnect with the password.")

    def test_networker_refusing_the_credentials_is_502(self):
        for username, upstream in (("operator", 401), ("no-access", 403)):
            for path in ("/api/dashboard", "/api/export"):
                with self.subTest(path=path, upstream=upstream):
                    status, raw, headers = self._post(path, {
                        "restApiHost": "networker.example.test",
                        "username": username,
                        "password": "not-a-real-password",
                        "apiMode": "nwui",
                    })
                    self.assertEqual(status, 502, raw)
                    self.assertIsNone(headers.get(LOGIN_REQUIRED_HEADER))
                    body = json.loads(raw)
                    self.assertEqual(body["code"], "networker_auth_failed")
                    self.assertEqual(body["upstreamStatus"], upstream)
                    self.assertIn("refused the credentials", body["error"])
                    self.assertIn("Check username/password", body["error"])


if __name__ == "__main__":
    unittest.main()
