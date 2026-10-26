"""The admin console over HTTPS: /admin, its gate, the admin window, and that
admin changes keep the POST rules every other route follows (JSON only, same
origin, CSRF token) and land in the audit log.

Boots the real app from a temp copy (tests/e2e_server.py), with and without a
gateway password.
"""
import json
import time
import unittest

from e2e_server import AppServer

PASSWORD = "admin-console-test-pw"


def _json(result):
    status, body, _headers = result
    return status, json.loads(body or b"{}")


class TestAdminWithPassword(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = AppServer(password=PASSWORD).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def _signed_in(self, admin=False):
        status, body, headers = self.server.request("POST", "/api/login", {"password": PASSWORD, "admin": admin})
        self.assertEqual(status, 200, body)
        return headers.get("Set-Cookie", "").split(";", 1)[0], json.loads(body)["csrfToken"]

    def get(self, path, cookie=""):
        return _json(self.server.request("GET", path, cookie=cookie))

    def post(self, path, payload, cookie="", csrf=""):
        headers = {"X-CSRF-Token": csrf} if csrf else {}
        return _json(self.server.request("POST", path, payload, headers=headers, cookie=cookie))

    def test_page_is_served_with_its_own_sign_in(self):
        status, body, headers = self.server.request("GET", "/admin")
        self.assertEqual(status, 200)
        html = body.decode("utf-8")
        self.assertIn("NetWorker Admin Console", html)
        self.assertIn("/api/admin/state", html)
        self.assertNotRegex(html, r"__[A-Z_]+__")
        self.assertIn("frame-ancestors 'none'", headers.get("Content-Security-Policy", ""))

    def test_state_without_a_cookie_asks_for_sign_in(self):
        status, body = self.get("/api/admin/state")
        self.assertEqual(status, 200)
        self.assertEqual(body["access"], "login")
        self.assertTrue(body["passwordSet"])
        self.assertFalse(body["unlocked"])

    def test_admin_api_needs_sign_in(self):
        status, _body = self.get("/api/admin/overview")
        self.assertEqual(status, 401)

    def test_dashboard_sign_in_does_not_open_the_admin_window(self):
        cookie, csrf = self._signed_in()
        status, body = self.get("/api/admin/overview", cookie)
        self.assertEqual(status, 403)
        self.assertEqual(body["code"], "unlock_required")
        status, body = self.post("/api/admin/appearance", {"design": "aurora"}, cookie, csrf)
        self.assertEqual(status, 403)
        self.assertEqual(body["code"], "unlock_required")

    def test_unlock_with_the_password(self):
        cookie, csrf = self._signed_in()
        status, _body = self.post("/api/admin/unlock", {"password": "wrong"}, cookie, csrf)
        self.assertEqual(status, 401)
        status, body = self.post("/api/admin/unlock", {"password": PASSWORD}, cookie, csrf)
        self.assertEqual(status, 200)
        self.assertGreater(body["unlockedUntil"], 0)
        status, body = self.get("/api/admin/overview", cookie)
        self.assertEqual(status, 200)
        self.assertTrue(body["passwordSet"])
        status, body = self.get("/api/admin/state", cookie)
        self.assertTrue(body["unlocked"])
        status, _body = self.post("/api/admin/lock", {}, cookie, csrf)
        self.assertEqual(status, 200)
        status, body = self.get("/api/admin/overview", cookie)
        self.assertEqual(status, 403)

    def test_background_refresh_does_not_keep_the_window_open(self):
        # The Logs tab re-polls every 4 s; if each poll extended the window, a
        # console left on that tab would never lock.
        cookie, csrf = self._signed_in(admin=True)
        until = self.get("/api/admin/state", cookie)[1]["unlockedUntil"]
        time.sleep(1.2)  # the window is reported in whole seconds
        status, _body = self.get("/api/admin/logs?level=INFO", cookie)
        self.assertEqual(status, 200)
        self.assertEqual(self.get("/api/admin/state", cookie)[1]["unlockedUntil"], until)
        status, _b, _h = self.server.request("GET", "/api/admin/logs?level=INFO", headers={"X-NWDash-Activity": "1"}, cookie=cookie)
        self.assertEqual(status, 200)
        extended = self.get("/api/admin/state", cookie)[1]["unlockedUntil"]
        self.assertGreater(extended, until)
        time.sleep(1.2)
        self.assertEqual(self.post("/api/admin/appearance", {"motion": "subtle"}, cookie, csrf)[0], 200)
        self.assertGreater(self.get("/api/admin/state", cookie)[1]["unlockedUntil"], extended, "a change is activity")

    def test_admin_sign_in_opens_the_window(self):
        cookie, _csrf = self._signed_in(admin=True)
        status, body = self.get("/api/admin/sessions", cookie)
        self.assertEqual(status, 200)
        mine = [s for s in body["sessions"] if s["current"]]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["ip"], "127.0.0.1")
        self.assertTrue(mine[0]["adminOpen"])

    def test_admin_changes_need_the_csrf_token(self):
        cookie, _csrf = self._signed_in(admin=True)
        status, _body = self.post("/api/admin/appearance", {"design": "command"}, cookie)
        self.assertEqual(status, 403)

    def test_admin_changes_refuse_cross_site_and_non_json(self):
        cookie, csrf = self._signed_in(admin=True)
        status, _b, _h = self.server.request(
            "POST", "/api/admin/appearance", {"design": "command"},
            headers={"X-CSRF-Token": csrf, "Origin": "https://evil.example.test", "Sec-Fetch-Site": "cross-site"}, cookie=cookie)
        self.assertEqual(status, 403)
        status, _b, _h = self.server.request(
            "POST", "/api/admin/appearance", data=b'{"design":"command"}',
            headers={"X-CSRF-Token": csrf, "Content-Type": "text/plain"}, cookie=cookie)
        self.assertEqual(status, 415)

    def test_appearance_defaults_reach_every_viewer_and_the_audit(self):
        cookie, csrf = self._signed_in(admin=True)
        status, body = self.post("/api/admin/appearance", {"design": "dedb", "motion": "subtle", "sidebar": "float"}, cookie, csrf)
        self.assertEqual(status, 200)
        self.assertEqual(body["design"], "dedb")
        status, prefs = self.get("/api/ui-theme", cookie)
        self.assertEqual((prefs["design"], prefs["motion"], prefs["sidebar"]), ("dedb", "subtle", "float"))
        status, body = self.post("/api/admin/appearance", {"design": "neon"}, cookie, csrf)
        self.assertEqual(status, 400)
        status, audit = self.get("/api/admin/audit", cookie)
        self.assertEqual(status, 200)
        actions = [row["action"] for row in audit["entries"]]
        self.assertIn("Appearance changed", actions)
        self.assertIn("Signed in", actions)
        self.assertIn("Appearance changed", audit["actions"])

    def test_failed_sign_in_is_audited(self):
        self.server.request("POST", "/api/login", {"password": "not it"})
        cookie, _csrf = self._signed_in(admin=True)
        _status, audit = self.get("/api/admin/audit?action=Sign-in%20failed", cookie)
        self.assertTrue(audit["entries"])
        self.assertNotIn("not it", json.dumps(audit))

    def test_audit_date_must_be_a_date(self):
        cookie, _csrf = self._signed_in(admin=True)
        status, _body = self.get("/api/admin/audit?date=yesterday", cookie)
        self.assertEqual(status, 400)

    def test_read_only_sections_answer(self):
        cookie, _csrf = self._signed_in(admin=True)
        for path in ("/api/admin/logs?level=INFO", "/api/admin/tls", "/api/admin/security", "/api/admin/shares", "/api/admin/appearance"):
            status, body = self.get(path, cookie)
            self.assertEqual(status, 200, path)
            self.assertTrue(body.get("ok"), path)
        _status, tls = self.get("/api/admin/tls", cookie)
        self.assertTrue(tls["ok"])
        self.assertNotIn("PRIVATE KEY", json.dumps(tls))
        _status, security = self.get("/api/admin/security", cookie)
        self.assertTrue(security["passwordSet"])
        self.assertTrue(security["passwordFromStartup"])

    def test_self_test_runs(self):
        cookie, csrf = self._signed_in(admin=True)
        status, body = self.post("/api/admin/self-test", {}, cookie, csrf)
        self.assertEqual(status, 200)
        self.assertEqual([s["id"] for s in body["steps"]], ["cert", "listener", "data", "networker", "smtp", "log"])

    def test_revoke_other_sessions(self):
        other_cookie, _ = self._signed_in()
        cookie, csrf = self._signed_in(admin=True)
        status, body = self.post("/api/admin/sessions/revoke-others", {}, cookie, csrf)
        self.assertEqual(status, 200)
        self.assertGreaterEqual(body["ended"], 1)
        status, _body = self.get("/api/csrf", other_cookie)
        self.assertEqual(status, 401)
        status, _body = self.get("/api/csrf", cookie)
        self.assertEqual(status, 200)

    def test_unknown_admin_routes(self):
        cookie, csrf = self._signed_in(admin=True)
        self.assertEqual(self.get("/api/admin/nothing", cookie)[0], 404)
        self.assertEqual(self.post("/api/admin/nothing", {}, cookie, csrf)[0], 404)

    def test_first_password_route_is_closed_once_a_password_exists(self):
        status, _body = self.post("/api/admin/setup", {"password": "another long password"})
        self.assertEqual(status, 409)


class TestAdminWithoutPassword(unittest.TestCase):
    """No password: the console opens to the server itself (these requests come
    from 127.0.0.1, with no proxy header) once given the setup code the service
    wrote to data/, and offers to set the first password."""

    def setUp(self):
        self.server = AppServer().start()
        self.addCleanup(self.server.stop)

    def setup_code(self):
        return (self.server.root / "data" / "setup-code.txt").read_text(encoding="ascii").strip()

    def test_open_on_loopback_with_the_setup_code(self):
        status, body = _json(self.server.request("GET", "/api/admin/state"))
        self.assertEqual(body["access"], "open")
        self.assertFalse(body["passwordSet"])
        self.assertEqual(body["setupCodeFile"], "setup-code.txt")
        status, body = _json(self.server.request("GET", "/api/admin/overview"))
        self.assertEqual((status, body["code"]), (403, "setup_code_required"))
        code = {"X-NWDash-Setup-Code": self.setup_code()}
        status, body = _json(self.server.request("GET", "/api/admin/overview", headers=code))
        self.assertEqual(status, 200)
        status, body = _json(self.server.request("POST", "/api/admin/appearance", {"motion": "off"}, headers=code))
        self.assertEqual(status, 200)
        status, body = _json(self.server.request("POST", "/api/admin/unlock", {"password": "x"}))
        self.assertEqual(status, 409)
        self.assertIn("setup code", self.server.output())  # the start-up message names where it is

    def test_a_local_reverse_proxy_is_not_the_server_itself(self):
        # The proxy connects from loopback, so only its forwarding header tells.
        code = self.setup_code()
        proxied = {"X-Forwarded-For": "192.0.2.9"}
        status, body = _json(self.server.request("GET", "/api/admin/state", headers=proxied))
        self.assertEqual(body["access"], "denied")
        status, body = _json(self.server.request("GET", "/api/admin/overview", headers={**proxied, "X-NWDash-Setup-Code": code}))
        self.assertEqual((status, body["code"]), (403, "denied"))
        status, body = _json(self.server.request("POST", "/api/admin/setup", {"password": "first long password", "setupCode": code},
                                                 headers={"Via": "1.1 proxy"}))
        self.assertEqual(status, 403)
        self.assertFalse(_json(self.server.request("GET", "/api/admin/state"))[1]["passwordSet"])

    def test_set_the_first_password(self):
        status, body = _json(self.server.request("POST", "/api/admin/setup", {"password": "first long password"}))
        self.assertEqual((status, body["code"]), (403, "setup_code_required"))
        code = self.setup_code()
        status, body = _json(self.server.request("POST", "/api/admin/setup", {"password": "short", "setupCode": code}))
        self.assertEqual(status, 400)
        status, raw, headers = self.server.request("POST", "/api/admin/setup", {"password": "first long password", "setupCode": code})
        self.assertEqual(status, 200, raw)
        self.assertFalse((self.server.root / "data" / "setup-code.txt").exists(), "the code is one-time")
        cookie = headers.get("Set-Cookie", "").split(";", 1)[0]
        self.assertTrue(cookie)
        # Sign-in is now on for everyone: no cookie, no API.
        self.assertEqual(self.server.request("GET", "/api/status")[0], 401)
        status, body = _json(self.server.request("GET", "/api/admin/state", cookie=cookie))
        self.assertEqual((body["access"], body["unlocked"]), ("ok", True))
        status, body = _json(self.server.request("GET", "/api/admin/audit", cookie=cookie))
        self.assertIn("Password set", [row["action"] for row in body["entries"]])
        self.assertEqual(self.server.request("POST", "/api/login", {"password": "first long password"})[0], 200)


if __name__ == "__main__":
    unittest.main()
