"""Every POST gets an answer, whatever its headers carry (review S11).

The defect: do_POST's gates (origin, sign-in, CSRF) ran before its try. A
valid cookie sent with a non-ASCII X-CSRF-Token made hmac.compare_digest raise
TypeError there, the exception escaped to socketserver, and the connection
was dropped with no response. Any other error in a gate did the same.

Boots the real app from a temp copy (tests/e2e_server.py). The prelude makes
one gate raise on demand, standing in for errors nobody has found yet.
"""
import http.client
import json
import unittest
import urllib.error

from e2e_server import AppServer

from nwdash.config import COOKIE_NAME

PASSWORD = "post-answers-test-pw"
GATE_ERROR_PRELUDE = """
import nwdash.server as _server
_same_origin = _server.DashboardHandler._same_origin
def _same_origin_or_raise(self):
    if self.headers.get("X-Test-Gate-Error"):
        raise RuntimeError("gate error injected by the test")
    return _same_origin(self)
_server.DashboardHandler._same_origin = _same_origin_or_raise
"""


class TestEveryPostIsAnswered(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = AppServer(password=PASSWORD, prelude=GATE_ERROR_PRELUDE).start()
        cls.cookie, cls.csrf = cls.server.login()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def post(self, headers, cookie=""):
        try:
            status, body, _headers = self.server.request(
                "POST", "/api/ui-theme", {"theme": "default"}, headers=headers, cookie=cookie)
        except (ConnectionError, http.client.HTTPException, urllib.error.URLError) as exc:
            self.fail(f"the server dropped the connection without an answer: {exc!r}")
        return status, json.loads(body or b"{}")

    def test_a_non_ascii_csrf_token_is_refused(self):
        status, body = self.post({"X-CSRF-Token": "tökén"}, cookie=self.cookie)
        self.assertEqual(status, 403)
        self.assertIn("CSRF", body["error"])

    def test_a_non_ascii_cookie_is_treated_as_signed_out(self):
        status, body = self.post({"X-CSRF-Token": self.csrf}, cookie=f"{COOKIE_NAME}=päyload.sig")
        self.assertEqual(status, 401)
        self.assertEqual(body["code"], "login_required")

    def test_an_error_in_a_gate_is_a_500(self):
        status, body = self.post({"X-Test-Gate-Error": "1"})
        self.assertEqual(status, 500)
        self.assertIn("Internal error (ref", body["error"])

    def test_the_valid_token_still_works(self):
        status, _body = self.post({"X-CSRF-Token": self.csrf}, cookie=self.cookie)
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
