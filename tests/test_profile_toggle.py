"""Profile-based scheduling: the ON/OFF toggle on saved email profiles.

Toggling a profile ON arms a schedule built from the STORED profile settings
(identity-replace semantics — never a duplicate); toggling OFF stops it. The
enabled state is DERIVED (a matching automation exists), never stored, and the
automation's profile_name stamp persists across restarts so cards stay ON
after a server restart. The e2e class follows the subprocess pattern of
tests/test_email_dedup.py (dead SMTP port, no NetWorker).

Source guards keep the new UI honest: the rendered dashboard ships the toggle
markup/classes, and every one of the 15 themes defines the --glow variable the
modern automation-modal styling relies on.
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
PASSWORD = "smoke-test-password-4"
STARTUP_TIMEOUT_SECONDS = 60
RESTORE_TIMEOUT_SECONDS = 30
SESSION_A = "c" * 32
PROFILE_NAME = "Ops toggle profile"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestSourceGuards(unittest.TestCase):
    """The shipped assets carry the profile-card toggle UI and theme glow."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(PROJECT_DIR))

    def test_rendered_dashboard_ships_profile_toggle_markup(self):
        from nwdash.ui import dashboard_html
        html = dashboard_html()
        for token in (
            'id="emailProfileCards"',
            "email-profile-cards",
            "ep-switch",
            "ep-toggle",
            "ep-slider",
            "toggle-profile",  # the JS action the switch posts
            "automation-modal",  # glow/press scoping wrapper class
        ):
            self.assertIn(token, html, token)

    def test_glow_variable_defined_for_all_15_themes(self):
        from nwdash.config import THEME_PALETTES
        css = (PROJECT_DIR / "nwdash" / "assets" / "app.css").read_text(encoding="utf-8")
        self.assertEqual(
            css.count("--glow:"), len(THEME_PALETTES),
            "--glow must be defined exactly once per theme (incl. :root default)",
        )
        root = re.search(r":root \{([^}]*)\}", css)
        self.assertIsNotNone(root)
        self.assertIn("--glow:", root.group(1), ":root (default theme)")
        for name in THEME_PALETTES:
            if name == "default":
                continue
            block = None
            for match in re.finditer(r'body\[data-theme="(\w+)"\] \{([^}]*)\}', css):
                if match.group(1) == name and "--brand:" in match.group(2):
                    block = match.group(2)
                    break
            self.assertIsNotNone(block, f"theme block missing: {name}")
            self.assertIn("--glow:", block, name)


class TestProfileToggleE2E(unittest.TestCase):
    """save-profile -> toggle on -> restart -> still on -> toggle off/on."""

    proc = None
    tmp = None
    port = None
    ctx = None
    stdout_log = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="nwdash-e2e-toggle-")
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

    def _list_profiles(self, cookie, token):
        status, data = self._automation_post(cookie, token, {"action": "list-profiles"})
        self.assertEqual(status, 200, data)
        self.assertTrue(data.get("ok"), data)
        return data.get("profiles", {})

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

    def test_toggle_lifecycle_survives_restart(self):
        cookie, token = self._login()

        # 1) Save a profile — nothing is scheduled and the card reads OFF.
        status, data = self._automation_post(
            cookie, token,
            self._smtp_form_payload(action="save-profile", profileName=PROFILE_NAME),
        )
        self.assertEqual(status, 200, data)
        self.assertTrue(data.get("ok"), data)
        profiles = data.get("profiles", {})
        self.assertIn(PROFILE_NAME, profiles)
        self.assertFalse(profiles[PROFILE_NAME].get("enabled"), profiles)
        self.assertEqual(self._list_schedules(cookie, token), {},
                         "save-profile must never arm a schedule")
        profiles = self._list_profiles(cookie, token)
        self.assertFalse(profiles[PROFILE_NAME].get("enabled"))
        self.assertEqual(profiles[PROFILE_NAME].get("automationId", ""), "")
        # The stored secrets never leak through the API.
        self.assertNotIn("_enc_smtpPassword", profiles[PROFILE_NAME])
        self.assertNotIn("_connection", profiles[PROFILE_NAME])

        # 2) Toggle ON — exactly one schedule armed from the stored profile.
        status, data = self._automation_post(
            cookie, token,
            {"action": "toggle-profile", "profileName": PROFILE_NAME,
             "enabled": True, "sessionId": SESSION_A},
        )
        self.assertEqual(status, 200, data)
        self.assertTrue(data.get("ok"), data)
        self.assertTrue(data.get("enabled"), data)
        automation_id = data.get("automationId", "")
        self.assertTrue(automation_id)
        rows = self._list_schedules(cookie, token)
        self.assertEqual(sorted(rows), [automation_id],
                         f"toggle-on must arm exactly one schedule: {sorted(rows)}")
        self.assertEqual(rows[automation_id].get("profileName"), PROFILE_NAME)
        profiles = self._list_profiles(cookie, token)
        self.assertTrue(profiles[PROFILE_NAME].get("enabled"))
        self.assertEqual(profiles[PROFILE_NAME].get("automationId"), automation_id)

        # Toggling ON again REPLACES (identity), never duplicates.
        status, data = self._automation_post(
            cookie, token,
            {"action": "toggle-profile", "profileName": PROFILE_NAME,
             "enabled": True, "sessionId": SESSION_A},
        )
        self.assertEqual(status, 200, data)
        rows = self._list_schedules(cookie, token)
        self.assertEqual(len(rows), 1, f"toggle-on duplicated the schedule: {sorted(rows)}")
        automation_id = data.get("automationId", "")

        # 3) profile_name persists to disk (survives the restart below).
        automations_file = Path(self.tmp.name) / "data" / "automations.json"
        records = json.loads(automations_file.read_text(encoding="utf-8"))
        self.assertEqual(sorted(records), [automation_id])
        self.assertEqual(records[automation_id].get("profile_name"), PROFILE_NAME)

        # 4) RESTART: the profile is still ON and its schedule still armed.
        self._teardown_proc()
        self._boot()
        cookie, token = self._login()
        deadline = time.monotonic() + RESTORE_TIMEOUT_SECONDS
        rows = {}
        while time.monotonic() < deadline:
            rows = self._list_schedules(cookie, token)
            if sorted(rows) == [automation_id]:
                break
            time.sleep(0.5)
        self.assertEqual(sorted(rows), [automation_id],
                         f"schedule did not survive the restart: {sorted(rows)}")
        self.assertEqual(rows[automation_id].get("profileName"), PROFILE_NAME,
                         "profile_name did not round-trip through automations.json")
        profiles = self._list_profiles(cookie, token)
        self.assertTrue(profiles[PROFILE_NAME].get("enabled"),
                        "profile shows OFF after restart despite an armed schedule")
        self.assertEqual(profiles[PROFILE_NAME].get("automationId"), automation_id)

        # 5) Toggle OFF — schedule gone, card reads OFF.
        status, data = self._automation_post(
            cookie, token,
            {"action": "toggle-profile", "profileName": PROFILE_NAME,
             "enabled": False, "sessionId": SESSION_A},
        )
        self.assertEqual(status, 200, data)
        self.assertTrue(data.get("ok"), data)
        self.assertFalse(data.get("enabled"), data)
        self.assertEqual(self._list_schedules(cookie, token), {})
        profiles = self._list_profiles(cookie, token)
        self.assertFalse(profiles[PROFILE_NAME].get("enabled"))

        # 6) Toggle ON again on the restarted server: no live NetWorker
        # session exists, so it must arm purely from the stored profile.
        status, data = self._automation_post(
            cookie, token,
            {"action": "toggle-profile", "profileName": PROFILE_NAME,
             "enabled": True, "sessionId": ""},
        )
        self.assertEqual(status, 200, data)
        self.assertTrue(data.get("enabled"), data)
        rearmed_id = data.get("automationId", "")
        self.assertTrue(rearmed_id)
        rows = self._list_schedules(cookie, token)
        self.assertEqual(sorted(rows), [rearmed_id])
        self.assertEqual(rows[rearmed_id].get("profileName"), PROFILE_NAME)

        # 7) Cleanup: off again.
        status, data = self._automation_post(
            cookie, token,
            {"action": "toggle-profile", "profileName": PROFILE_NAME,
             "enabled": False, "sessionId": ""},
        )
        self.assertEqual(status, 200, data)
        self.assertEqual(self._list_schedules(cookie, token), {})


if __name__ == "__main__":
    unittest.main()
