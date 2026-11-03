"""scripts/Setup-NWDash.cmd end to end, under Windows PowerShell 5.1, against
fake bundles whose deploy\\install.ps1 only records what it was given, and a
fake gh CLI in front of PATH.

Review S9: the bootstrap printed every forwarded argument, -AuthPassword and
-ServicePassword included, and put them on the installer's command line,
where process-creation auditing records them. Review S10: it downloaded and
ran a newer bundle unasked and unverified, and unpacked "..\\" entries
outside its staging folder.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
SETUP_CMD = PROJECT / "scripts" / "Setup-NWDash.cmd"

# A stand-in installer: writes its arguments and the hand-over variables to
# $env:NWDASH_TEST_OUT. The marker comment is how the bootstrap recognises an
# installer that reads the passwords from the environment.
RECORDER = r"""param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Rest)
# reads NWDASH_SETUP_AUTH_PASSWORD
$handed = { param($n) [Environment]::GetEnvironmentVariable('NWDASH_SETUP_' + $n) }  # no marker in the old one
@{ version = '__VERSION__'; args = @($Rest); auth = "$(& $handed 'AUTH_PASSWORD')"; service = "$(& $handed 'SERVICE_PASSWORD')" } |
  ConvertTo-Json | Set-Content -Path $env:NWDASH_TEST_OUT -Encoding UTF8
exit 7
"""
OLD_RECORDER = RECORDER.replace("# reads NWDASH_SETUP_AUTH_PASSWORD\n", "")
# No spaces: cmd.exe's %* splits a quoted argument in two (the prompt avoids that).
SECRETS = ("dash-s3cret-pw", "svc-s3cret")


def bundle_zip(path: Path, version: str, installer: str = RECORDER, extra: dict | None = None) -> Path:
    zip_path = path / f"nwdash-bundle-{version}-win-x64.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("deploy/install.ps1", installer.replace("__VERSION__", version))
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return zip_path


@unittest.skipUnless(os.name == "nt" and shutil.which("powershell"), "needs cmd.exe and Windows PowerShell")
class BootstrapCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="nwdash-bootstrap-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.site = self.dir / "site"  # where the operator put the .cmd and the bundles
        self.site.mkdir()
        self.temp = self.dir / "temp"  # the bootstrap's %TEMP%: staging goes here
        self.temp.mkdir()
        shutil.copy(SETUP_CMD, self.site)
        self.out = self.dir / "installer.json"

    def run_setup(self, *args, env=None):
        # No pwsh 7 module path (Windows PowerShell cannot load its modules), no
        # real token, and nothing from this machine's own gh on PATH by accident.
        base = {k: v for k, v in os.environ.items() if k.upper() not in ("PSMODULEPATH", "GITHUB_TOKEN")}
        base.update({"TEMP": str(self.temp), "TMP": str(self.temp), "NWDASH_TEST_OUT": str(self.out)})
        base.update(env or {})
        result = subprocess.run(
            ["cmd.exe", "/c", str(self.site / "Setup-NWDash.cmd"), *args],
            capture_output=True, text=True, timeout=180, check=False, env=base, cwd=self.site,
        )
        return result

    def installer_saw(self):
        self.assertTrue(self.out.exists(), "the bundled installer did not run")
        return json.loads(self.out.read_text(encoding="utf-8-sig"))


class TestPasswordsStayOffTheRecord(BootstrapCase):
    def test_passwords_are_not_shown_or_put_on_the_installer_command_line(self):
        bundle_zip(self.site, "9.9.9")
        result = self.run_setup("-NoUpdate", "-Port", "9443", "-AuthPassword", SECRETS[0],
                                "-ServiceAccount", "EXAMPLE\\svc-nwdash", "-ServicePassword", SECRETS[1])
        for secret in SECRETS:
            self.assertNotIn(secret, result.stdout + result.stderr)
        self.assertIn("-AuthPassword ********", result.stdout)
        seen = self.installer_saw()
        self.assertEqual(seen["args"], ["-Port", "9443", "-ServiceAccount", "EXAMPLE\\svc-nwdash"])
        self.assertEqual((seen["auth"], seen["service"]), SECRETS)
        self.assertEqual(result.returncode, 7, "the installer's exit code is passed on")

    def test_prefixes_and_the_colon_form_are_recognised(self):
        bundle_zip(self.site, "9.9.9")
        result = self.run_setup("-NoUpdate", f"-AuthPass:{SECRETS[1]}", "-check")
        self.assertNotIn(SECRETS[1], result.stdout)
        seen = self.installer_saw()
        self.assertEqual((seen["args"], seen["auth"]), (["-check"], SECRETS[1]))

    def test_an_older_installer_still_gets_them_as_arguments(self):
        """A bundle from before this change reads only its parameters."""
        bundle_zip(self.site, "9.9.9", installer=OLD_RECORDER)
        result = self.run_setup("-NoUpdate", "-AuthPassword", SECRETS[0])
        self.assertNotIn(SECRETS[0], result.stdout)
        seen = self.installer_saw()
        self.assertEqual((seen["args"], seen["auth"]), (["-AuthPassword", SECRETS[0]], ""))


# Stands in for the GitHub CLI: answers `release view` from release.json, and
# `release download` by copying whatever is in assets\ into --dir.
FAKE_GH = r"""import os, shutil, sys
here = os.environ["FAKE_GH_DIR"]
with open(os.path.join(here, "calls.txt"), "a", encoding="utf-8") as log:
    log.write(" ".join(sys.argv[1:]) + "\n")
args = sys.argv[1:]
if args[:2] == ["release", "view"]:
    with open(os.path.join(here, "release.json"), encoding="utf-8") as release:
        sys.stdout.write(release.read())
elif args[:2] == ["release", "download"]:
    for name in os.listdir(os.path.join(here, "assets")):
        shutil.copy(os.path.join(here, "assets", name), os.path.join(args[args.index("--dir") + 1], name))
else:
    sys.exit(1)
"""
NEWER = "nwdash-bundle-9.9.9-win-x64.zip"


class TestUpdatesAreAskedForAndVerified(BootstrapCase):
    """Review S10: the bootstrap downloaded a newer bundle whenever gh or a
    GITHUB_TOKEN was there and ran it as admin, with no hash check."""

    def setUp(self):
        super().setUp()
        self.gh = self.dir / "fake-gh"
        (self.gh / "assets").mkdir(parents=True)
        (self.gh / "fake_gh.py").write_text(FAKE_GH, encoding="utf-8")
        (self.gh / "gh.cmd").write_text(f'@"{sys.executable}" "{self.gh / "fake_gh.py"}" %*\r\n', encoding="ascii")
        bundle_zip(self.site, "9.9.8")

    def publish(self, served_version="9.9.9", notes=True, digest=True):
        """Release v9.9.9 as GitHub describes it; what a download returns may differ."""
        genuine = bundle_zip(self.dir, "9.9.9")
        sha = hashlib.sha256(genuine.read_bytes()).hexdigest()
        genuine.unlink()
        bundle_zip(self.gh / "assets", "9.9.9")
        if served_version != "9.9.9":
            served = self.gh / "assets" / NEWER
            served.unlink()
            renamed = bundle_zip(self.gh / "assets", served_version)
            renamed.rename(served)
        body = "Fixes.\n\n" + (f"**Bundle SHA-256:** `{sha}` (`{NEWER}`)\n" if notes else "")
        asset = {"name": NEWER, "id": 1, **({"digest": f"sha256:{sha}"} if digest else {})}
        (self.gh / "release.json").write_text(json.dumps({"tagName": "v9.9.9", "body": body, "assets": [asset]}),
                                              encoding="utf-8")

    def run_with_gh(self, *args):
        path = f"{self.gh};{os.environ.get('PATH', '')}"
        return self.run_setup(*args, env={"PATH": path, "FAKE_GH_DIR": str(self.gh)})

    def downloads(self):
        calls = self.gh / "calls.txt"
        return [c for c in calls.read_text(encoding="utf-8").splitlines() if "download" in c] if calls.exists() else []

    def test_a_newer_release_is_only_announced(self):
        self.publish()
        result = self.run_with_gh()
        self.assertIn("v9.9.9 is available. Re-run with -Update", result.stdout)
        self.assertEqual(self.downloads(), [])
        self.assertEqual(self.installer_saw()["version"], "9.9.8")
        self.assertFalse((self.site / NEWER).exists())

    def test_update_uses_a_download_that_verifies(self):
        self.publish()
        result = self.run_with_gh("-Update", "-Port", "9443")
        self.assertIn(f"{NEWER} verified (SHA-256", result.stdout)
        seen = self.installer_saw()
        self.assertEqual((seen["version"], seen["args"]), ("9.9.9", ["-Port", "9443"]))
        self.assertTrue((self.site / NEWER).exists())

    def test_update_refuses_a_download_that_does_not_match(self):
        self.publish(served_version="6.6.6")
        result = self.run_with_gh("-Update")
        self.assertIn(f"NOT used - {NEWER} does not match its published SHA-256", result.stdout)
        self.assertEqual(self.installer_saw()["version"], "9.9.8")
        self.assertFalse((self.site / NEWER).exists(), "an unverified download must not be left where the next run picks it")

    def test_update_refuses_what_it_cannot_verify(self):
        for missing, expected in (({"notes": False}, "release notes give no Bundle SHA-256"),
                                  ({"digest": False}, "GitHub gives no SHA-256 digest")):
            with self.subTest(missing=missing):
                self.publish(**missing)
                result = self.run_with_gh("-Update")
                self.assertIn(expected, result.stdout)
                self.assertEqual(self.installer_saw()["version"], "9.9.8")

    def test_no_update_does_not_ask_github(self):
        self.publish()
        self.run_with_gh("-NoUpdate")
        self.assertFalse((self.gh / "calls.txt").exists())
        self.assertEqual(self.installer_saw()["version"], "9.9.8")


class TestUnpackingStaysInStaging(BootstrapCase):
    """Review S10: zip entries were joined to the staging folder unchecked, so
    a "..\\" name was written outside it, by a run about to be elevated."""

    def test_an_entry_outside_the_staging_folder_refuses_the_bundle(self):
        bundle_zip(self.site, "9.9.9", extra={"../escaped.txt": "x", "deploy/../../escaped-too.txt": "x"})
        result = self.run_setup("-NoUpdate")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("would unpack outside its folder", result.stdout)
        self.assertFalse(self.out.exists(), "the installer must not run")
        self.assertEqual(sorted(p.name for p in self.temp.iterdir()), [], "nothing outside staging, staging removed")


if __name__ == "__main__":
    unittest.main()
