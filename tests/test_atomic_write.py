"""Saving through a briefly held file (review O5).

On Windows os.replace fails while another process holds the target (antivirus,
a backup agent, a reader), and most writers swallowed that, so sessions and
schedules were silently not saved. nwdash.fileio retries the replace a bounded
number of times and logs the final failure, and every writer of data/ goes
through it.
"""
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from nwdash import admin, auth, emailer, fileio, models, profiles, secrets, sessions, snapshots
from nwdash import config as _cfg
from nwdash.config import LOG


def held(*failures):
    """A stand-in for os.replace that raises each of `failures` in turn, then
    replaces for real. Records every call."""
    real = os.replace
    queue = list(failures)
    calls = []

    def replace(src, dst):
        calls.append((Path(src), Path(dst)))
        if queue:
            raise queue.pop(0)
        real(src, dst)
    replace.calls = calls
    return replace


def denied(path="target"):
    return PermissionError(13, "The process cannot access the file because it is being used by another process", str(path))


class TempDir(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="nwdash-atomic-")
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        sleep = mock.patch("nwdash.fileio.time.sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)

    def leftovers(self):
        return sorted(p.name for p in self.dir.rglob("*.tmp"))


class TestReplaceWithRetry(TempDir):
    def setUp(self):
        super().setUp()
        self.src, self.dst = self.dir / "new.json", self.dir / "live.json"
        self.src.write_text("new", encoding="utf-8")
        self.dst.write_text("old", encoding="utf-8")

    def test_a_held_target_is_tried_again_until_it_is_free(self):
        replace = held(denied(), denied())
        with mock.patch("nwdash.fileio.os.replace", replace):
            fileio.replace_with_retry(self.src, self.dst)
        self.assertEqual(self.dst.read_text(encoding="utf-8"), "new")
        self.assertEqual(len(replace.calls), 3)
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], list(fileio.REPLACE_RETRY_DELAYS[:2]))

    def test_it_gives_up_after_a_bounded_number_of_tries(self):
        tries = len(fileio.REPLACE_RETRY_DELAYS) + 1
        replace = held(*[denied() for _ in range(tries)])
        with mock.patch("nwdash.fileio.os.replace", replace), self.assertRaises(PermissionError):
            fileio.replace_with_retry(self.src, self.dst)
        self.assertEqual(len(replace.calls), tries)
        self.assertLess(sum(fileio.REPLACE_RETRY_DELAYS), 3)  # a request thread waits this long at most
        self.assertEqual(self.dst.read_text(encoding="utf-8"), "old")

    def test_errors_that_waiting_cannot_fix_are_raised_at_once(self):
        with self.assertRaises(FileNotFoundError):
            fileio.replace_with_retry(self.dir / "missing.json", self.dst)
        self.sleep.assert_not_called()


class TestAtomicWrite(TempDir):
    def test_it_writes_exactly_the_text_and_leaves_no_temporary_file(self):
        target = self.dir / "sub" / "state.json"
        fileio.atomic_write_text(target, '{"a":\n1}')
        self.assertEqual(target.read_bytes(), b'{"a":\n1}')  # no newline translation
        self.assertEqual(self.leftovers(), [])

    def test_a_held_file_is_saved_once_it_is_let_go(self):
        target = self.dir / "sessions.json"
        target.write_text("old", encoding="utf-8")
        with mock.patch("nwdash.fileio.os.replace", held(denied(target))):
            fileio.atomic_write_text(target, "new")
        self.assertEqual(target.read_text(encoding="utf-8"), "new")
        self.assertEqual(self.leftovers(), [])

    def test_a_final_failure_is_logged_and_raised_and_the_old_file_kept(self):
        target = self.dir / "automations.json"
        target.write_text("old", encoding="utf-8")
        failures = [denied(target) for _ in range(len(fileio.REPLACE_RETRY_DELAYS) + 1)]
        with mock.patch("nwdash.fileio.os.replace", held(*failures)), \
             self.assertLogs(LOG, "WARNING") as logs, self.assertRaises(PermissionError):
            fileio.atomic_write_text(target, "new")
        self.assertIn("could not save automations.json", "\n".join(logs.output))
        self.assertEqual(target.read_text(encoding="utf-8"), "old")
        self.assertEqual(self.leftovers(), [])

    def test_writers_saving_the_same_file_at_once_do_not_trip_over_each_other(self):
        target = self.dir / "ui_prefs.json"
        errors = []

        def write(n):
            try:
                for i in range(25):
                    fileio.atomic_write_text(target, json.dumps({"writer": n, "i": i}))
            except OSError as exc:  # pragma: no cover - the failure being tested for
                errors.append(exc)
        with mock.patch("nwdash.fileio.time.sleep", lambda s: threading.Event().wait(s)):
            threads = [threading.Thread(target=write, args=(n,)) for n in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(errors, [])
        self.assertIn(json.loads(target.read_text(encoding="utf-8"))["writer"], range(4))
        self.assertEqual(self.leftovers(), [])

    @unittest.skipUnless(os.name == "nt", "Windows refuses to replace a file another handle holds open")
    def test_a_reader_holding_the_file_on_windows_no_longer_loses_the_save(self):
        target = self.dir / "sessions.json"
        target.write_text("old", encoding="utf-8")
        other = self.dir / "other.json"
        other.write_text("x", encoding="utf-8")
        handle = target.open("rb")  # Python opens without delete sharing, as most readers do
        try:
            with self.assertRaises(PermissionError):
                os.replace(other, target)  # what every writer used to try once
        finally:
            threading.Timer(0.2, handle.close).start()
        with mock.patch("nwdash.fileio.time.sleep", lambda s: threading.Event().wait(s)):
            fileio.atomic_write_text(target, "new")
        self.assertEqual(target.read_text(encoding="utf-8"), "new")


class TestEveryWriterUsesIt(TempDir):
    """The data/ files, with every replace refused: a writer that carries on
    without the file says so in the log, and one that reports failure still
    does. Either way the file on disk is the old one, whole."""

    def setUp(self):
        super().setUp()
        files = {
            (sessions, "SESSION_PERSISTENCE_FILE"): "sessions.json",
            (emailer, "AUTOMATIONS_FILE"): "automations.json",
            (models, "LAST_GOOD_DASHBOARD_FILE"): "last_good_dashboard.json",
            (snapshots, "EMAIL_CONFIG_FILE"): "email_config.json",
            (snapshots, "EMAIL_PROFILES_FILE"): "email_profiles.json",
            (snapshots, "DASHBOARD_SNAPSHOT_FILE"): "networker_snapshots.json",
            (snapshots, "AUTO_SNAPSHOT_FILE"): "auto_snapshot_config.json",
            (snapshots, "UI_PREFS_FILE"): "ui_prefs.json",
            (auth, "AUTH_CONFIG_FILE"): "auth.json",
            (auth, "AUTH_SESSIONS_FILE"): "auth_sessions.json",
            (profiles, "PROFILES_FILE"): "profiles.json",
            (admin, "CONSOLE_ALLOWLIST_FILE"): "allowed_hosts.json",
        }
        patches = [mock.patch.object(module, attr, self.dir / name) for (module, attr), name in files.items()]
        patches += [mock.patch.object(module, "DATA_DIR", self.dir)
                    for module in (emailer, models, snapshots, auth, profiles, admin)]
        patches += [
            mock.patch.object(sessions, "WMI_CIPHER", object()),
            mock.patch.object(emailer, "WMI_CIPHER", object()),
            mock.patch.object(_cfg, "ALLOWLIST_SOURCE", "none"),
            mock.patch("nwdash.fileio.os.replace", side_effect=denied()),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in files.values():
            (self.dir / name).write_text('{"old": true}', encoding="utf-8")

    def assert_old(self, name):
        self.assertEqual((self.dir / name).read_text(encoding="utf-8"), '{"old": true}', name)
        self.assertEqual(self.leftovers(), [])

    def test_writers_that_carry_on_log_the_lost_save(self):
        cases = {
            "sessions.json": sessions.persist_sessions,
            "automations.json": emailer.persist_automations,
            "last_good_dashboard.json": lambda: models.persist_last_good_dashboard("sid", {"summary": {}}),
            "email_config.json": lambda: snapshots.write_email_config({"smtpServer": "mail.example.test"}),
            "email_profiles.json": lambda: snapshots.write_email_profiles({"p": {}}),
            "auth_sessions.json": lambda: auth._LoginSessions()._save({}),
        }
        for name, write in cases.items():
            with self.subTest(name), self.assertLogs(LOG, "WARNING") as logs:
                write()
                self.assertIn(f"could not save {name}", "\n".join(logs.output))
                self.assert_old(name)

    def test_writers_that_report_failure_still_raise(self):
        cases = {
            "networker_snapshots.json": lambda: snapshots.write_dashboard_snapshots({}),
            # These two wrote the live file directly: a failure part-way left it cut short.
            "auto_snapshot_config.json": lambda: snapshots.save_auto_snapshot_config(True),
            "ui_prefs.json": lambda: snapshots.save_ui_prefs({"design": "aurora"}),
            "auth.json": lambda: auth.set_auth_password("a password 123"),
            "profiles.json": lambda: profiles.save_profiles({}),
        }
        for name, write in cases.items():
            with self.subTest(name), self.assertLogs(LOG, "WARNING"), self.assertRaises(OSError):
                write()
            self.assert_old(name)

    def test_the_console_allow_list_reports_the_failure(self):
        with self.assertLogs(LOG, "WARNING"):
            status, body = admin.save_allowlist(["192.0.2.10"], "s1", "127.0.0.1")
        self.assertEqual(status, 500)
        self.assertIn("could not be saved", body["error"])
        self.assert_old("allowed_hosts.json")

    def test_a_key_that_cannot_be_saved_still_stops_the_start(self):
        key = self.dir / ".session_key"
        key.write_bytes(b"old key")
        with self.assertLogs(LOG, "WARNING"), self.assertRaises(secrets.KeyMaterialError):
            secrets._write_protected_key(key, os.urandom(32))
        self.assertEqual(key.read_bytes(), b"old key")
        self.assertEqual(self.leftovers(), [])


if __name__ == "__main__":
    unittest.main()
