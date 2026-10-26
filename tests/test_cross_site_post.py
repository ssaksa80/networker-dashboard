"""Cross-site POSTs are refused with or without a password, and a password-less
dashboard starts only on loopback unless explicitly allowed (review S4).

The defect: the CSRF token is enforced only when a password is set, and POST
bodies were parsed whatever their Content-Type. With no password, any web page
the operator visited could POST text/plain JSON to the API (rewrite SMTP,
delete profiles, probe hosts), and nothing stopped a network-exposed instance
from running without a password.

Requests go through http.client: urllib adds a form Content-Type by itself.
"""
import http.client
import json
import unittest

from e2e_server import AppServer

from nwdash.main import parse_args, unauthenticated_exposure_refusal

THEME = json.dumps({"theme": "default"}).encode("utf-8")
JSON = {"Content-Type": "application/json"}


class TestCrossSitePostsWithoutAuth(unittest.TestCase):
    """No password: the CSRF token gate is off, so these checks are the guard."""

    @classmethod
    def setUpClass(cls):
        cls.server = AppServer().start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def _connect(self):
        conn = http.client.HTTPSConnection("127.0.0.1", self.server.port, context=self.server.ctx, timeout=20)
        self.addCleanup(conn.close)
        return conn

    def _post(self, headers, path="/api/ui-theme"):
        conn = self._connect()
        conn.request("POST", path, body=THEME, headers=headers)
        resp = conn.getresponse()
        resp.read()
        return resp.status

    def test_json_from_a_non_browser_client_is_accepted(self):
        self.assertEqual(self._post(JSON), 200)
        self.assertEqual(self._post({"Content-Type": "application/json; charset=utf-8"}), 200)

    def test_same_origin_browser_post_is_accepted(self):
        origin = f"https://127.0.0.1:{self.server.port}"
        self.assertEqual(self._post({**JSON, "Origin": origin, "Sec-Fetch-Site": "same-origin"}), 200)

    def test_bodies_a_page_can_send_without_preflight_are_refused(self):
        for content_type in (
            "text/plain",
            "text/plain;charset=UTF-8",
            "application/x-www-form-urlencoded",
            "multipart/form-data; boundary=x",
            None,
        ):
            with self.subTest(content_type=content_type):
                headers = {"Content-Type": content_type} if content_type else {}
                self.assertEqual(self._post(headers), 415)

    def test_cross_origin_posts_are_refused(self):
        for headers in (
            {"Origin": "https://evil.example"},
            {"Origin": "null"},
            {"Origin": f"http://127.0.0.1:{self.server.port}"},
            {"Sec-Fetch-Site": "cross-site"},
            {"Sec-Fetch-Site": "same-site"},
        ):
            with self.subTest(headers=headers):
                self.assertEqual(self._post({**JSON, **headers}), 403)

    def test_login_and_logout_are_gated_too(self):
        for path in ("/api/login", "/api/logout"):
            with self.subTest(path=path):
                self.assertEqual(self._post({"Content-Type": "text/plain"}, path=path), 415)
                self.assertEqual(self._post({**JSON, "Origin": "https://evil.example"}, path=path), 403)

    def test_a_refused_post_leaves_the_connection_usable(self):
        conn = self._connect()
        conn.request("POST", "/api/ui-theme", body=THEME, headers={"Content-Type": "text/plain"})
        resp = conn.getresponse()
        resp.read()
        self.assertEqual(resp.status, 415)
        conn.request("GET", "/api/health")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        resp.read()


class TestUnauthenticatedExposureGuard(unittest.TestCase):
    def test_network_bind_without_a_password_refuses_to_start(self):
        server = AppServer()
        self.addCleanup(server.stop)
        server.launch(bind="0.0.0.0")  # refused before anything binds
        code = server.wait_exit(60)
        self.assertEqual(code, 2, server.output())
        self.assertIn("Refusing to listen on 0.0.0.0 with no dashboard password", server.output())

    def test_refusal_rules(self):
        self.assertTrue(unauthenticated_exposure_refusal("0.0.0.0", False, False))
        self.assertTrue(unauthenticated_exposure_refusal("192.0.2.10", False, False))
        self.assertEqual(unauthenticated_exposure_refusal("0.0.0.0", True, False), "")
        self.assertEqual(unauthenticated_exposure_refusal("0.0.0.0", False, True), "")
        for loopback in ("127.0.0.1", "localhost", "::1"):
            with self.subTest(bind=loopback):
                self.assertEqual(unauthenticated_exposure_refusal(loopback, False, False), "")

    def test_override_flag_is_off_by_default(self):
        self.assertFalse(parse_args([]).allow_unauthenticated)
        self.assertTrue(parse_args(["--allow-unauthenticated"]).allow_unauthenticated)


if __name__ == "__main__":
    unittest.main()
