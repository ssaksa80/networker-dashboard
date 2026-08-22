"""Saved-profile credential handling: host binding, key handling, AAD binding.

The defect these cover: _resolve_profile_password substituted only the PASSWORD
from a saved profile while the destination still came from the request, so
posting profileName="prod-nw" together with restApiHost="attacker.example" made
the server decrypt the production credential and send it to that host. The host
allow-list is the only other guard and is off by default.

Everything here is unittest.TestCase on purpose: CI runs
`python -m unittest discover -s tests`, which does not collect bare pytest
functions.
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
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from nwdash import profiles as profiles_mod  # noqa: E402
from nwdash import secrets as secrets_mod  # noqa: E402
from nwdash.models import BadRequest  # noqa: E402

SENTINEL = "__profile_password__"
SAVED = "(saved)"

STORED_PROFILE = {
    "restApiHost": "prod-nw.corp.local",
    "restApiPort": "9090",
    "backupServerHost": "authc.corp.local",
    "backupServerPort": "9090",
    "username": "administrator",
    "apiMode": "auto",
    "apiVersion": "v3",
    "reportRange": "24h",
    "timeoutSeconds": "30",
    "useWmiHealth": False,
    "wmiUsername": "",
    "verifyTls": True,
    "useAuthcHeader": True,
}


def _payload(**overrides):
    """A dashboard payload that matches STORED_PROFILE unless overridden."""
    base = {
        "profileName": "prod-nw",
        "restApiHost": "prod-nw.corp.local",
        "restApiPort": "9090",
        "backupServerHost": "authc.corp.local",
        "backupServerPort": "9090",
        "username": "administrator",
        "password": SENTINEL,
        "wmiPassword": "",
        "verifyTls": True,
    }
    base.update(overrides)
    return base


class ProfileFixtureMixin:
    """Point the profile store at a throwaway file for the duration of a test."""

    def use_profiles(self, profiles):
        tmp = tempfile.TemporaryDirectory(prefix="nwdash-profiles-")
        self.addCleanup(tmp.cleanup)
        data_dir = Path(tmp.name)
        path = data_dir / "profiles.json"
        path.write_text(json.dumps(profiles), encoding="utf-8")
        self._patch(profiles_mod, "PROFILES_FILE", path)
        self._patch(profiles_mod, "DATA_DIR", data_dir)
        return path

    def _patch(self, module, attr, value):
        original = getattr(module, attr)
        setattr(module, attr, value)
        self.addCleanup(setattr, module, attr, original)


class TestProfileHostBinding(ProfileFixtureMixin, unittest.TestCase):
    """A stored secret and a caller-supplied destination must not be combinable."""

    def setUp(self):
        self._patch(
            profiles_mod, "decrypt_profile_secret",
            lambda value, *, name, field: "prod-secret" if value else "",
        )
        self.use_profiles({"prod-nw": dict(STORED_PROFILE, _enc_password="enc:v2:stub")})

    def test_matching_target_substitutes_the_stored_password(self):
        result = profiles_mod._resolve_profile_password(_payload())
        self.assertEqual(result["password"], "prod-secret")
        self.assertEqual(result["restApiHost"], "prod-nw.corp.local")

    def test_attacker_host_is_refused_and_leaks_nothing(self):
        with self.assertRaises(BadRequest) as ctx:
            profiles_mod._resolve_profile_password(_payload(restApiHost="attacker.example"))
        message = str(ctx.exception)
        self.assertIn("REST API server", message)
        self.assertIn("Re-enter the password", message)
        self.assertNotIn("prod-secret", message)

    def test_every_bound_field_is_enforced(self):
        for override in (
            {"restApiHost": "attacker.example"},
            {"restApiPort": "8443"},
            {"backupServerHost": "attacker.example"},
            {"backupServerPort": "8443"},
            {"username": "someone-else"},
            {"verifyTls": False},
        ):
            with self.subTest(**override):
                with self.assertRaises(BadRequest):
                    profiles_mod._resolve_profile_password(_payload(**override))

    def test_url_and_case_spellings_of_the_same_host_still_work(self):
        for spelling in (
            {"restApiHost": "PROD-NW.CORP.LOCAL"},
            {"restApiHost": "prod-nw.corp.local:9090", "restApiPort": ""},
            {"restApiHost": "https://prod-nw.corp.local:9090", "restApiPort": ""},
        ):
            with self.subTest(**spelling):
                result = profiles_mod._resolve_profile_password(_payload(**spelling))
                self.assertEqual(result["password"], "prod-secret")
                self.assertEqual(result["restApiHost"], "prod-nw.corp.local")

    def test_wmi_password_sentinel_is_bound_too(self):
        with self.assertRaises(BadRequest):
            profiles_mod._resolve_profile_password(
                _payload(password="typed-by-hand", wmiPassword=SAVED, restApiHost="attacker.example")
            )

    def test_typed_password_may_target_any_host(self):
        """The one legitimate flow that changes host: re-enter the credential."""
        result = profiles_mod._resolve_profile_password(
            _payload(password="typed-by-hand", restApiHost="dr-nw.corp.local")
        )
        self.assertEqual(result["password"], "typed-by-hand")
        self.assertEqual(result["restApiHost"], "dr-nw.corp.local")

    def test_no_profile_name_is_untouched(self):
        payload = _payload(profileName="", password="typed-by-hand")
        self.assertIs(profiles_mod._resolve_profile_password(payload), payload)

    def test_unknown_profile_says_so(self):
        with self.assertRaises(BadRequest) as ctx:
            profiles_mod._resolve_profile_password(_payload(profileName="does-not-exist"))
        self.assertIn("was not found", str(ctx.exception))

    def test_backup_host_defaults_to_rest_host_on_both_sides(self):
        self.use_profiles({
            "prod-nw": dict(STORED_PROFILE, backupServerHost="", _enc_password="enc:v2:stub"),
        })
        result = profiles_mod._resolve_profile_password(_payload(backupServerHost=""))
        self.assertEqual(result["password"], "prod-secret")
        with self.assertRaises(BadRequest):
            profiles_mod._resolve_profile_password(_payload(backupServerHost="attacker.example"))

    def test_profile_without_verify_tls_is_not_held_to_one(self):
        """Profiles saved before verifyTls existed must keep working."""
        stored = {k: v for k, v in STORED_PROFILE.items() if k != "verifyTls"}
        self.use_profiles({"prod-nw": dict(stored, _enc_password="enc:v2:stub")})
        result = profiles_mod._resolve_profile_password(_payload(verifyTls=False))
        self.assertEqual(result["password"], "prod-secret")


class TestDecryptFailureMessage(ProfileFixtureMixin, unittest.TestCase):
    """A dead key must not be reported as 'Password is required.'"""

    def test_decrypt_failure_names_the_key(self):
        self._patch(profiles_mod, "decrypt_profile_secret", lambda value, *, name, field: "")
        self.use_profiles({"prod-nw": dict(STORED_PROFILE, _enc_password="enc:v2:unreadable")})
        with self.assertRaises(BadRequest) as ctx:
            profiles_mod._resolve_profile_password(_payload())
        message = str(ctx.exception)
        self.assertIn(".session_key", message)
        self.assertNotEqual(message, "Password is required.")

    def test_profile_with_no_saved_password_says_that_instead(self):
        self._patch(profiles_mod, "decrypt_profile_secret", lambda value, *, name, field: "")
        self.use_profiles({"prod-nw": dict(STORED_PROFILE)})
        with self.assertRaises(BadRequest) as ctx:
            profiles_mod._resolve_profile_password(_payload())
        self.assertIn("no saved password", str(ctx.exception))


class TestSaveProfilesFailsLoud(ProfileFixtureMixin, unittest.TestCase):
    def test_write_failure_propagates(self):
        tmp = tempfile.TemporaryDirectory(prefix="nwdash-profiles-")
        self.addCleanup(tmp.cleanup)
        # A directory where the profiles file should be: replace() cannot succeed.
        target = Path(tmp.name) / "profiles.json"
        target.mkdir()
        self._patch(profiles_mod, "PROFILES_FILE", target)
        self._patch(profiles_mod, "DATA_DIR", Path(tmp.name))
        with self.assertRaises(OSError):
            profiles_mod.save_profiles({"a": {}})


class TestProfileSecretAad(unittest.TestCase):
    """AAD bound to name||field: a ciphertext only opens in the slot it was written for."""

    @classmethod
    def setUpClass(cls):
        if not secrets_mod._derive_profile_key():
            raise unittest.SkipTest("cryptography hazmat unavailable")

    def test_round_trip(self):
        blob = secrets_mod.encrypt_profile_secret("s3cret", name="prod-nw", field="password")
        self.assertTrue(blob.startswith("enc:v2:"))
        self.assertEqual(
            secrets_mod.decrypt_profile_secret(blob, name="prod-nw", field="password"), "s3cret"
        )

    def test_blob_cannot_be_moved_to_another_profile(self):
        blob = secrets_mod.encrypt_profile_secret("s3cret", name="prod-nw", field="password")
        self.assertEqual(
            secrets_mod.decrypt_profile_secret(blob, name="attacker-owned", field="password"), ""
        )

    def test_blob_cannot_be_moved_to_another_field(self):
        blob = secrets_mod.encrypt_profile_secret("s3cret", name="prod-nw", field="password")
        self.assertEqual(
            secrets_mod.decrypt_profile_secret(blob, name="prod-nw", field="wmiPassword"), ""
        )

    def test_name_and_field_cannot_be_confused_across_the_separator(self):
        a = secrets_mod.encrypt_profile_secret("s3cret", name="ab", field="c")
        self.assertEqual(secrets_mod.decrypt_profile_secret(a, name="a", field="bc"), "")

    def test_v1_blobs_still_decrypt(self):
        """Existing saved profiles must not break on the format change."""
        import base64
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        key = secrets_mod._derive_profile_key()
        nonce = os.urandom(12)
        ct = AESGCM(key).encrypt(nonce, b"legacy-secret", b"profile")
        legacy = "enc:v1:" + base64.b64encode(nonce + ct).decode("ascii")
        self.assertEqual(
            secrets_mod.decrypt_profile_secret(legacy, name="prod-nw", field="password"),
            "legacy-secret",
        )
        self.assertTrue(secrets_mod.profile_secret_needs_rebinding(legacy))
        self.assertFalse(secrets_mod.profile_secret_needs_rebinding(
            secrets_mod.encrypt_profile_secret("x", name="n", field="password")
        ))

    def test_encrypt_raises_when_no_cipher_is_available(self):
        original_derive = secrets_mod._derive_profile_key
        original_cipher = secrets_mod.WMI_CIPHER
        secrets_mod._derive_profile_key = lambda: None
        secrets_mod.WMI_CIPHER = None
        try:
            with self.assertRaises(secrets_mod.SecretEncryptionError):
                secrets_mod.encrypt_profile_secret("s3cret", name="n", field="password")
        finally:
            secrets_mod._derive_profile_key = original_derive
            secrets_mod.WMI_CIPHER = original_cipher


class TestProfileSecretMigration(ProfileFixtureMixin, unittest.TestCase):
    def setUp(self):
        if not secrets_mod._derive_profile_key():
            self.skipTest("cryptography hazmat unavailable")

    def _v1(self, plaintext):
        import base64
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        nonce = os.urandom(12)
        ct = AESGCM(secrets_mod._derive_profile_key()).encrypt(nonce, plaintext.encode(), b"profile")
        return "enc:v1:" + base64.b64encode(nonce + ct).decode("ascii")

    def test_v1_secrets_are_rebound_without_loss(self):
        path = self.use_profiles({
            "prod-nw": dict(STORED_PROFILE, _enc_password=self._v1("keep-me")),
        })
        self.assertEqual(profiles_mod.migrate_profile_secrets(), 1)
        stored = json.loads(path.read_text(encoding="utf-8"))["prod-nw"]["_enc_password"]
        self.assertTrue(stored.startswith("enc:v2:"))
        self.assertEqual(
            secrets_mod.decrypt_profile_secret(stored, name="prod-nw", field="password"), "keep-me"
        )
        self.assertEqual(profiles_mod.migrate_profile_secrets(), 0)  # idempotent

    def test_undecryptable_blob_is_left_alone(self):
        """A missing key must never cost the operator the ciphertext."""
        path = self.use_profiles({
            "prod-nw": dict(STORED_PROFILE, _enc_password="enc:v1:bm90LXJlYWxseS1jaXBoZXJ0ZXh0"),
        })
        self.assertEqual(profiles_mod.migrate_profile_secrets(), 0)
        stored = json.loads(path.read_text(encoding="utf-8"))["prod-nw"]["_enc_password"]
        self.assertEqual(stored, "enc:v1:bm90LXJlYWxseS1jaXBoZXJ0ZXh0")


class TestKeyMaterialFailsLoud(unittest.TestCase):
    """An unreadable key file must never be replaced — that orphans every secret."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="nwdash-keys-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _patch(self, attr, value):
        original = getattr(secrets_mod, attr)
        setattr(secrets_mod, attr, value)
        self.addCleanup(setattr, secrets_mod, attr, original)

    def test_unreadable_session_key_raises_and_is_not_overwritten(self):
        key_file = self.dir / ".session_key"
        original = b"not a fernet key at all"
        key_file.write_bytes(original)
        self._patch("SESSION_KEY_FILE", key_file)
        self._patch("DATA_DIR", self.dir)
        self._patch("_dpapi_available", lambda: False)
        with self.assertRaises(secrets_mod.KeyMaterialError) as ctx:
            secrets_mod._load_or_create_stable_key()
        self.assertIn("NOT replaced", str(ctx.exception))
        self.assertEqual(key_file.read_bytes(), original)

    def test_undecryptable_wrapped_session_key_raises(self):
        key_file = self.dir / ".session_key"
        original = secrets_mod.DPAPI_MARKER + b"garbage-that-cannot-be-unwrapped"
        key_file.write_bytes(original)
        self._patch("SESSION_KEY_FILE", key_file)
        self._patch("DATA_DIR", self.dir)
        self._patch("_read_protected_key", lambda path: None)
        with self.assertRaises(secrets_mod.KeyMaterialError):
            secrets_mod._load_or_create_stable_key()
        self.assertEqual(key_file.read_bytes(), original)

    def test_truncated_auth_key_raises_and_is_not_overwritten(self):
        key_file = self.dir / ".auth_key"
        original = b"tooshort"
        key_file.write_bytes(original)
        self._patch("AUTH_KEY_FILE", key_file)
        self._patch("DATA_DIR", self.dir)
        self._patch("_dpapi_available", lambda: False)
        with self.assertRaises(secrets_mod.KeyMaterialError):
            secrets_mod._load_or_create_auth_key()
        self.assertEqual(key_file.read_bytes(), original)

    def test_dpapi_protect_failure_never_writes_a_plaintext_key(self):
        key_file = self.dir / ".session_key"

        def _boom(_data):
            raise OSError("CryptProtectData failed")

        self._patch("_dpapi_available", lambda: True)
        self._patch("_dpapi_protect", _boom)
        with self.assertRaises(secrets_mod.KeyMaterialError):
            secrets_mod._write_protected_key(key_file, b"master-key-material")
        self.assertFalse(key_file.exists())

    def test_unwritable_key_path_raises(self):
        key_file = self.dir / "nested"
        key_file.mkdir()  # a directory where the key file should be
        self._patch("_dpapi_available", lambda: False)
        with self.assertRaises(secrets_mod.KeyMaterialError):
            secrets_mod._write_protected_key(key_file, b"master-key-material")

    def test_best_effort_migration_write_does_not_raise(self):
        key_file = self.dir / "nested2"
        key_file.mkdir()
        self._patch("_dpapi_available", lambda: False)
        secrets_mod._write_protected_key(key_file, b"master-key-material", required=False)

    def test_migration_rewrite_never_takes_the_process_down(self):
        """A key that already loaded must not become fatal during the
        plaintext -> DPAPI-wrapped rewrite."""
        key_file = self.dir / ".session_key"
        original = b"plaintext-legacy-key"
        key_file.write_bytes(original)

        def _boom(_data):
            raise OSError("CryptProtectData failed")

        self._patch("_dpapi_available", lambda: True)
        self._patch("_dpapi_protect", _boom)
        secrets_mod._write_protected_key(key_file, b"master-key-material", required=False)
        self.assertEqual(key_file.read_bytes(), original)


class TestSessionCredentialPurge(unittest.TestCase):
    """Deleting a profile must also destroy the password copy any session it
    established left behind in data/sessions.json."""

    def setUp(self):
        from http.cookiejar import CookieJar

        from nwdash import sessions as sessions_mod
        from nwdash.models import ApiConfig, DashboardSession, _put_session, _session_ids_snapshot
        self.sessions_mod = sessions_mod
        self.tmp = tempfile.TemporaryDirectory(prefix="nwdash-purge-")
        self.addCleanup(self.tmp.cleanup)
        self.store = Path(self.tmp.name) / "sessions.json"
        for attr, value in (("SESSION_PERSISTENCE_FILE", self.store), ("DATA_DIR", Path(self.tmp.name))):
            original = getattr(sessions_mod, attr)
            setattr(sessions_mod, attr, value)
            self.addCleanup(setattr, sessions_mod, attr, original)

        def _config(host, user):
            return ApiConfig(
                rest_api_host=host, rest_api_port=9090,
                backup_server_host=host, backup_server_port=9090,
                username=user, password="", api_mode="nwui", api_version="v3",
                report_range="24h", custom_start_date="", custom_end_date="",
                use_wmi_health=False, wmi_username="", wmi_password="",
                timeout_seconds=30, verify_tls=True, use_authc_header=True,
            )

        def _session(host, user):
            return DashboardSession(
                config=_config(host, user), cookie_jar=CookieJar(), auth_headers={},
                encrypted_networker_password="ciphertext-of-the-password",
                encrypted_wmi_password="", created_at=1.0, last_used=2.0,
            )

        self.existing = set(_session_ids_snapshot())
        _put_session("doomed", _session("prod-nw.corp.local", "administrator"))
        _put_session("other-host", _session("dr-nw.corp.local", "administrator"))
        _put_session("other-user", _session("prod-nw.corp.local", "someone-else"))
        self.addCleanup(self._drop_test_sessions)

    def _drop_test_sessions(self):
        from nwdash.models import _pop_session
        for sid in ("doomed", "other-host", "other-user"):
            _pop_session(sid)

    def test_only_the_matching_credential_is_purged(self):
        from nwdash.models import _session_ids_snapshot
        result = self.sessions_mod.purge_sessions_for_credential(
            "PROD-NW.CORP.LOCAL", "administrator"  # case-insensitive host match
        )
        self.assertEqual(result["sessions"], 1)
        live = set(_session_ids_snapshot()) - self.existing
        self.assertEqual(live, {"other-host", "other-user"})
        if self.store.exists():
            written = self.store.read_text(encoding="utf-8")
            self.assertNotIn("doomed", written)
            self.assertIn("other-host", written)

    def test_blank_target_is_a_no_op(self):
        from nwdash.models import _session_ids_snapshot
        self.assertEqual(
            self.sessions_mod.purge_sessions_for_credential("", "administrator"),
            {"sessions": 0, "automations": 0},
        )
        self.assertIn("doomed", _session_ids_snapshot())


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


PASSWORD = "smoke-test-password-profiles"
STARTUP_TIMEOUT_SECONDS = 60


class TestProfilesApiEndToEnd(unittest.TestCase):
    """/api/profiles CRUD over real HTTPS with auth + CSRF, plus the host swap.

    Follows the subprocess pattern of tests/test_server_http.py: the server runs
    from a temp-dir copy so it never touches the project's data/.
    """

    proc = None
    tmp = None
    port = None
    ctx = None
    cookie = ""
    csrf = ""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="nwdash-profiles-e2e-")
        root = Path(cls.tmp.name)
        shutil.copy2(PROJECT_DIR / "networker_dashboard.py", root / "networker_dashboard.py")
        shutil.copytree(
            PROJECT_DIR / "nwdash", root / "nwdash",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        cls.port = _free_port()
        env = dict(os.environ)
        env["DASHBOARD_AUTH_PASSWORD"] = PASSWORD
        cls.stdout_log = open(root / "server-stdout.log", "wb")
        try:
            cls.proc = subprocess.Popen(
                [sys.executable, "networker_dashboard.py",
                 "--port", str(cls.port), "--bind", "127.0.0.1", "--no-launch"],
                cwd=str(root), env=env, stdout=cls.stdout_log, stderr=subprocess.STDOUT,
            )
        except OSError as exc:
            raise unittest.SkipTest(f"could not launch server subprocess: {exc}")
        cls.ctx = ssl._create_unverified_context()
        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if cls.proc.poll() is not None:
                cls._teardown_proc()
                raise unittest.SkipTest(f"server exited early (code {cls.proc.returncode})")
            try:
                if cls._request("GET", "/api/health")[0] == 200:
                    cls._login()
                    return
            except (urllib.error.URLError, OSError, ConnectionError):
                pass
            time.sleep(0.5)
        cls._teardown_proc()
        raise unittest.SkipTest("server did not become ready")

    @classmethod
    def _request(cls, method, path, body=None, headers=None):
        url = f"https://127.0.0.1:{cls.port}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if cls.cookie:
            req.add_header("Cookie", cls.cookie)
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, context=cls.ctx, timeout=30) as resp:
                return resp.status, resp.read().decode("utf-8"), resp.headers
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8"), exc.headers

    @classmethod
    def _login(cls):
        status, _body, headers = cls._request("POST", "/api/login", {"password": PASSWORD})
        assert status == 200, f"login failed: {status}"
        set_cookie = headers.get("Set-Cookie", "")
        cls.cookie = set_cookie.split(";", 1)[0]
        status, body, _ = cls._request("GET", "/api/csrf")
        assert status == 200, f"csrf bootstrap failed: {status}"
        cls.csrf = json.loads(body)["csrfToken"]

    @classmethod
    def _post_api(cls, path, body, with_csrf=True):
        headers = {"X-CSRF-Token": cls.csrf} if with_csrf else {}
        status, raw, _ = cls._request("POST", path, body, headers)
        try:
            return status, json.loads(raw)
        except json.JSONDecodeError:
            return status, {"raw": raw}

    @classmethod
    def _teardown_proc(cls):
        if cls.proc and cls.proc.poll() is None:
            cls.proc.terminate()
            try:
                cls.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                cls.proc.kill()
        if getattr(cls, "stdout_log", None):
            cls.stdout_log.close()

    @classmethod
    def tearDownClass(cls):
        cls._teardown_proc()
        if cls.tmp:
            cls.tmp.cleanup()

    def test_01_save_lists_masked_and_never_returns_the_secret(self):
        status, body = self._post_api("/api/profiles", {
            "action": "save",
            "name": "prod-nw",
            "data": dict(STORED_PROFILE, password="prod-secret"),
        })
        self.assertEqual(status, 200, body)
        profile = body["profiles"]["prod-nw"]
        self.assertEqual(profile["password"], SAVED)
        self.assertNotIn("_enc_password", profile)
        self.assertNotIn("prod-secret", json.dumps(body))

        status, listed, _ = self._request("GET", "/api/profiles")
        self.assertEqual(status, 200)
        self.assertNotIn("prod-secret", listed)
        self.assertNotIn("_enc_password", listed)

    def test_02_saved_password_is_refused_for_another_host(self):
        status, body = self._post_api("/api/dashboard", {
            "profileName": "prod-nw",
            "restApiHost": "attacker.example",
            "restApiPort": "9090",
            "backupServerHost": "authc.corp.local",
            "backupServerPort": "9090",
            "username": "administrator",
            "password": SENTINEL,
            "verifyTls": True,
        })
        self.assertEqual(status, 400, body)
        self.assertIn("can only be used with that profile", body.get("error", ""))
        self.assertNotIn("prod-secret", json.dumps(body))

    def test_03_csrf_is_still_required_for_profile_writes(self):
        status, _ = self._post_api(
            "/api/profiles", {"action": "delete", "name": "prod-nw"}, with_csrf=False
        )
        self.assertEqual(status, 403)

    def test_04_delete_removes_the_profile(self):
        status, body = self._post_api("/api/profiles", {"action": "delete", "name": "prod-nw"})
        self.assertEqual(status, 200, body)
        self.assertNotIn("prod-nw", body["profiles"])
        self.assertEqual(body["sessionsPurged"], 0)


if __name__ == "__main__":
    unittest.main()
