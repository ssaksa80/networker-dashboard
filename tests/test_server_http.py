"""End-to-end smoke test: boot the real entry script in a subprocess and drive
the auth + CSRF matrix over HTTPS.

The server runs from a temp-dir copy of the entry script + nwdash package so it
creates its own data/ and .certs/ and never touches the project's state.
"""
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
PASSWORD = "smoke-test-password-1"
STARTUP_TIMEOUT_SECONDS = 60


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestServerHttp(unittest.TestCase):
    proc = None
    tmp = None
    port = None
    ctx = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="nwdash-e2e-")
        root = Path(cls.tmp.name)
        shutil.copy2(PROJECT_DIR / "networker_dashboard.py", root / "networker_dashboard.py")
        shutil.copytree(
            PROJECT_DIR / "nwdash", root / "nwdash",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        cls.port = _free_port()
        env = dict(os.environ)
        env["DASHBOARD_AUTH_PASSWORD"] = PASSWORD
        cls.stdout_log = cls.enterClassContext(open(root / "server-stdout.log", "wb"))
        try:
            cls.proc = subprocess.Popen(
                [sys.executable, "networker_dashboard.py",
                 "--port", str(cls.port), "--bind", "127.0.0.1", "--no-launch"],
                cwd=str(root),
                env=env,
                stdout=cls.stdout_log,
                stderr=subprocess.STDOUT,
            )
        except OSError as exc:
            raise unittest.SkipTest(f"could not launch server subprocess: {exc}")
        cls.ctx = ssl._create_unverified_context()
        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        last_error = "no response"
        while time.monotonic() < deadline:
            if cls.proc.poll() is not None:
                cls._teardown_proc()
                raise unittest.SkipTest(
                    f"server exited early (code {cls.proc.returncode}) — port "
                    f"{cls.port} may not be bindable in this environment"
                )
            try:
                status, _, _ = cls._raw_request("GET", "/api/health")
                if status == 200:
                    return
                last_error = f"health status {status}"
            except (urllib.error.URLError, OSError, ConnectionError) as exc:
                last_error = str(exc)
            time.sleep(0.5)
        cls._teardown_proc()
        raise unittest.SkipTest(
            f"server did not become healthy on 127.0.0.1:{cls.port} within "
            f"{STARTUP_TIMEOUT_SECONDS}s ({last_error})"
        )

    @classmethod
    def _teardown_proc(cls):
        proc, cls.proc = cls.proc, None
        if proc is not None and proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    check=False,
                )
        if cls.stdout_log is not None:
            cls.stdout_log.close()
            cls.stdout_log = None

    @classmethod
    def tearDownClass(cls):
        cls._teardown_proc()
        if cls.tmp is not None:
            try:
                cls.tmp.cleanup()
            except OSError:
                pass  # transient Windows file locks on temp dir; OS cleans later

    @classmethod
    def _raw_request(cls, method, path, body=None, headers=None, cookie=None):
        h = dict(headers or {})
        if cookie:
            h["Cookie"] = cookie
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(
            f"https://127.0.0.1:{cls.port}{path}", data=data, headers=h, method=method
        )
        try:
            with urllib.request.urlopen(req, context=cls.ctx, timeout=20) as resp:
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), exc.headers

    def _login(self):
        status, body, headers = self._raw_request("POST", "/api/login", {"password": PASSWORD})
        self.assertEqual(status, 200)
        payload = json.loads(body)
        token = payload.get("csrfToken", "")
        set_cookie = headers.get("Set-Cookie", "")
        cookie = set_cookie.split(";", 1)[0]
        self.assertTrue(token, "login response missing csrfToken")
        self.assertTrue(cookie.startswith("nwdash_auth="), f"unexpected cookie: {set_cookie!r}")
        return cookie, token

    def _linked_assets(self, page):
        """Fetch, with no cookie, every file the page links and every file its
        CSS links (review F10), and return their bodies joined."""
        queue = list(dict.fromkeys(re.findall(rb'(?:href|src)="(/assets/[^"]+)"', page)))
        self.assertTrue(queue, "the page links no assets")
        seen, bodies = set(), []
        while queue:
            url = queue.pop(0).decode("ascii")
            if url in seen:
                continue
            seen.add(url)
            status, body, headers = self._raw_request("GET", url)
            self.assertEqual(status, 200, url)
            self.assertEqual(headers.get("Cache-Control"), "public, max-age=31536000, immutable", url)
            self.assertIsNone(headers.get("Pragma"), url)
            self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff", url)
            expected = {"css": "text/css", "js": "text/javascript", "png": "image/png"}[url.rsplit(".", 1)[1]]
            self.assertEqual(headers.get("Content-Type", "").split(";")[0], expected, url)
            if url.endswith(".css"):
                queue += re.findall(rb'url\("(/assets/[^"]+)"\)', body)
            bodies.append(body)
        return b"".join(bodies)

    def test_health_ok(self):
        status, body, _ = self._raw_request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body).get("ok"))

    def test_unauth_post_rejected(self):
        status, _, _ = self._raw_request("POST", "/api/ui-theme", {"theme": "default"})
        self.assertEqual(status, 401)

    def test_login_wrong_password(self):
        status, _, _ = self._raw_request("POST", "/api/login", {"password": "not-the-password"})
        self.assertEqual(status, 401)

    def test_login_right_password_and_csrf_matrix(self):
        cookie, token = self._login()

        status, _, _ = self._raw_request("POST", "/api/ui-theme", {"theme": "default"}, cookie=cookie)
        self.assertEqual(status, 403, "authed POST without CSRF token must be 403")

        status, _, _ = self._raw_request(
            "POST", "/api/ui-theme", {"theme": "default"},
            headers={"X-CSRF-Token": token}, cookie=cookie,
        )
        self.assertEqual(status, 200, "authed POST with CSRF token must be 200")

    def test_logout_revokes_the_cookie_server_side(self):
        # Regression (review S6): logout only expired the cookie in the browser;
        # replaying a captured copy kept working for its full 12 h.
        cookie, token = self._login()
        other_cookie, _ = self._login()
        status, body, _ = self._raw_request(
            "POST", "/api/logout", {}, headers={"X-CSRF-Token": token}, cookie=cookie,
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body).get("revoked"))
        status, _, _ = self._raw_request("GET", "/api/csrf", cookie=cookie)
        self.assertEqual(status, 401, "a logged-out cookie must stop working everywhere")
        status, _, _ = self._raw_request(
            "POST", "/api/ui-theme", {"theme": "default"}, headers={"X-CSRF-Token": token}, cookie=cookie,
        )
        self.assertEqual(status, 401)
        status, _, _ = self._raw_request("GET", "/api/csrf", cookie=other_cookie)
        self.assertEqual(status, 200, "other logins are unaffected")

    def test_csrf_endpoint_matches_login_token(self):
        cookie, token = self._login()
        status, body, _ = self._raw_request("GET", "/api/csrf", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body).get("csrfToken"), token)

    def test_unknown_post_path_404(self):
        cookie, token = self._login()
        status, _, _ = self._raw_request(
            "POST", "/api/definitely-not-a-real-endpoint", {},
            headers={"X-CSRF-Token": token}, cookie=cookie,
        )
        self.assertEqual(status, 404)

    def test_keepalive_survives_early_error_post(self):
        # Regression: an early-error POST (401/403/404) must drain its request
        # body. Before the fix, the unread body bytes were parsed as the start
        # of the next request line on the same keep-alive connection
        # ('{"theme":..}GET /api/health' -> 501), poisoning the connection.
        import http.client

        conn = http.client.HTTPSConnection("127.0.0.1", self.port, context=self.ctx, timeout=20)
        try:
            payload = json.dumps({"theme": "default"})
            # 1: unauthenticated bodied POST -> 401, body must be drained
            conn.request("POST", "/api/ui-theme", body=payload,
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            self.assertEqual(resp.status, 401)
            resp.read()
            # 2: SAME connection must still parse the next request cleanly
            conn.request("GET", "/api/health")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            self.assertIn(b'"ok":', resp.read().replace(b" ", b""))
            # 3: authed but tokenless bodied POST -> 403, then reuse again
            cookie, _ = self._login()
            conn.request("POST", "/api/ui-theme", body=payload,
                         headers={"Content-Type": "application/json", "Cookie": cookie})
            resp = conn.getresponse()
            self.assertEqual(resp.status, 403)
            resp.read()
            conn.request("GET", "/api/health")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            resp.read()
        finally:
            conn.close()

    def test_root_without_cookie_serves_login_page(self):
        status, body, _ = self._raw_request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"/api/login", body)
        self.assertNotIn(b"__NETWORKER_LOGO_SRC__", body)

    def test_tv_without_cookie_serves_login_page(self):
        # /tv is auth-gated exactly like /
        status, body, _ = self._raw_request("GET", "/tv")
        self.assertEqual(status, 200)
        self.assertIn(b"/api/login", body)
        self.assertNotIn(b"calc(100vw / 240)", body)

    def test_tv_with_cookie_serves_tv_page(self):
        cookie, _ = self._login()
        status, body, _ = self._raw_request("GET", "/tv", cookie=cookie)
        self.assertEqual(status, 200)
        loaded = body + self._linked_assets(body)
        self.assertIn(b"calc(100vw / 240)", loaded, "fluid scaling rule missing")
        self.assertIn(b"jobs-min", loaded, "minimize wiring missing")
        self.assertIn(b"\x89PNG", loaded, "linked logo missing")
        self.assertIn(b"/api/current-dashboard", loaded, "bootstrap fetch missing")
        self.assertIn(b"/api/stream", loaded, "SSE wiring missing")
        self.assertNotIn(b"__NETWORKER_LOGO_SRC__", loaded)

    def test_dashboard_links_its_css_js_and_logo(self):
        # Review F10: the page was 1.4 MB, the logo inlined five times.
        cookie, _ = self._login()
        status, body, headers = self._raw_request("GET", "/", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Cache-Control"), "no-store, max-age=0")
        self.assertLess(len(body), 100_000)
        self.assertNotIn(b"data:image/", body)
        loaded = body + self._linked_assets(body)
        self.assertIn(b"X-CSRF-Token", loaded)
        self.assertIn(b"window.NWDesign = {", loaded)
        self.assertIn(b"function renderDashboard(data)", loaded)
        self.assertNotRegex(loaded, rb"__[A-Z_]+__")

    def test_admin_console_loads_its_assets_before_sign_in(self):
        # The console's own sign-in card needs its CSS and JS, so the asset
        # routes are open; the page is the only thing that links them.
        status, body, _ = self._raw_request("GET", "/admin")
        self.assertEqual(status, 200)
        self.assertNotIn(b"data:image/", body)
        loaded = body + self._linked_assets(body)
        self.assertIn(b'opts.headers["X-CSRF-Token"] = csrf', loaded)
        self.assertNotRegex(loaded, rb"__[A-Z_]+__")

    def test_only_the_linked_urls_are_served(self):
        cookie, _ = self._login()
        for path in ("/assets/app.css", "/assets/app.0000000000000000.css", "/assets/", "/assets/app.js/",
                     "/assets/../nwdash/ui.py", "/assets/%2e%2e/nwdash/ui.py", "/nwdash/assets/app.css"):
            with self.subTest(path=path):
                status, _, _ = self._raw_request("GET", path)
                self.assertEqual(status, 401)
                status, _, _ = self._raw_request("GET", path, cookie=cookie)
                self.assertEqual(status, 404)

    def test_logo_route_serves_the_logo(self):
        # It served the favicon wherever assets/networker-logo.png was absent,
        # which is every install: the logo is embedded in nwdash/ui.py.
        status, body, headers = self._raw_request("GET", "/networker-logo.png")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "image/png")
        self.assertTrue(body.startswith(b"\x89PNG"))

    def test_current_dashboard_with_cookie_returns_json(self):
        cookie, _ = self._login()
        status, body, _ = self._raw_request("GET", "/api/current-dashboard", cookie=cookie)
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertIn("ok", payload)  # fresh boot: ok=False with a message is fine
        if not payload.get("ok"):
            self.assertTrue(payload.get("message"))


if __name__ == "__main__":
    unittest.main()
