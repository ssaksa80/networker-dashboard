"""No duplicate email notifications, and ONLY explicitly scheduled ones send.

Regression tests for the field-reported bug where users received duplicate
alert emails and emails they never scheduled:
  * "Save configuration" used to ALSO arm a schedule (silent second automation
    next to the explicit Schedule button) — save must now only persist config.
  * Duplicate detection was session-scoped, so re-scheduling after a restart
    (new browser session id) created a SECOND automation with the same
    recipients; both fired. Scheduling must replace ANY same-identity schedule.
  * automations.json files already polluted with same-identity duplicates must
    be healed at restore time (keep newest, drop and log the rest).

Unit tests cover the identity helper; the e2e class follows the subprocess
pattern of test_automation_persistence.py (dead SMTP port, no NetWorker).
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
PASSWORD = "smoke-test-password-3"
STARTUP_TIMEOUT_SECONDS = 60
RESTORE_TIMEOUT_SECONDS = 30
SESSION_A = "a" * 32
SESSION_B = "b" * 32


class TestAutomationIdentity(unittest.TestCase):
    """Unit: identity is (type, recipients, host, from) — order/case invariant."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(PROJECT_DIR))
        from nwdash.emailer import automation_identity
        cls.identity = staticmethod(automation_identity)

    def test_recipient_order_and_case_do_not_change_identity(self):
        a = self.identity("alert", ["Ops@Example.com", "noc@example.com"], "SMTP.corp.local", "Dash@corp.local")
        b = self.identity("alert", ["noc@example.com ", " ops@example.COM"], "smtp.corp.local", "dash@CORP.local")
        self.assertEqual(a, b)

    def test_different_recipients_or_type_change_identity(self):
        base = self.identity("alert", ["ops@example.com"], "smtp.corp.local", "dash@corp.local")
        self.assertNotEqual(
            base, self.identity("alert", ["other@example.com"], "smtp.corp.local", "dash@corp.local")
        )
        self.assertNotEqual(
            base, self.identity("daily_report", ["ops@example.com"], "smtp.corp.local", "dash@corp.local")
        )
        self.assertNotEqual(
            base, self.identity("alert", ["ops@example.com"], "smtp2.corp.local", "dash@corp.local")
        )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestEmailDedupE2E(unittest.TestCase):
    """End-to-end over the real server subprocess (see module docstring)."""

    proc = None
    tmp = None
    port = None
    ctx = None
    stdout_log = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="nwdash-e2e-dedup-")
        root = Path(cls.tmp.name)
        shutil.copy2(PROJECT_DIR / "networker_dashboard.py", root / "networker_dashboard.py")
        shutil.copytree(
            PROJECT_DIR / "nwdash", root / "nwdash",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        cls.ctx = ssl._create_unverified_context()
        cls._boot(first=True)

    @classmethod
    def _boot(cls, first=False):
        root = Path(cls.tmp.name)
        cls.port = _free_port()
        env = dict(os.environ)
        env["DASHBOARD_AUTH_PASSWORD"] = PASSWORD
        mode = "wb" if first else "ab"
        cls.stdout_log = open(root / "server-stdout.log", mode)
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
        cookie = headers.get("Set-Cookie", "").split(";", 1)[0]
        self.assertTrue(token, "login response missing csrfToken")
        return cookie, token

    def _automation_post(self, cookie, token, payload):
        status, body, _ = self._raw_request(
            "POST", "/api/alert-automation", payload,
            headers={"X-CSRF-Token": token}, cookie=cookie,
        )
        return status, json.loads(body)

    def _list_schedules(self, cookie, token):
        status, data = self._automation_post(cookie, token, {"action": "list", "sessionId": SESSION_A})
        self.assertEqual(status, 200)
        self.assertTrue(data.get("ok"))
        return {row.get("automationId"): row for row in data.get("schedules", [])}

    def _smtp_form_payload(self, **overrides):
        # Dead local SMTP port: scheduling never needs a reachable SMTP server.
        payload = {
            "sessionId": SESSION_A,
            "smtpHost": "127.0.0.1",
            "smtpPort": str(_free_port()),
            "smtpSecurity": "none",
            "smtpUsername": "",
            "smtpPassword": "",
            "smtpFrom": "dashboard@example.com",
            "smtpTo": "ops@example.com, noc@example.com",
            "scheduleType": "alert",
            "intervalMinutes": "60",
            "trigger": "critical",
            "reportTime": "08:00",
            "quietStart": "",
            "quietEnd": "",
            "digest": True,
            "theme": "default",
        }
        payload.update(overrides)
        return payload

    def test_save_never_arms_and_identity_replaces_and_restore_heals(self):
        cookie, token = self._login()

        # 1) SAVE must persist config only — the schedules list stays EMPTY.
        status, data = self._automation_post(
            cookie, token, self._smtp_form_payload(action="save")
        )
        self.assertEqual(status, 200, data)
        self.assertTrue(data.get("ok"), data)
        self.assertIn("configuration saved", str(data.get("message", "")).lower())
        self.assertEqual(data.get("activeAutomations", ""), "")
        self.assertEqual(self._list_schedules(cookie, token), {},
                         "save action armed a schedule; only start may do that")

        # 2) START from session A arms exactly one schedule.
        status, data = self._automation_post(
            cookie, token, self._smtp_form_payload(action="start")
        )
        self.assertEqual(status, 200, data)
        first_id = data.get("automationId", "")
        self.assertTrue(first_id)
        self.assertEqual(sorted(self._list_schedules(cookie, token)), [first_id])

        # 3) START again from a DIFFERENT session id with the same recipients
        # (different order/case) — the old schedule is REPLACED, never doubled.
        status, data = self._automation_post(
            cookie, token,
            self._smtp_form_payload(
                action="start", sessionId=SESSION_B,
                smtpTo="NOC@example.com, ops@EXAMPLE.com",
            ),
        )
        self.assertEqual(status, 200, data)
        second_id = data.get("automationId", "")
        self.assertTrue(second_id)
        rows = self._list_schedules(cookie, token)
        self.assertEqual(
            sorted(rows), [second_id],
            f"same-identity reschedule duplicated the schedule: {sorted(rows)}",
        )

        # 4) Heal on restore: craft an automations.json that already contains
        # two same-identity records (the pre-fix pollution), reusing the exact
        # shape the live server persisted.
        automations_file = Path(self.tmp.name) / "data" / "automations.json"
        self.assertTrue(automations_file.exists())
        self._teardown_proc()
        records = json.loads(automations_file.read_text(encoding="utf-8"))
        self.assertEqual(sorted(records), [second_id])
        survivor = records[second_id]
        stale_id = f"{SESSION_A}:deadbeef"
        stale = dict(survivor)
        stale["automation_id"] = stale_id
        stale["created_at"] = float(survivor.get("created_at") or time.time()) - 1000.0
        records[stale_id] = stale
        automations_file.write_text(json.dumps(records), encoding="utf-8")

        # 5) Fresh boot: only the NEWEST record per identity survives, and the
        # drop is logged.
        self._boot()
        cookie, token = self._login()
        deadline = time.monotonic() + RESTORE_TIMEOUT_SECONDS
        rows = {}
        while time.monotonic() < deadline:
            rows = self._list_schedules(cookie, token)
            if sorted(rows) == [second_id]:
                break
            time.sleep(0.5)
        self.assertEqual(
            sorted(rows), [second_id],
            f"restore did not dedup same-identity schedules: {sorted(rows)}",
        )
        log_text = (Path(self.tmp.name) / "server-stdout.log").read_text(
            encoding="utf-8", errors="replace"
        )
        self.assertIn("dropped duplicate schedule", log_text)
        self.assertIn(stale_id, log_text)

        # 6) The healed store is persisted back to disk without the duplicate
        # (persist runs moments after the in-memory dedup; brief poll).
        healed = {}
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            healed = json.loads(automations_file.read_text(encoding="utf-8"))
            if sorted(healed) == [second_id]:
                break
            time.sleep(0.5)
        self.assertEqual(sorted(healed), [second_id])

        # 7) Cleanup: stop the surviving schedule.
        status, data = self._automation_post(
            cookie, token,
            {"action": "stop", "sessionId": SESSION_B, "automationId": second_id},
        )
        self.assertEqual(status, 200, data)
        self.assertEqual(self._list_schedules(cookie, token), {})


if __name__ == "__main__":
    unittest.main()
