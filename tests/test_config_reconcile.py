"""email_config.json is the source of truth for CONFIG-DRIVEN schedules.

Regression tests for the field-reported bug where a deployed automations.json
carried STALE config-driven schedules whose recipients had drifted: the
identity dedup (type + recipients + host + from) treats them as distinct
schedules, so both kept firing — duplicate/wrong emails. Recipients drift over
time, so they cannot participate in the uniqueness key for schedules armed
from the form (empty profile_name — includes every legacy record).

Contract under test:
  * Boot-time reconciliation: at most ONE config-driven schedule per
    schedule_type survives restore (newest created_at wins), and the survivor's
    settings are SYNCED from email_config.json. Fields the config file does
    not store (quiet hours) are preserved.
  * Profile-driven schedules (profile_name set) are untouched — multiple named
    profiles may be ON concurrently by design.
  * Arm-time singleton: action=start with no profile cancels every other
    config-driven schedule of the same type, regardless of recipients.

Follows the subprocess e2e pattern of test_email_dedup.py (dead SMTP port,
no NetWorker).
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
PASSWORD = "smoke-test-password-4"
STARTUP_TIMEOUT_SECONDS = 60
RESTORE_TIMEOUT_SECONDS = 30
SESSION_A = "a" * 32
SESSION_B = "b" * 32
SESSION_OLD = "c" * 32

OLD_DAILY_ID = f"{SESSION_OLD}:0da11o1d"
NEW_DAILY_ID = f"{SESSION_A}:0da11new"
OLD_ALERT_ID = f"{SESSION_OLD}:a1e1to1d"
NEW_ALERT_ID = f"{SESSION_A}:a1e1tnew"

# The saved email configuration (anonymized prod shape): ONE config per type.
DAILY_RECIPIENTS = [
    "ops1@example.com", "ops2@example.com", "ops3@example.com",
    "ops4@example.com", "ops5@example.com",
]
ALERT_RECIPIENT = "alert-new@example.com"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


SMTP_PORT = _free_port()  # dead local port: nothing ever needs to deliver


def _email_config() -> dict:
    return {
        "smtp": {
            "host": "127.0.0.1",
            "port": SMTP_PORT,
            "security": "none",
            "username": "",
            "from": "dashboard@example.com",
            "encrypted_password": "",
        },
        "types": {
            "daily_report": {
                "recipients": list(DAILY_RECIPIENTS),
                "report_time": "07:30",
                "theme": "steel",
            },
            "alert": {
                "recipients": [ALERT_RECIPIENT],
                "trigger": "warning",
                "interval_minutes": 240,
            },
        },
    }


def _legacy_record(automation_id, session_id, schedule_type, recipients,
                   created_at, **overrides) -> dict:
    """automations.json record in the LEGACY shape the field file has: no
    profile_name key, no connection snapshot."""
    record = {
        "automation_id": automation_id,
        "session_id": session_id,
        "smtp_host": "127.0.0.1",
        "smtp_port": SMTP_PORT,
        "smtp_username": "",
        "encrypted_smtp_password": "",
        "smtp_from": "dashboard@example.com",
        "recipients": list(recipients),
        "smtp_security": "none",
        "interval_minutes": 60,
        "trigger": "critical",
        "schedule_type": schedule_type,
        "report_time": "08:00",
        "created_at": created_at,
        "theme": "default",
        "last_signature": "",
        "enabled": True,
        "quiet_start": "",
        "quiet_end": "",
        "digest": True,
    }
    record.update(overrides)
    return record


class TestConfigReconcileE2E(unittest.TestCase):
    """End-to-end over the real server subprocess (see module docstring)."""

    proc = None
    tmp = None
    port = None
    ctx = None
    stdout_log = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="nwdash-e2e-reconcile-")
        root = Path(cls.tmp.name)
        shutil.copy2(PROJECT_DIR / "networker_dashboard.py", root / "networker_dashboard.py")
        shutil.copytree(
            PROJECT_DIR / "nwdash", root / "nwdash",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        cls.ctx = ssl._create_unverified_context()
        # Pre-seed the EXACT field scenario (anonymized) BEFORE first boot:
        # four legacy config-driven schedules — two per type, recipients
        # drifted apart so identity dedup keeps all four — plus a saved email
        # config holding exactly one configuration per type.
        now = time.time()
        data_dir = root / "data"
        data_dir.mkdir()
        records = {
            OLD_DAILY_ID: _legacy_record(
                OLD_DAILY_ID, SESSION_OLD, "daily_report",
                ["a@example.com", "b@example.com", "c@example.com", "d@example.com"],
                now - 5000.0,
            ),
            NEW_DAILY_ID: _legacy_record(
                NEW_DAILY_ID, SESSION_A, "daily_report",
                DAILY_RECIPIENTS, now - 1000.0, report_time="07:30",
            ),
            OLD_ALERT_ID: _legacy_record(
                OLD_ALERT_ID, SESSION_OLD, "alert",
                ["alert-old@example.com"], now - 5000.0,
            ),
            NEW_ALERT_ID: _legacy_record(
                NEW_ALERT_ID, SESSION_A, "alert",
                [ALERT_RECIPIENT], now - 1000.0,
                quiet_start="07:00", quiet_end="17:00",
            ),
        }
        (data_dir / "automations.json").write_text(json.dumps(records), encoding="utf-8")
        (data_dir / "email_config.json").write_text(json.dumps(_email_config()), encoding="utf-8")
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

    def _wait_for_schedule_ids(self, cookie, token, expected_ids):
        deadline = time.monotonic() + RESTORE_TIMEOUT_SECONDS
        rows = {}
        while time.monotonic() < deadline:
            rows = self._list_schedules(cookie, token)
            if sorted(rows) == sorted(expected_ids):
                return rows
            time.sleep(0.5)
        self.fail(f"expected schedules {sorted(expected_ids)}, got {sorted(rows)}")

    def test_boot_reconciles_and_start_enforces_type_singleton(self):
        cookie, token = self._login()

        # ── 1) Field scenario: 4 legacy records boot down to 2 (newest per
        # type), each synced to email_config.json. ──────────────────────────
        rows = self._wait_for_schedule_ids(cookie, token, [NEW_DAILY_ID, NEW_ALERT_ID])
        daily = rows[NEW_DAILY_ID]
        self.assertEqual(daily.get("recipients"), ", ".join(DAILY_RECIPIENTS))
        self.assertEqual(daily.get("reportTime"), "07:30")
        self.assertEqual(daily.get("theme"), "steel",
                         "daily report theme was not synced from email_config.json")
        alert = rows[NEW_ALERT_ID]
        self.assertEqual(alert.get("recipients"), ALERT_RECIPIENT)
        self.assertEqual(alert.get("trigger"), "warning",
                         "alert trigger was not synced from email_config.json")
        self.assertEqual(alert.get("intervalMinutes"), 240,
                         "alert interval was not synced from email_config.json")
        self.assertEqual(alert.get("quietStart"), "07:00",
                         "quiet hours (not stored in email config) must be preserved")

        log_text = (Path(self.tmp.name) / "server-stdout.log").read_text(
            encoding="utf-8", errors="replace"
        )
        self.assertIn(
            f"reconcile: dropped stale daily_report schedule {OLD_DAILY_ID} "
            f"superseded by {NEW_DAILY_ID}", log_text,
        )
        self.assertIn(
            f"reconcile: dropped stale alert schedule {OLD_ALERT_ID} "
            f"superseded by {NEW_ALERT_ID}", log_text,
        )

        # The healed + synced store is persisted back to disk.
        automations_file = Path(self.tmp.name) / "data" / "automations.json"
        healed = {}
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            healed = json.loads(automations_file.read_text(encoding="utf-8"))
            if sorted(healed) == sorted([NEW_DAILY_ID, NEW_ALERT_ID]):
                break
            time.sleep(0.5)
        self.assertEqual(sorted(healed), sorted([NEW_DAILY_ID, NEW_ALERT_ID]))
        self.assertEqual(healed[NEW_DAILY_ID]["recipients"], DAILY_RECIPIENTS)
        self.assertEqual(healed[NEW_DAILY_ID]["theme"], "steel")
        self.assertEqual(healed[NEW_ALERT_ID]["recipients"], [ALERT_RECIPIENT])
        self.assertEqual(healed[NEW_ALERT_ID]["trigger"], "warning")
        self.assertEqual(healed[NEW_ALERT_ID]["interval_minutes"], 240)
        self.assertEqual(healed[NEW_ALERT_ID]["quiet_start"], "07:00")

        # ── 2) Profile isolation: named profiles survive reconciliation; only
        # the legacy (config-driven) schedule is the per-type singleton. ─────
        self._teardown_proc()
        now = time.time()
        profile_a_id = f"{SESSION_A}:profa001"
        profile_b_id = f"{SESSION_A}:profb001"
        legacy_id = f"{SESSION_OLD}:1e9acy01"
        records = {
            profile_a_id: _legacy_record(
                profile_a_id, SESSION_A, "alert", ["profile-a@example.com"],
                now - 3000.0, profile_name="alpha",
            ),
            profile_b_id: _legacy_record(
                profile_b_id, SESSION_A, "alert", ["profile-b@example.com"],
                now - 2000.0, profile_name="beta",
            ),
            legacy_id: _legacy_record(
                legacy_id, SESSION_OLD, "alert", ["alert-old@example.com"],
                now - 4000.0,
            ),
        }
        automations_file.write_text(json.dumps(records), encoding="utf-8")
        self._boot()
        cookie, token = self._login()
        rows = self._wait_for_schedule_ids(
            cookie, token, [profile_a_id, profile_b_id, legacy_id]
        )
        self.assertEqual(rows[profile_a_id].get("recipients"), "profile-a@example.com")
        self.assertEqual(rows[profile_b_id].get("recipients"), "profile-b@example.com")
        # The config-driven survivor syncs to the saved alert config.
        self.assertEqual(rows[legacy_id].get("recipients"), ALERT_RECIPIENT)
        self.assertEqual(rows[legacy_id].get("trigger"), "warning")

        # ── 3) Arm-time singleton: starting a config-driven alert replaces
        # EVERY config-driven alert regardless of recipients; profiles stay. ─
        def _start_payload(session_id, recipient):
            return {
                "action": "start",
                "sessionId": session_id,
                "smtpHost": "127.0.0.1",
                "smtpPort": str(SMTP_PORT),
                "smtpSecurity": "none",
                "smtpUsername": "",
                "smtpPassword": "",
                "smtpFrom": "dashboard@example.com",
                "smtpTo": recipient,
                "scheduleType": "alert",
                "intervalMinutes": "60",
                "trigger": "critical",
                "reportTime": "08:00",
                "quietStart": "",
                "quietEnd": "",
                "digest": True,
                "theme": "default",
            }

        status, data = self._automation_post(
            cookie, token, _start_payload(SESSION_A, "x@example.com")
        )
        self.assertEqual(status, 200, data)
        x_id = data.get("automationId", "")
        self.assertTrue(x_id)
        rows = self._list_schedules(cookie, token)
        self.assertEqual(sorted(rows), sorted([profile_a_id, profile_b_id, x_id]),
                         "start must replace the legacy config-driven alert")

        status, data = self._automation_post(
            cookie, token, _start_payload(SESSION_B, "y@example.com")
        )
        self.assertEqual(status, 200, data)
        y_id = data.get("automationId", "")
        self.assertTrue(y_id)
        rows = self._list_schedules(cookie, token)
        self.assertEqual(
            sorted(rows), sorted([profile_a_id, profile_b_id, y_id]),
            "a second config-driven alert with different recipients must "
            f"replace the first, not join it: {sorted(rows)}",
        )
        self.assertEqual(rows[y_id].get("recipients"), "y@example.com")
        self.assertEqual(rows[y_id].get("profileName"), "")
        self.assertEqual(rows[profile_a_id].get("recipients"), "profile-a@example.com")
        self.assertEqual(rows[profile_b_id].get("recipients"), "profile-b@example.com")


if __name__ == "__main__":
    unittest.main()
