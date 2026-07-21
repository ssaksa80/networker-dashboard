"""End-to-end: email alert schedules and named email profiles survive a full
server restart.

Regression for the field-reported bug where saved schedules vanished after a
restart/update: automations were keyed to a live dashboard session and were
cancelled/dropped as soon as the session disappeared. Schedules must now be
restored unconditionally from data/automations.json — deliberately WITHOUT any
surviving session (this test never creates one; that is the point).

Extends the subprocess pattern of test_server_http.py: boot the real entry
script from a temp dir, drive the HTTPS API with auth + CSRF, kill the process,
boot a FRESH process on the same data dir, and assert the state came back.
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
PASSWORD = "smoke-test-password-2"
STARTUP_TIMEOUT_SECONDS = 60
RESTORE_TIMEOUT_SECONDS = 30
SECRET_SMTP_PASSWORD = "super-secret-smtp-pw-1"
PROFILE_NAME = "NOC Shift"
# A session id that never existed in any server process: schedules must be
# accepted, persisted, and restored without it ever becoming live.
DEAD_SESSION_ID = "e2e0dead0session0id0000000000000"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestAutomationRestartPersistence(unittest.TestCase):
    proc = None
    tmp = None
    port = None
    ctx = None
    stdout_log = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="nwdash-e2e-persist-")
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
        """Start (or restart) the server subprocess against the SAME temp dir.
        Each boot uses a fresh port so a lingering old listener can never make
        bind_dashboard_server silently fall back to a random port."""
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
        self.assertTrue(cookie.startswith("nwdash_auth="))
        return cookie, token

    def _automation_post(self, cookie, token, payload):
        status, body, _ = self._raw_request(
            "POST", "/api/alert-automation", payload,
            headers={"X-CSRF-Token": token}, cookie=cookie,
        )
        return status, json.loads(body)

    def _list_schedule_ids(self, cookie, token):
        status, data = self._automation_post(cookie, token, {"action": "list", "sessionId": DEAD_SESSION_ID})
        self.assertEqual(status, 200)
        self.assertTrue(data.get("ok"))
        return {row.get("automationId"): row for row in data.get("schedules", [])}

    def _wait_for_schedule(self, cookie, token, automation_id):
        """Automation restore runs on a background thread after boot; poll."""
        deadline = time.monotonic() + RESTORE_TIMEOUT_SECONDS
        rows = {}
        while time.monotonic() < deadline:
            rows = self._list_schedule_ids(cookie, token)
            if automation_id in rows:
                return rows[automation_id]
            time.sleep(0.5)
        self.fail(
            f"automation {automation_id!r} not restored within {RESTORE_TIMEOUT_SECONDS}s; "
            f"listed: {sorted(rows)}"
        )

    def _smtp_form_payload(self, **overrides):
        # SMTP host points at a local port nobody listens on: scheduling and
        # persistence must never require a reachable SMTP server or NetWorker.
        payload = {
            "sessionId": DEAD_SESSION_ID,
            "smtpHost": "127.0.0.1",
            "smtpPort": str(_free_port()),  # freed immediately -> dead port
            "smtpSecurity": "none",
            "smtpUsername": "",
            "smtpPassword": "",
            "smtpFrom": "dashboard@example.com",
            "smtpTo": "ops@example.com",
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

    def test_schedule_and_profiles_survive_server_restart(self):
        cookie, token = self._login()

        # 1) Schedule an automation with NO live dashboard session.
        status, data = self._automation_post(
            cookie, token, self._smtp_form_payload(action="start")
        )
        self.assertEqual(status, 200, data)
        self.assertTrue(data.get("ok"), data)
        automation_id = data.get("automationId", "")
        self.assertTrue(automation_id, "start did not return an automationId")

        # 2) It is listed as saved (and flagged as not session-bound).
        rows = self._list_schedule_ids(cookie, token)
        self.assertIn(automation_id, rows)
        self.assertFalse(rows[automation_id].get("sessionLive"))

        # ...and it is on disk, ready to survive the restart.
        data_dir = Path(self.tmp.name) / "data"
        self.assertTrue((data_dir / "automations.json").exists())

        # 3) Profile CRUD: save a named profile with a real password...
        status, data = self._automation_post(
            cookie, token,
            self._smtp_form_payload(
                action="save-profile", profileName=PROFILE_NAME,
                smtpUsername="alerts@example.com", smtpPassword=SECRET_SMTP_PASSWORD,
            ),
        )
        self.assertEqual(status, 200, data)
        self.assertTrue(data.get("ok"), data)
        self.assertIn(PROFILE_NAME, data.get("profiles", {}))

        # ...list must mask the password with the "(saved)" sentinel and never
        # leak the plaintext anywhere in the response.
        status, body, _ = self._raw_request(
            "POST", "/api/alert-automation", {"action": "list-profiles"},
            headers={"X-CSRF-Token": token}, cookie=cookie,
        )
        self.assertEqual(status, 200)
        self.assertNotIn(SECRET_SMTP_PASSWORD.encode(), body)
        listed = json.loads(body)
        profile = listed["profiles"][PROFILE_NAME]
        self.assertEqual(profile.get("smtpPassword"), "(saved)")
        self.assertEqual(profile.get("smtpHost"), "127.0.0.1")
        self.assertNotIn("_enc_smtpPassword", profile)

        # get-profile is masked the same way.
        status, data = self._automation_post(
            cookie, token, {"action": "get-profile", "profileName": PROFILE_NAME}
        )
        self.assertEqual(status, 200, data)
        self.assertEqual(data["profile"].get("smtpPassword"), "(saved)")
        self.assertTrue((data_dir / "email_profiles.json").exists())

        # 4) Hard restart: kill the process, boot a FRESH one on the same data
        # dir. No dashboard session can survive this — that is the scenario
        # that used to wipe every schedule.
        self._teardown_proc()
        self._boot()
        cookie, token = self._login()

        # 5) The schedule is STILL listed after restart (restore is async).
        row = self._wait_for_schedule(cookie, token, automation_id)
        self.assertEqual(row.get("scheduleType"), "alert")
        self.assertEqual(row.get("recipients"), "ops@example.com")
        self.assertFalse(row.get("sessionLive"))

        # 6) The named profile also survived, still masked.
        status, data = self._automation_post(cookie, token, {"action": "list-profiles"})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["profiles"][PROFILE_NAME].get("smtpPassword"), "(saved)")

        # 7) Delete the profile; it is gone from the store.
        status, data = self._automation_post(
            cookie, token, {"action": "delete-profile", "profileName": PROFILE_NAME}
        )
        self.assertEqual(status, 200, data)
        self.assertNotIn(PROFILE_NAME, data.get("profiles", {}))
        status, data = self._automation_post(cookie, token, {"action": "list-profiles"})
        self.assertNotIn(PROFILE_NAME, data.get("profiles", {}))

        # 8) Editing the restored schedule from a NEW browser session must
        # update it in place, not duplicate it.
        new_session = "b" * 32
        status, data = self._automation_post(
            cookie, token,
            self._smtp_form_payload(
                action="start", sessionId=new_session,
                automationId=automation_id, intervalMinutes="120",
            ),
        )
        self.assertEqual(status, 200, data)
        self.assertEqual(data.get("automationId"), automation_id, "edit minted a new schedule id")
        rows = self._list_schedule_ids(cookie, token)
        self.assertEqual(sorted(rows), [automation_id], f"edit duplicated the schedule: {sorted(rows)}")
        self.assertEqual(rows[automation_id].get("intervalMinutes"), 120)

        # 9) The restored schedule can still be stopped by id (cleanup).
        status, data = self._automation_post(
            cookie, token,
            {"action": "stop", "sessionId": DEAD_SESSION_ID, "automationId": automation_id},
        )
        self.assertEqual(status, 200, data)
        self.assertNotIn(automation_id, self._list_schedule_ids(cookie, token))


if __name__ == "__main__":
    unittest.main()
