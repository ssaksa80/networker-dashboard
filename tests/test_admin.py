"""Admin console back end (nwdash/admin.py) and the login-session table it reads.

Unit level: every file the code touches is patched to a temp dir, and the
runtime flags (_cfg.AUTH_ENABLED, ALLOWLIST_SOURCE, ...) are patched per test.
The HTTP routes are exercised end to end in test_admin_http.py.
"""
import http.client
import json
import tempfile
import time
import unittest
from http import HTTPStatus
from pathlib import Path
from unittest import mock

from nwdash import admin, auth, models, profiles, snapshots
from nwdash import config as _cfg
from nwdash.certs import write_embedded_dev_certificate


class TempDataMixin:
    """A throwaway data/ and logs/ for the admin, auth and UI-prefs files."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        sessions = auth._LoginSessions()
        patches = [
            (auth, "DATA_DIR", self.dir),
            (auth, "AUTH_SESSIONS_FILE", self.dir / "auth_sessions.json"),
            (auth, "AUTH_CONFIG_FILE", self.dir / "auth.json"),
            (auth, "LOGIN_SESSIONS", sessions),
            (admin, "LOGIN_SESSIONS", sessions),
            (admin, "DATA_DIR", self.dir),
            (admin, "AUDIT_FILE", self.dir / "audit.jsonl"),
            (admin, "CONSOLE_ALLOWLIST_FILE", self.dir / "allowed_hosts.json"),
            (admin, "LOG_FILE", self.dir / "networker_dashboard.log"),
            (admin, "SETUP_CODE_FILE", self.dir / "setup-code.txt"),
            (admin, "_setup_code", ""),
            (snapshots, "DATA_DIR", self.dir),
            (snapshots, "UI_PREFS_FILE", self.dir / "ui_prefs.json"),
            (_cfg, "AUTH_ENABLED", True),
            (_cfg, "ALLOWLIST_SOURCE", "none"),
            (_cfg, "AUTH_PASSWORD_FROM_STARTUP", False),
        ]
        for target, name, value in patches:
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.sessions = sessions
        auth.LOGIN_ATTEMPTS.clear()
        self.addCleanup(auth.LOGIN_ATTEMPTS.clear)

    def audit_rows(self):
        return admin.read_audit(limit=1000)


class TestLoginSessionMetadata(TempDataMixin, unittest.TestCase):
    def test_login_keeps_address_and_browser(self):
        cookie = auth._make_auth_cookie("192.0.2.41", "Mozilla/5.0 (Windows NT 10.0) Edg/141.0")
        sid = auth.session_id_of_cookie(cookie)
        [(stored_sid, entry)] = self.sessions.entries()
        self.assertEqual(stored_sid, sid)
        self.assertEqual(entry["ip"], "192.0.2.41")
        self.assertIn("Edg/", entry["agent"])
        self.assertGreater(entry["iat"], 0)

    def test_files_from_before_the_metadata_still_load(self):
        (self.dir / "auth_sessions.json").write_text(json.dumps({"old-sid": int(time.time()) + 600}), encoding="utf-8")
        fresh = auth._LoginSessions()
        self.assertIn("old-sid", fresh)
        [(_sid, entry)] = fresh.entries()
        self.assertEqual(entry["ip"], "")

    def test_discard_all_except_keeps_only_the_named_login(self):
        keep = auth.session_id_of_cookie(auth._make_auth_cookie())
        auth._make_auth_cookie()
        auth._make_auth_cookie()
        self.assertEqual(self.sessions.discard_all_except(keep), 2)
        self.assertEqual([sid for sid, _ in self.sessions.entries()], [keep])

    def test_admin_window_opens_closes_and_ends_with_the_login(self):
        sid = auth.session_id_of_cookie(auth._make_auth_cookie())
        self.assertEqual(self.sessions.admin_until(sid), 0)
        self.assertGreater(self.sessions.unlock_admin(sid, 60), time.time())
        self.assertGreater(self.sessions.admin_until(sid), 0)
        self.sessions.lock_admin(sid)
        self.assertEqual(self.sessions.admin_until(sid), 0)
        self.sessions.unlock_admin(sid, 60)
        self.sessions.discard(sid)
        self.assertEqual(self.sessions.admin_until(sid), 0)
        self.assertEqual(self.sessions.unlock_admin("not-a-login", 60), 0)

    def test_admin_window_expires(self):
        sid = auth.session_id_of_cookie(auth._make_auth_cookie())
        self.sessions.unlock_admin(sid, -1)
        self.assertEqual(self.sessions.admin_until(sid), 0)

    def test_admin_window_is_not_written_to_disk(self):
        sid = auth.session_id_of_cookie(auth._make_auth_cookie())
        self.sessions.unlock_admin(sid, 600)
        self.assertNotIn("admin", (self.dir / "auth_sessions.json").read_text(encoding="utf-8"))
        self.assertEqual(auth._LoginSessions().admin_until(sid), 0)


class TestConsoleAccess(TempDataMixin, unittest.TestCase):
    def test_password_set_means_sign_in(self):
        self.assertEqual(admin.console_access("192.0.2.41", True), "ok")
        self.assertEqual(admin.console_access("192.0.2.41", False), "login")

    def test_no_password_means_this_server_only(self):
        with mock.patch.object(_cfg, "AUTH_ENABLED", False):
            self.assertEqual(admin.console_access("127.0.0.1", True), "open")
            self.assertEqual(admin.console_access("::1", True), "open")
            self.assertEqual(admin.console_access("192.0.2.41", True), "denied")
            # Being on loopback is not enough: the setup code stands in for a password.
            self.assertFalse(admin.admin_window_open(""))
            code, _written = admin.prepare_setup_code()
            self.assertTrue(admin.admin_window_open("", code))

    def test_window_is_needed_when_a_password_is_set(self):
        sid = auth.session_id_of_cookie(auth._make_auth_cookie())
        self.assertFalse(admin.admin_window_open(sid))
        self.assertFalse(admin.admin_window_open(""))

    def test_unlock_needs_the_right_password_and_is_rate_limited(self):
        auth.set_auth_password("correct horse battery")
        sid = auth.session_id_of_cookie(auth._make_auth_cookie())
        status, _body = admin.unlock(sid, "wrong", "192.0.2.41")
        self.assertEqual(status, HTTPStatus.UNAUTHORIZED)
        self.assertFalse(admin.admin_window_open(sid))
        status, _body = admin.unlock(sid, "correct horse battery", "192.0.2.41")
        self.assertEqual(status, HTTPStatus.OK)
        self.assertTrue(admin.admin_window_open(sid))
        self.assertEqual([r["action"] for r in self.audit_rows()], ["Admin unlocked", "Admin unlock failed"])
        for _ in range(_cfg.LOGIN_MAX_ATTEMPTS):
            admin.unlock(sid, "wrong", "198.51.100.7")
        status, _body = admin.unlock(sid, "correct horse battery", "198.51.100.7")
        self.assertEqual(status, HTTPStatus.TOO_MANY_REQUESTS)

    def test_touch_extends_an_open_window_only(self):
        sid = auth.session_id_of_cookie(auth._make_auth_cookie())
        admin.touch_admin_window(sid)
        self.assertFalse(admin.admin_window_open(sid))
        self.sessions.unlock_admin(sid, 5)
        admin.touch_admin_window(sid)
        self.assertGreater(self.sessions.admin_until(sid), time.time() + 60)


class TestNoPasswordLocality(TempDataMixin, unittest.TestCase):
    """With no password, "on the server itself" must survive a reverse proxy on
    the same host, which connects from loopback too."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(_cfg, "AUTH_ENABLED", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _headers(**values):
        message = http.client.HTTPMessage()  # what the request handler passes
        for name, value in values.items():
            message[name.replace("_", "-")] = value
        return message

    def test_any_proxy_header_makes_a_loopback_request_remote(self):
        self.assertTrue(admin.is_local_request("127.0.0.1", self._headers(Host="127.0.0.1:8443")))
        for name in sorted(admin._PROXY_HEADERS):
            for spelling in (name, name.upper(), name.title()):
                with self.subTest(header=spelling):
                    headers = self._headers(**{spelling: "192.0.2.9"})
                    self.assertFalse(admin.is_local_request("127.0.0.1", headers))
                    self.assertEqual(admin.console_access("127.0.0.1", False, headers), "denied")
        self.assertFalse(admin.is_local_request("192.0.2.41", self._headers()))

    def test_setup_code_is_written_checked_and_one_time(self):
        code, written = admin.prepare_setup_code()
        self.assertTrue(written)
        self.assertEqual((self.dir / "setup-code.txt").read_text(encoding="ascii").strip(), code)
        self.assertRegex(code, r"^[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}$")
        self.assertTrue(admin.setup_code_ok(code))
        self.assertTrue(admin.setup_code_ok(code.lower().replace("-", " ")))  # as typed
        self.assertFalse(admin.setup_code_ok(""))
        self.assertFalse(admin.setup_code_ok("AAAA-AAAA-AAAA"))
        newer, _ = admin.prepare_setup_code()  # every start makes a new one
        if newer != code:  # 1 in 2**60 they match
            self.assertFalse(admin.setup_code_ok(code))
        admin.clear_setup_code()
        self.assertFalse(admin.setup_code_ok(newer))
        self.assertFalse((self.dir / "setup-code.txt").exists())

    def test_first_password_needs_this_server_and_the_code(self):
        code, _ = admin.prepare_setup_code()
        proxied = self._headers(X_Forwarded_For="192.0.2.9")
        status, _body = admin.first_password("a long enough password", "127.0.0.1", proxied, code)
        self.assertEqual(status, HTTPStatus.FORBIDDEN)
        status, body = admin.first_password("a long enough password", "127.0.0.1", self._headers(), "")
        self.assertEqual((status, body["code"]), (HTTPStatus.FORBIDDEN, "setup_code_required"))
        self.assertFalse(_cfg.AUTH_ENABLED)
        status, _body = admin.first_password("a long enough password", "127.0.0.1", self._headers(), code)
        self.assertEqual(status, HTTPStatus.OK)
        self.assertTrue(_cfg.AUTH_ENABLED)
        self.assertTrue(auth.verify_auth_password("a long enough password"))
        self.assertFalse((self.dir / "setup-code.txt").exists(), "the code is one-time")

    def test_guessing_the_code_locks_the_address_out(self):
        code, _ = admin.prepare_setup_code()
        for _ in range(_cfg.LOGIN_MAX_ATTEMPTS):
            status, _body = admin.first_password("a long enough password", "127.0.0.1", self._headers(), "WRNG-WRNG-WRNG")
            self.assertEqual(status, HTTPStatus.FORBIDDEN)
        status, _body = admin.first_password("a long enough password", "127.0.0.1", self._headers(), code)
        self.assertEqual(status, HTTPStatus.TOO_MANY_REQUESTS)
        self.assertFalse(_cfg.AUTH_ENABLED)


class TestAudit(TempDataMixin, unittest.TestCase):
    def test_entries_are_newest_first_and_filterable(self):
        admin.audit("Theme changed", ip="192.0.2.41", item="appearance", field="theme", old="default", new="midnight")
        admin.audit("Profile saved", ip="192.0.2.57", item="DR site")
        rows = self.audit_rows()
        self.assertEqual([r["action"] for r in rows], ["Profile saved", "Theme changed"])
        self.assertEqual(rows[1]["old"], "default")
        self.assertEqual([r["action"] for r in admin.read_audit(action="Theme changed")], ["Theme changed"])
        self.assertEqual([r["item"] for r in admin.read_audit(query="192.0.2.57")], ["DR site"])
        today = rows[0]["ts"][:10]
        self.assertEqual(len(admin.read_audit(date=today)), 2)
        self.assertEqual(admin.read_audit(date="1999-01-01"), [])
        self.assertEqual(admin.audit_actions(), ["Profile saved", "Theme changed"])

    def test_session_is_named_without_revealing_its_id(self):
        admin.audit("Signed in", sid="0123456789abcdef0123456789abcdef")
        entry = self.audit_rows()[0]
        self.assertTrue(entry["session"])
        self.assertNotIn("0123456789abcdef", json.dumps(entry))

    def test_values_are_clipped_and_kept_on_one_line(self):
        admin.audit("Note", new="line one\nline two " + "x" * 1000)
        value = self.audit_rows()[0]["new"]
        self.assertNotIn("\n", value)
        self.assertLessEqual(len(value), 300)

    def test_file_rotates_and_both_halves_are_read(self):
        # Each line is ~115 bytes: the 7th write finds the file over 600 and
        # moves it to audit.jsonl.1, so all twelve lines stay readable.
        with mock.patch.object(admin, "AUDIT_MAX_BYTES", 600):
            for i in range(12):
                admin.audit("Step", item=f"item {i}")
        self.assertTrue((self.dir / "audit.jsonl.1").exists())
        items = [r["item"] for r in self.audit_rows()]
        self.assertEqual(items, [f"item {i}" for i in range(11, -1, -1)])

    def test_a_failed_write_does_not_raise(self):
        with mock.patch.object(admin, "AUDIT_FILE", self.dir / "missing" / "dir" / "audit.jsonl"), \
             mock.patch.object(admin, "DATA_DIR", self.dir / "file-not-dir"), \
             self.assertLogs(admin.LOG, "WARNING"):
            (self.dir / "file-not-dir").write_text("x", encoding="utf-8")
            admin.audit("Anything")


class TestPassword(TempDataMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        auth.set_auth_password("old password 123")
        self.sid = auth.session_id_of_cookie(auth._make_auth_cookie())
        auth._make_auth_cookie()  # another browser

    def test_wrong_current_password_is_refused_and_audited(self):
        status, _body = admin.change_password(self.sid, "nope", "new password 456", "192.0.2.41")
        self.assertEqual(status, HTTPStatus.UNAUTHORIZED)
        self.assertTrue(auth.verify_auth_password("old password 123"))
        self.assertEqual(self.audit_rows()[0]["action"], "Password change refused")

    def test_short_password_is_refused(self):
        status, body = admin.change_password(self.sid, "old password 123", "short", "192.0.2.41")
        self.assertEqual(status, HTTPStatus.BAD_REQUEST)
        self.assertIn("12 characters", body["error"])

    def test_change_signs_out_everyone_else(self):
        status, body = admin.change_password(self.sid, "old password 123", "new password 456", "192.0.2.41")
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(body["sessionsEnded"], 1)
        self.assertTrue(auth.verify_auth_password("new password 456"))
        self.assertEqual([sid for sid, _ in self.sessions.entries()], [self.sid])
        self.assertNotIn("new password 456", (self.dir / "audit.jsonl").read_text(encoding="utf-8"))

    def test_startup_password_warning(self):
        with mock.patch.object(_cfg, "AUTH_PASSWORD_FROM_STARTUP", True):
            _status, body = admin.change_password(self.sid, "old password 123", "new password 456", "192.0.2.41")
        self.assertIn("DASHBOARD_AUTH_PASSWORD", body["warning"])


class TestFirstPassword(TempDataMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(_cfg, "AUTH_ENABLED", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_only_from_the_server_itself(self):
        status, _body = admin.first_password("a good long password", "192.0.2.41")
        self.assertEqual(status, HTTPStatus.FORBIDDEN)
        self.assertFalse(_cfg.AUTH_ENABLED)

    def test_sets_the_password_and_turns_sign_in_on(self):
        code, _ = admin.prepare_setup_code()
        status, _body = admin.first_password("a good long password", "127.0.0.1", None, code)
        self.assertEqual(status, HTTPStatus.OK)
        self.assertTrue(_cfg.AUTH_ENABLED)
        self.assertTrue(auth.verify_auth_password("a good long password"))
        status, _body = admin.first_password("another long password", "127.0.0.1", None, code)
        self.assertEqual(status, HTTPStatus.CONFLICT)

    def test_weak_password_is_refused(self):
        code, _ = admin.prepare_setup_code()
        status, _body = admin.first_password("short", "127.0.0.1", None, code)
        self.assertEqual(status, HTTPStatus.BAD_REQUEST)
        self.assertFalse(_cfg.AUTH_ENABLED)


class TestAllowList(TempDataMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(profiles.configure_allowed_hosts, "")

    def test_entries_are_validated(self):
        for bad in (["has space.example.test"], ["host:1234"], ["a" * 300], "not-a-list"):
            status, _body = admin.save_allowlist(bad, "", "127.0.0.1")
            self.assertEqual(status, HTTPStatus.BAD_REQUEST, bad)

    def test_save_applies_now_and_survives_a_restart(self):
        with mock.patch.object(profiles, "_resolve_ips", return_value=set()):
            status, body = admin.save_allowlist(["NWSERVER01.example.test", "198.51.100.0/24", "198.51.100.0/24"], "", "127.0.0.1")
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(body["entries"], ["nwserver01.example.test", "198.51.100.0/24"])
        self.assertTrue(_cfg.ALLOWLIST_ENABLED)
        self.assertEqual(_cfg.ALLOWLIST_SOURCE, "console")
        self.assertEqual(admin.load_console_allowlist(), "nwserver01.example.test,198.51.100.0/24")
        self.assertEqual(self.audit_rows()[0]["action"], "Allow-list changed")

    def test_empty_list_turns_it_off(self):
        admin.save_allowlist(["198.51.100.0/24"], "", "127.0.0.1")
        status, body = admin.save_allowlist([], "", "127.0.0.1")
        self.assertEqual(status, HTTPStatus.OK)
        self.assertFalse(body["enabled"])
        self.assertEqual(_cfg.ALLOWLIST_SOURCE, "none")

    def test_a_startup_list_is_read_only(self):
        with mock.patch.object(_cfg, "ALLOWLIST_SOURCE", "startup"):
            status, _body = admin.save_allowlist(["198.51.100.0/24"], "", "127.0.0.1")
            self.assertEqual(status, HTTPStatus.CONFLICT)
            self.assertFalse(admin.allowlist_view()["editable"])

    def test_a_damaged_file_is_ignored(self):
        (self.dir / "allowed_hosts.json").write_text('{"entries": ["bad host"]}', encoding="utf-8")
        with self.assertLogs(admin.LOG, "WARNING"):
            self.assertEqual(admin.load_console_allowlist(), "")


class TestSessionsAndShares(TempDataMixin, unittest.TestCase):
    def test_list_and_revoke_sessions(self):
        mine = auth.session_id_of_cookie(auth._make_auth_cookie("127.0.0.1", "Edg/141"))
        auth._make_auth_cookie("192.0.2.57", "Chrome/141")
        rows = admin.list_sessions(mine)
        self.assertEqual(len(rows), 2)
        self.assertNotIn(mine, json.dumps(rows))
        own = next(r for r in rows if r["current"])
        other = next(r for r in rows if not r["current"])
        status, _body = admin.revoke_session(own["id"], mine, "127.0.0.1")
        self.assertEqual(status, HTTPStatus.BAD_REQUEST)
        status, _body = admin.revoke_session(other["id"], mine, "127.0.0.1")
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(len(admin.list_sessions(mine)), 1)
        status, _body = admin.revoke_session(other["id"], mine, "127.0.0.1")
        self.assertEqual(status, HTTPStatus.NOT_FOUND)

    def test_share_links_are_listed_without_their_token(self):
        with mock.patch.dict(models.SHARE_TOKENS, clear=True):
            token = models.create_share_token("session-a")
            models.create_share_token("session-b")
            rows = admin.list_share_links()
            self.assertEqual(len(rows), 2)
            self.assertNotIn(token, json.dumps(rows))
            ref = next(r["id"] for r in rows if r["hint"].startswith(token[:4]))
            self.assertEqual(admin.revoke_share_link(ref, "", "127.0.0.1")[0], HTTPStatus.OK)
            self.assertIsNone(models.validate_share_token(token))
            self.assertEqual(admin.revoke_all_share_links("", "127.0.0.1")["revoked"], 1)
            self.assertEqual(admin.list_share_links(), [])


class TestLogsAndTls(TempDataMixin, unittest.TestCase):
    def test_log_tail_filters_by_level_and_text(self):
        lines = [
            {"ts": "2026-10-27T02:40:00+00:00", "level": "DEBUG", "msg": "noise"},
            {"ts": "2026-10-27T02:40:01+00:00", "level": "INFO", "msg": "shared refresh ok", "client": "192.0.2.41"},
            {"ts": "2026-10-27T02:40:02+00:00", "level": "ERROR", "msg": "SMTP send failed", "exc": "Traceback"},
        ]
        (self.dir / "networker_dashboard.log").write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n", encoding="utf-8")
        info = admin.read_log("INFO")
        self.assertEqual([x["level"] for x in info["lines"]], ["INFO", "ERROR", "INFO"])
        self.assertIn("Traceback", info["lines"][1]["msg"])
        self.assertEqual([x["msg"] for x in admin.read_log("ERROR")["lines"]], ["SMTP send failed\nTraceback"])
        self.assertEqual(len(admin.read_log("DEBUG", query="smtp")["lines"]), 1)

    def test_missing_log_file(self):
        self.assertTrue(admin.read_log()["missing"])

    def test_tls_info_reads_the_serving_certificate(self):
        cert, key = self.dir / "cert.pem", self.dir / "key.pem"
        write_embedded_dev_certificate(cert, key)
        with mock.patch.object(_cfg, "TLS_CERT_FILE", str(cert)):
            info = admin.tls_info()
        self.assertTrue(info["ok"])
        self.assertRegex(info["sha256"], r"^([0-9A-F]{2}:){31}[0-9A-F]{2}$")
        self.assertNotIn("PRIVATE", json.dumps(info))
        if "subject" in info:  # the decoder is private API; tolerate its absence
            self.assertIsInstance(info["daysLeft"], int)

    def test_tls_info_without_a_certificate(self):
        with mock.patch.object(_cfg, "TLS_CERT_FILE", str(self.dir / "absent.pem")):
            self.assertFalse(admin.tls_info()["ok"])

    def test_embedded_runtime_is_the_one_with_a_pth_file(self):
        # The bundle's python.org embeddable runtime keeps python312._pth
        # beside python.exe; a regular install does not.
        exe = self.dir / "python.exe"
        with mock.patch.object(admin.sys, "executable", str(exe)):
            self.assertFalse(admin.embedded_runtime())
            (self.dir / "python312._pth").write_text("python312.zip\n.\n", encoding="utf-8")
            self.assertTrue(admin.embedded_runtime())


class TestSelfTest(TempDataMixin, unittest.TestCase):
    def test_steps_and_outcome(self):
        with mock.patch.object(admin, "load_profiles", return_value={}), \
             mock.patch.object(admin, "load_email_config", return_value={}), \
             mock.patch.object(_cfg, "TLS_CERT_FILE", ""):
            result = admin.run_self_test(lambda: (True, "healthy"))
        states = {step["id"]: step["state"] for step in result["steps"]}
        self.assertEqual(states["cert"], "bad")
        self.assertEqual(states["listener"], "ok")
        self.assertEqual(states["data"], "ok")
        self.assertEqual(states["networker"], "warn")
        self.assertEqual(states["smtp"], "warn")
        self.assertFalse(result["passed"])
        self.assertEqual(result["failures"], 1)

    def test_unreachable_profile_host_fails_the_step(self):
        record = {"restApiHost": "nwserver01.example.test", "restApiPort": 9090}
        with mock.patch.object(admin, "load_profiles", return_value={"Primary DC": record}), \
             mock.patch.object(admin, "load_email_config", return_value={}), \
             mock.patch.object(admin, "_host_allowed", return_value=True), \
             mock.patch.object(admin, "_tcp_reachable", return_value=(False, "timed out")):
            result = admin.run_self_test(lambda: (True, "healthy"))
        step = next(s for s in result["steps"] if s["id"] == "networker")
        self.assertEqual(step["state"], "bad")
        self.assertIn("Primary DC", step["detail"])


class TestUiPrefs(TempDataMixin, unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(snapshots.load_ui_prefs(), {"theme": "default", "design": "daylight", "motion": "full", "sidebar": "dock"})

    def test_theme_and_design_are_merged_not_overwritten(self):
        snapshots.save_ui_prefs({"design": "aurora", "sidebar": "float"})
        snapshots.save_ui_theme("midnight")
        self.assertEqual(snapshots.load_ui_prefs(), {"theme": "midnight", "design": "aurora", "motion": "full", "sidebar": "float"})
        self.assertEqual(snapshots.load_ui_theme(), "midnight")

    def test_unknown_values_are_refused(self):
        with self.assertRaises(models.BadRequest):
            snapshots.save_ui_prefs({"design": "neon"})
        with self.assertRaises(models.BadRequest):
            snapshots.save_ui_prefs({"motion": "fast"})


if __name__ == "__main__":
    unittest.main()
