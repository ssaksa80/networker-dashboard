"""Guards for the DEDB-style deployment tree (deploy/ + bootstrap).

The deployment logic lives INSIDE the bundle (deploy/install.ps1 +
deploy/lib/common.ps1, packed by deploy/build-bundle.ps1); the old
scripts/build-bundle.ps1 is gone and scripts/Setup-NWDash.cmd is a thin
bootstrap that downloads/unpacks the bundle and hands off to the bundled
installer. These tests keep that structure (and the release-asset naming)
from silently regressing.
"""

import re
import unittest
from pathlib import Path

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
