"""scripts/Setup-NWDash.cmd end to end, under Windows PowerShell 5.1, against
fake bundles whose deploy\\install.ps1 only records what it was given.

Review S9: the bootstrap printed every forwarded argument, -AuthPassword and
-ServicePassword included, and put them on the installer's command line,
where process-creation auditing records them.
"""
import json
import os
import shutil
import subprocess
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


if __name__ == "__main__":
    unittest.main()
