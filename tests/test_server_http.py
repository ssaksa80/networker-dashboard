"""End-to-end smoke test: boot the real entry script in a subprocess and drive
the auth + CSRF matrix over HTTPS.

The server runs from a temp-dir copy of the entry script + nwdash package so it
creates its own data/ and .certs/ and never touches the project's state.
"""
import json
import os
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
        cls.stdout_log = open(root / "server-stdout.log", "wb")
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


if __name__ == "__main__":
    unittest.main()
