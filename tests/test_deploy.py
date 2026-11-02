"""Guards for the DEDB-style deployment tree (deploy/ + bootstrap).

The deployment logic lives INSIDE the bundle (deploy/install.ps1 +
deploy/lib/common.ps1, packed by deploy/build-bundle.ps1); the old
scripts/build-bundle.ps1 is gone and scripts/Setup-NWDash.cmd is a thin
bootstrap that downloads/unpacks the bundle and hands off to the bundled
installer. These tests keep that structure (and the release-asset naming)
from silently regressing.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

PROJECT = Path(__file__).resolve().parents[1]
DEPLOY = PROJECT / "deploy"
SETUP_CMD = PROJECT / "scripts" / "Setup-NWDash.cmd"


class TestDeployTree(unittest.TestCase):
    def test_deploy_scripts_exist(self):
        for rel in ("build-bundle.ps1", "install.ps1", "lib/common.ps1"):
            self.assertTrue((DEPLOY / rel).is_file(), f"deploy/{rel} missing")

    def test_old_scripts_build_bundle_removed(self):
        self.assertFalse(
            (PROJECT / "scripts" / "build-bundle.ps1").exists(),
            "scripts/build-bundle.ps1 was replaced by deploy/build-bundle.ps1",
        )

    def test_bundle_name_pattern(self):
        build = (DEPLOY / "build-bundle.ps1").read_text(encoding="utf-8")
        self.assertIn('"nwdash-bundle-$Ver-win-x64"', build)

    def test_build_pins_runtime_hashes(self):
        build = (DEPLOY / "build-bundle.ps1").read_text(encoding="utf-8")
        self.assertIn(
            "https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip",
            build,
        )
        # Both runtime artifacts must stay SHA-pinned (supply-chain guard).
        for var in ("$PySha", "$NssmExeSha"):
            match = re.search(re.escape(var) + r"\s*=\s*'([0-9a-f]{64})'", build)
            self.assertIsNotNone(match, f"{var} is not a pinned sha256")

    def test_version_source_matches_config(self):
        config = (PROJECT / "nwdash" / "config.py").read_text(encoding="utf-8")
        self.assertRegex(config, r'(?m)^APP_VERSION\s*=\s*"\d+\.\d+\.\d+"', "APP_VERSION missing")


class TestSetupBootstrap(unittest.TestCase):
    def test_references_new_bundle_pattern(self):
        text = SETUP_CMD.read_text(encoding="utf-8")
        self.assertIn("nwdash-bundle-*-win-x64.zip", text)
        self.assertIn(r"nwdash-bundle-(\d+\.\d+\.\d+)-win-x64\.zip", text)

    def test_old_bundle_pattern_gone(self):
        text = SETUP_CMD.read_text(encoding="utf-8")
        self.assertNotIn("networker-dashboard-*-bundle.zip", text)

    def test_hands_off_to_bundled_installer(self):
        text = SETUP_CMD.read_text(encoding="utf-8")
        self.assertIn(r"deploy\install.ps1", text)
        # The bootstrap must carry NO service/task logic of its own ("nssm" may
        # appear in comments, but never as an executed command).
        for forbidden in (
            "Register-ScheduledTask",
            "New-ScheduledTaskAction",
            "nssm install",
            "nssm set",
            "nssm start",
            "nssm stop",
        ):
            self.assertNotIn(forbidden, text, f"bootstrap should not contain {forbidden}")


class TestDashboardPasswordGate(unittest.TestCase):
    """install.ps1 refuses a password-less network install up front.

    Since review S4 the app refuses to start on a non-loopback bind with no
    password, so without this gate such an install would only fail its health
    gate, in an NSSM restart loop, after the old install was already removed.
    """

    @classmethod
    def setUpClass(cls):
        cls.install = (DEPLOY / "install.ps1").read_text(encoding="utf-8")
        cls.common = (DEPLOY / "lib" / "common.ps1").read_text(encoding="utf-8")

    def test_env_var_the_installer_sets_is_the_one_the_app_reads(self):
        from nwdash.main import allow_unauthenticated_requested, parse_args

        match = re.search(r"\$script:NwdashAllowUnauthEnv = '([A-Z_]+)=([^']*)'", self.common)
        self.assertIsNotNone(match, "NwdashAllowUnauthEnv missing from common.ps1")
        name, value = match.groups()
        with mock.patch.dict(os.environ, {name: value}):
            self.assertTrue(allow_unauthenticated_requested(parse_args([])))
        with mock.patch.dict(os.environ, {name: "0"}):
            self.assertFalse(allow_unauthenticated_requested(parse_args([])))

    def test_fresh_install_is_refused_before_elevation(self):
        refusal = self.install.index("if ($authPlan -eq 'refused'")
        self.assertLess(refusal, self.install.index("# ---- elevation"))
        self.assertIn("[switch]$AllowUnauthenticated", self.install)
        self.assertIn("$envExtra += $script:NwdashAllowUnauthEnv", self.install)

    def test_upgrade_is_refused_before_the_service_is_stopped(self):
        upgrade = self.install[self.install.index("# ---- upgrade / rollback"):]
        self.assertLess(upgrade.index("if ($upgradePlan -eq 'refused')"), upgrade.index("& $nssm stop"))

    @unittest.skipUnless(shutil.which("powershell"), "Windows PowerShell not available")
    def test_auth_plan_powershell_tests_pass(self):
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
             str(DEPLOY / "test" / "auth-plan.test.ps1")],
            capture_output=True, text=True, timeout=120, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class TestFolderLock(unittest.TestCase):
    """install.ps1 locks the install folder on every path (review S5).

    deploy/test/folder-acl.test.ps1 covers what the lock does; these cover that
    each path reaches it, at a point where a failure leaves the service alone.
    """

    @classmethod
    def setUpClass(cls):
        cls.install = (DEPLOY / "install.ps1").read_text(encoding="utf-8")
        cls.common = (DEPLOY / "lib" / "common.ps1").read_text(encoding="utf-8")
        cls.before_elevation = cls.install[:cls.install.index("# ---- elevation")]

    def test_nothing_before_elevation_trips_over_a_locked_folder(self):
        """Test-Path raises access denied on a locked file, and that is fatal
        under EAP=Stop: every non-elevated run would die before the UAC prompt."""
        self.assertNotIn("Test-Path (Join-Path $InstallDir 'data", self.before_elevation)
        self.assertIn("Get-NwdashPathState -Path (Join-Path $InstallDir 'data\\auth.json')", self.before_elevation)
        self.assertIn("-PasswordUnknown ($authFile -eq 'denied')", self.before_elevation)

    def test_a_bad_target_or_account_is_refused_before_elevation(self):
        self.assertIn("if ($aclProblem) { Stop-Nwdash", self.before_elevation)
        self.assertIn("Resolve-NwdashAccountSid -Account $ServiceAccount", self.before_elevation)

    def test_a_fresh_install_locks_before_its_first_start(self):
        fresh = self.install[self.install.index("# ---- fresh install"):]
        lock = fresh.index("Set-NwdashInstallAcl")
        self.assertLess(fresh.index("Copy-Item -Recurse -Force $PySrc"), lock)
        self.assertLess(lock, fresh.index("Start-NwdashServiceVerified"))

    def test_an_upgrade_or_rollback_locks_before_the_service_is_stopped(self):
        upgrade = self.install[self.install.index("# ---- upgrade / rollback"):self.install.index("# ---- fresh install")]
        lock = upgrade.index("Set-NwdashInstallAcl")
        self.assertLess(lock, upgrade.index("Set-NwdashNssmAutoRestart -Nssm $nssm -Enabled:$false"))
        self.assertLess(lock, upgrade.index("& $nssm stop"))

    def test_an_upgrade_takes_the_account_from_the_live_service(self):
        upgrade = self.install[self.install.index("# ---- upgrade / rollback"):self.install.index("# ---- fresh install")]
        self.assertIn("$liveAccount = Read-NwdashServiceAccount", upgrade)
        self.assertIn("if (-not $liveAccount) { Stop-Nwdash", upgrade)
        self.assertNotIn("-Account $ServiceAccount", upgrade)

    def test_a_restore_puts_the_app_back_on_the_lock(self):
        restore = self.common[self.common.index("function Restore-NwdashApp"):self.common.index("# Prune backups")]
        self.assertIn("Reset-NwdashAppAcl -InstallDir $InstallDir", restore)

    def test_new_scripts_stay_ascii(self):
        for path in (DEPLOY / "install.ps1", DEPLOY / "lib" / "common.ps1", DEPLOY / "test" / "folder-acl.test.ps1"):
            self.assertTrue(path.read_bytes().isascii(), f"{path.name} must stay ASCII for Windows PowerShell 5.1")

    def test_the_lock_does_not_use_set_acl(self):
        """Set-Acl wipes audit rules on an item that is already protected, and its
        module does not load in Windows PowerShell started through another process
        from pwsh 7, as Setup-NWDash.cmd is from a pwsh prompt."""
        code = "\n".join(line for line in self.common.splitlines() if not line.lstrip().startswith("#"))
        self.assertNotIn("Set-Acl", code)

    @unittest.skipUnless(shutil.which("powershell"), "Windows PowerShell not available")
    def test_folder_acl_powershell_tests_pass(self):
        # Without the PSModulePath pwsh 7 (CI's default shell) passes down: Windows
        # PowerShell cannot load Get-Acl with it, and this test file reads with it.
        env = {name: value for name, value in os.environ.items() if name.upper() != "PSMODULEPATH"}
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
             str(DEPLOY / "test" / "folder-acl.test.ps1")],
            capture_output=True, text=True, timeout=300, check=False, env=env,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


@unittest.skipUnless(shutil.which("powershell"), "Windows PowerShell not available")
class TestLockingAnExistingInstall(unittest.TestCase):
    """An upgrade from an open install, with real machine-scope DPAPI keys, in a
    throwaway copy: the lock changes who can reach the keys, never the keys."""

    LOAD_KEYS = (
        "import hashlib, nwdash.secrets as s;"
        "print(hashlib.sha256(s.WMI_CREDENTIAL_KEY + s.AUTH_SECRET_KEY).hexdigest())"
    )

    def setUp(self):
        # Not mkdtemp: its 0o700 adds an OWNER RIGHTS entry on Windows, which an
        # install folder does not have and which stops an owner undoing a deny.
        self.dir = Path(tempfile.gettempdir()) / f"nwdash-upgrade-{uuid.uuid4().hex}"
        self.dir.mkdir()
        self.addCleanup(self._remove)
        shutil.copytree(PROJECT / "nwdash", self.dir / "nwdash", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy(PROJECT / "networker_dashboard.py", self.dir)
        self.data = self.dir / "data"

    def _powershell(self, script):
        return subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, text=True, timeout=180, check=False,
        )

    def _remove(self):
        self._powershell(
            "$me = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value;"
            f"icacls '{self.dir}' /remove:d \"*$me\" /T /Q | Out-Null;"
            f"icacls '{self.dir}' /grant \"*${{me}}:(OI)(CI)F\" /T /Q | Out-Null"
        )
        shutil.rmtree(self.dir, ignore_errors=True)

    def _python(self, *args):
        return subprocess.run(
            [sys.executable, *args], cwd=self.dir, capture_output=True, text=True, timeout=120, check=False,
        )

    def _key_files(self):
        return {path.name: path.read_bytes() for path in self.data.glob(".*_key")}

    def _lock(self):
        """Apply the installer's lock with this account as the service. /setowner
        is left out: it needs elevation, and folder-acl.test.ps1 covers it."""
        script = (
            f". '{DEPLOY / 'lib' / 'common.ps1'}';"
            "$me = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value;"
            f"Set-NwdashInstallAcl -InstallDir '{self.dir}' -ServiceSid $me "
            "-Icacls { param($a) if ($a -notcontains '/setowner' -and -not \"$($a[0])\".EndsWith('\\backups\\*')) "
            "{ Invoke-NwdashIcacls -Arguments $a } }"
        )
        result = self._powershell(script)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_keys_survive_the_lock_byte_for_byte(self):
        before = self._python("-c", self.LOAD_KEYS)
        self.assertEqual(before.returncode, 0, before.stderr)
        keys = self._key_files()
        self.assertIn(".auth_key", keys)
        for name, blob in keys.items():
            self.assertTrue(blob.startswith(b"DPAPI1\n"), f"{name} should be DPAPI-wrapped")

        self._lock()

        self.assertEqual(self._key_files(), keys, "the lock must not rewrite a key")
        after = self._python("-c", self.LOAD_KEYS)
        self.assertEqual(after.returncode, 0, after.stderr)
        self.assertEqual(after.stdout, before.stdout, "the service must still unwrap the same keys")
        self.assertEqual(self._key_files(), keys, "a load after the lock must not rewrite a key")

    def test_the_health_step_agrees_with_the_real_lock(self):
        """The lock is PowerShell and the step that watches it is Python, so check
        one against the other: passed on the real lock, failed once it is opened."""
        check = "import nwdash.admin as a; print(a.folder_permissions())"
        self.assertEqual(self._python("-c", self.LOAD_KEYS).returncode, 0)
        self._lock()

        locked = self._python("-c", check)
        self.assertEqual(locked.returncode, 0, locked.stderr)
        self.assertIn("('ok',", locked.stdout)

        opened = self._powershell(f"icacls '{self.data}' /grant '*S-1-5-32-545:(OI)(CI)(R)' /Q")
        self.assertEqual(opened.returncode, 0, opened.stdout + opened.stderr)
        result = self._python("-c", check)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("('bad',", result.stdout)
        self.assertIn("Other local users can read data", result.stdout)

        self._lock()  # what -Upgrade does, and the step's advice
        self.assertIn("('ok',", self._python("-c", check).stdout)

    def test_a_certificate_upload_works_and_stays_under_the_lock(self):
        """The TLS tab writes <key>.new and <key>.previous beside the live key and
        then replaces it. As the service, under the real lock: it can, and what it
        wrote stays closed to other local users."""
        self._lock()
        upload = f"""
import sys
from pathlib import Path
sys.path.insert(0, {str(PROJECT / "tests")!r})
import certgen
from nwdash import admin, fsacl
from nwdash import config as cfg
key = certgen.RsaKey()
cert_file, key_file = Path(".certs/site.crt").resolve(), Path(".certs/site.key").resolve()
cert_file.write_text(certgen.certificate(key, cn="old"), encoding="ascii")
key_file.write_text(key.pem(), encoding="ascii")
cfg.TLS_CERT_FILE, cfg.TLS_KEY_FILE = str(cert_file), str(key_file)
status, body = admin.install_certificate(certgen.certificate(key), key.pem(), confirm_name=True, apply=lambda context: True)
previous = key_file.with_name("site.key.previous")
print("status", int(status), body.get("error", ""))
print("previous", previous.exists())
print("staged", sorted(path.name for path in cert_file.parent.glob("*.new")))
for path in (cert_file.parent, key_file, previous):
    print(path.name, fsacl.broad_grants(fsacl.dacl_sddl(path), fsacl.READ))
print("step", admin.folder_permissions()[0])
"""
        result = self._python("-c", upload)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for line in ("status 200", "previous True", "staged []", ".certs []", "site.key []",
                     "site.key.previous []", "step ok"):
            self.assertIn(line, result.stdout)

    def test_a_copy_started_by_a_denied_account_stops_cleanly(self):
        self.assertEqual(self._python("-c", self.LOAD_KEYS).returncode, 0)
        keys = self._key_files()
        denied = self._powershell(
            "$me = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value;"
            f"icacls '{self.data}' /deny \"*${{me}}:(OI)(CI)(R)\" /Q"
        )
        self.assertEqual(denied.returncode, 0, denied.stdout + denied.stderr)

        result = self._python("networker_dashboard.py", "--no-launch")

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("NOT replaced", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        restored = self._powershell(
            "$me = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value;"
            f"icacls '{self.data}' /remove:d \"*$me\" /T /Q"
        )
        self.assertEqual(restored.returncode, 0, restored.stdout + restored.stderr)
        self.assertEqual(self._key_files(), keys)

    def test_a_dry_run_survives_a_locked_data_folder(self):
        """The real install.ps1 -Check, which changes nothing: a non-elevated run
        cannot read data\\auth.json on a locked install and must say so, not die."""
        self.data.mkdir()
        (self.data / "auth.json").write_text("{}", encoding="utf-8")
        denied = self._powershell(
            "$me = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value;"
            f"icacls '{self.data}' /deny \"*${{me}}:(OI)(CI)(R)\" /Q"
        )
        self.assertEqual(denied.returncode, 0, denied.stdout + denied.stderr)

        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(DEPLOY / "install.ps1"),
             "-Check", "-InstallDir", str(self.dir)],
            capture_output=True, text=True, timeout=120, check=False,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("not known - data\\ is locked", result.stdout)
        self.assertIn("data\\, .certs\\, logs\\ for SYSTEM, Administrators and LocalSystem only", result.stdout)

    def _dry_run(self, *args):
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(DEPLOY / "install.ps1"),
             "-Check", *args],
            capture_output=True, text=True, timeout=120, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_a_dry_run_says_a_drive_root_would_be_refused(self):
        output = self._dry_run("-InstallDir", "C:\\")
        self.assertIn("is a drive root", output)
        self.assertIn("an install would be REFUSED", output)

    def test_a_dry_run_says_an_unknown_account_would_be_refused(self):
        output = self._dry_run("-InstallDir", str(self.dir), "-ServiceAccount", "no-such-domain\\no-such-user")
        self.assertIn("does not resolve to a Windows account", output)
        self.assertIn("an install would be REFUSED", output)

    def test_a_dry_run_names_a_chosen_service_account(self):
        output = self._dry_run("-InstallDir", str(self.dir), "-ServiceAccount", "NT AUTHORITY\\NetworkService")
        self.assertIn("Administrators and NT AUTHORITY\\NetworkService only", output)


class TestBundleAllowList(unittest.TestCase):
    def test_allow_list_is_two_way_current(self):
        """Every shippable repo file is allow-listed and vice versa (same
        guarantee the build enforces at run time, checked here so the suite
        catches a stale list without running PowerShell)."""
        build = (DEPLOY / "build-bundle.ps1").read_text(encoding="utf-8")
        listed = set(re.findall(r"'(nwdash\\[^']+|networker_dashboard\.py)'", build))
        on_disk = {
            str(p.relative_to(PROJECT))
            for p in (PROJECT / "nwdash").rglob("*")
            if p.is_file() and p.suffix in (".py", ".html", ".css", ".js")
        }
        on_disk.add("networker_dashboard.py")
        self.assertEqual(on_disk - listed, set(), "repo files missing from the allow-list")
        self.assertEqual(listed - on_disk, set(), "allow-listed files missing from the repo")


if __name__ == "__main__":
    unittest.main()
