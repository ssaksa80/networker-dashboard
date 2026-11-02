"""The admin console's view of the install folder's lock (review S5).

deploy/install.ps1 applies the lock; deploy/test/folder-acl.test.ps1 covers that.
These tests cover the other half: the self-test step that shows the lock going
away, and the reader of Windows access lists behind it.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nwdash import admin, fsacl
from nwdash import config as _cfg

LOCKED = "D:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;0x1301bf;;;S-1-5-21-1-2-3-1105)"
APP_FOLDER = "D:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;0x1200a9;;;BU)"
# What a folder inherits from C:\ on a client edition.
INHERITED_FROM_C = (
    "D:AI(A;OICIID;FA;;;BA)(A;OICIID;FA;;;SY)(A;OICIID;0x1200a9;;;BU)"
    "(A;ID;0x1301bf;;;AU)(A;OICIIOID;SDGXGWGR;;;AU)"
)


class TestBroadGrants(unittest.TestCase):
    def test_a_locked_folder_gives_other_users_nothing(self):
        self.assertEqual(fsacl.broad_grants(LOCKED, fsacl.READ), [])
        self.assertEqual(fsacl.broad_grants(LOCKED, fsacl.CHANGE), [])

    def test_the_app_folder_is_readable_but_not_changeable(self):
        self.assertEqual(fsacl.broad_grants(APP_FOLDER, fsacl.READ), ["Users"])
        self.assertEqual(fsacl.broad_grants(APP_FOLDER, fsacl.CHANGE), [])

    def test_a_folder_inheriting_from_c_is_open(self):
        self.assertEqual(fsacl.broad_grants(INHERITED_FROM_C, fsacl.READ), ["Authenticated Users", "Users"])
        self.assertEqual(fsacl.broad_grants(INHERITED_FROM_C, fsacl.CHANGE), ["Authenticated Users"])

    def test_inherit_only_entries_count(self):
        """They decide what the next key file gets."""
        sddl = "D:PAI(A;OICI;FA;;;SY)(A;OICIIO;FR;;;BU)"
        self.assertEqual(fsacl.broad_grants(sddl, fsacl.READ), ["Users"])

    def test_every_way_of_spelling_a_read_right(self):
        for rights in ("FA", "FR", "GA", "GR", "CC", "0x1", "0x120089", "0X1200A9"):
            self.assertEqual(fsacl.broad_grants(f"D:(A;;{rights};;;WD)", fsacl.READ), ["Everyone"], rights)

    def test_every_way_of_spelling_a_change_right(self):
        for rights in ("FA", "FW", "GA", "GW", "DC", "SD", "WD", "WO", "0x2", "0x1301bf"):
            self.assertEqual(fsacl.broad_grants(f"D:(A;;{rights};;;BU)", fsacl.CHANGE), ["Users"], rights)

    def test_read_only_rights_are_not_a_change(self):
        for rights in ("FR", "FX", "GR", "GX", "RC", "0x1200a9"):
            self.assertEqual(fsacl.broad_grants(f"D:(A;;{rights};;;BU)", fsacl.CHANGE), [], rights)

    def test_only_allow_entries_count(self):
        self.assertEqual(fsacl.broad_grants("D:(D;;FA;;;WD)(A;;FA;;;SY)", fsacl.READ), [])

    def test_callback_allow_entries_count(self):
        self.assertEqual(fsacl.broad_grants('D:(XA;;FR;;;BU;(Title=="x"))', fsacl.READ), ["Users"])

    def test_no_access_control_at_all_is_everyone(self):
        self.assertEqual(fsacl.broad_grants("D:NO_ACCESS_CONTROL", fsacl.READ), ["Everyone"])

    def test_a_group_of_another_domain_counts(self):
        sddl = "D:(A;;FR;;;S-1-5-21-111-222-333-513)(A;;FR;;;S-1-5-21-111-222-333-1105)"
        self.assertEqual(fsacl.broad_grants(sddl, fsacl.READ), ["domain group S-1-5-21-111-222-333-513"])


@unittest.skipUnless(sys.platform == "win32", "reads Windows access lists")
class TestRealAccessLists(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="nwdash-fsacl-")
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def _icacls(self, *args):
        subprocess.run(["icacls", str(self.dir), *args], check=True, capture_output=True)

    def test_what_icacls_grants_is_what_the_reader_reports(self):
        me = f"{os.environ['USERNAME']}:(OI)(CI)F"
        self._icacls("/inheritance:r", "/grant:r", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F", me)
        self.assertEqual(fsacl.broad_grants(fsacl.dacl_sddl(self.dir), fsacl.READ), [])

        self._icacls("/grant", "*S-1-5-32-545:(OI)(CI)RX")
        self.assertEqual(fsacl.broad_grants(fsacl.dacl_sddl(self.dir), fsacl.READ), ["Users"])
        self.assertEqual(fsacl.broad_grants(fsacl.dacl_sddl(self.dir), fsacl.CHANGE), [])

        self._icacls("/grant", "*S-1-5-11:(OI)(CI)M")
        self.assertEqual(fsacl.broad_grants(fsacl.dacl_sddl(self.dir), fsacl.CHANGE), ["Authenticated Users"])

    def test_a_file_inherits_what_the_folder_hands_down(self):
        self._icacls("/grant", "*S-1-1-0:(OI)(CI)R")
        key = self.dir / ".session_key"
        key.write_bytes(b"x")
        self.assertIn("Everyone", fsacl.broad_grants(fsacl.dacl_sddl(key), fsacl.READ))

    def test_a_missing_path_is_an_os_error(self):
        with self.assertRaises(OSError):
            fsacl.dacl_sddl(self.dir / "absent")


class TestFolderPermissionsStep(unittest.TestCase):
    """The Health tab's step: which paths it asks about, and what it says."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="nwdash-step-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.data = self.root / "data"
        self.logs = self.root / "logs"
        self.certs = self.root / ".certs"
        for folder in (self.data, self.logs, self.certs):
            folder.mkdir()
        self.session_key = self.data / ".session_key"
        self.auth_key = self.data / ".auth_key"
        self.tls_key = self.certs / "site.key"
        self.old_tls_key = self.certs / "site.key.previous"  # what an upload keeps
        for file in (self.session_key, self.auth_key, self.tls_key, self.old_tls_key):
            file.write_bytes(b"x")
        self.lists = {}
        for target, name, value in (
            (admin, "DATA_DIR", self.data), (admin, "LOG_FILE", self.logs / "app.log"),
            (admin, "SESSION_KEY_FILE", self.session_key), (admin, "AUTH_KEY_FILE", self.auth_key),
            (admin, "APP_BASE_DIR", self.root), (_cfg, "TLS_KEY_FILE", str(self.tls_key)),
            (fsacl, "dacl_sddl", self._sddl), (sys, "platform", "win32"),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _sddl(self, path):
        answer = self.lists.get(Path(path), LOCKED)
        if isinstance(answer, OSError):
            raise answer
        return answer

    def test_a_locked_install_passes(self):
        self.lists[self.root] = APP_FOLDER
        state, detail = admin.folder_permissions()
        self.assertEqual(state, "ok", detail)

    def test_every_secret_is_checked(self):
        for path in (self.data, self.session_key, self.auth_key, self.logs, self.tls_key, self.old_tls_key):
            with self.subTest(path=path.name):
                self.lists = {path: INHERITED_FROM_C}
                state, detail = admin.folder_permissions()
                self.assertEqual(state, "bad")
                self.assertIn(path.name, detail)
                self.assertIn("Authenticated Users", detail)

    def test_an_app_folder_others_can_change_fails(self):
        self.lists[self.root] = INHERITED_FROM_C
        state, detail = admin.folder_permissions()
        self.assertEqual(state, "bad")
        self.assertIn(f"can change {self.root.name}", detail)

    def test_the_package_folder_is_checked_too(self):
        """A rollback can bring it back open while the root stays locked."""
        package = Path(admin.__file__).resolve().parent
        self.lists[package] = INHERITED_FROM_C
        state, detail = admin.folder_permissions()
        self.assertEqual(state, "bad")
        self.assertIn(package.name, detail)

    def test_the_failure_says_how_to_put_the_lock_back(self):
        self.lists[self.data] = INHERITED_FROM_C
        self.assertIn("-Upgrade", admin.folder_permissions()[1])

    def test_a_file_not_written_yet_is_skipped(self):
        self.auth_key.unlink()
        self.lists[self.auth_key] = OSError("must not be asked")
        self.assertEqual(admin.folder_permissions()[0], "ok")

    def test_no_tls_key_configured_is_fine(self):
        with mock.patch.object(_cfg, "TLS_KEY_FILE", ""):
            self.assertEqual(admin.folder_permissions()[0], "ok")

    def test_an_unreadable_list_is_a_warning_not_a_pass(self):
        self.lists[self.data] = PermissionError(13, "denied")
        state, detail = admin.folder_permissions()
        self.assertEqual(state, "warn")
        self.assertIn("denied", detail)

    def test_off_windows_nothing_is_claimed(self):
        with mock.patch.object(sys, "platform", "linux"):
            state, detail = admin.folder_permissions()
        self.assertEqual(state, "warn")
        self.assertIn("Not checked", detail)

    def test_the_self_test_runs_it_after_the_data_folder(self):
        self.lists[self.data] = INHERITED_FROM_C
        with mock.patch.object(admin, "load_profiles", return_value={}), \
             mock.patch.object(admin, "load_email_config", return_value={}):
            result = admin.run_self_test(lambda: (True, "healthy"))
        ids = [step["id"] for step in result["steps"]]
        self.assertEqual(ids.index("folders"), ids.index("data") + 1)
        step = next(s for s in result["steps"] if s["id"] == "folders")
        self.assertEqual(step["state"], "bad")
        self.assertFalse(result["passed"])

    def test_the_admin_page_lists_the_step(self):
        script = (Path(admin.__file__).parent / "assets" / "admin.js").read_text(encoding="utf-8")
        self.assertIn('{id: "folders", label: "Folder permissions"}', script)


if __name__ == "__main__":
    unittest.main()
