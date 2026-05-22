import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

import networker_dashboard as nd


class _TmpDataDir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._orig = {
            "DATA_DIR": nd.DATA_DIR,
            "AUTH_KEY_FILE": nd.AUTH_KEY_FILE,
            "AUTH_CONFIG_FILE": nd.AUTH_CONFIG_FILE,
        }
        nd.DATA_DIR = Path(self._tmp)
        nd.AUTH_KEY_FILE = nd.DATA_DIR / ".auth_key"
        nd.AUTH_CONFIG_FILE = nd.DATA_DIR / "auth.json"

    def tearDown(self):
        nd.DATA_DIR = self._orig["DATA_DIR"]
        nd.AUTH_KEY_FILE = self._orig["AUTH_KEY_FILE"]
        nd.AUTH_CONFIG_FILE = self._orig["AUTH_CONFIG_FILE"]
        shutil.rmtree(self._tmp, ignore_errors=True)


class PasswordTests(_TmpDataDir):
    def test_set_and_verify_password(self):
        self.assertFalse(nd.auth_password_configured())
        nd.set_auth_password("hunter2")
        self.assertTrue(nd.auth_password_configured())
        self.assertTrue(nd.verify_auth_password("hunter2"))
        self.assertFalse(nd.verify_auth_password("wrong"))

    def test_verify_without_config_is_false(self):
        self.assertFalse(nd.verify_auth_password("anything"))

    def test_password_hash_not_plaintext_at_rest(self):
        nd.set_auth_password("plaintext-secret")
        raw = nd.AUTH_CONFIG_FILE.read_text(encoding="utf-8")
        self.assertNotIn("plaintext-secret", raw)
        data = json.loads(raw)
        self.assertIn("salt", data)
        self.assertIn("hash", data)


class CookieTests(unittest.TestCase):
    def test_cookie_roundtrip(self):
        self.assertTrue(nd._verify_auth_cookie(nd._make_auth_cookie()))

    def test_tampered_cookie_rejected(self):
        cookie = nd._make_auth_cookie()
        payload, _, sig = cookie.rpartition(".")
        self.assertFalse(nd._verify_auth_cookie(payload + ".AAAA" + sig))

    def test_expired_cookie_rejected(self):
        import base64 as b64
        import hashlib as _hashlib
        import hmac as _hmac
        now = int(time.time())
        payload = b64.urlsafe_b64encode(
            json.dumps({"iat": now - 100000, "exp": now - 1}).encode()
        ).decode().rstrip("=")
        sig = b64.urlsafe_b64encode(
            _hmac.new(nd.AUTH_SECRET_KEY, payload.encode(), _hashlib.sha256).digest()
        ).decode().rstrip("=")
        self.assertFalse(nd._verify_auth_cookie(f"{payload}.{sig}"))

    def test_garbage_cookie_rejected(self):
        self.assertFalse(nd._verify_auth_cookie("not-a-cookie"))
        self.assertFalse(nd._verify_auth_cookie(""))


class RateLimitTests(unittest.TestCase):
    def setUp(self):
        nd.LOGIN_ATTEMPTS.clear()

    def tearDown(self):
        nd.LOGIN_ATTEMPTS.clear()

    def test_rate_limit_after_max_attempts(self):
        ip = "10.0.0.1"
        for _ in range(nd.LOGIN_MAX_ATTEMPTS):
            self.assertFalse(nd._login_rate_limited(ip))
            nd._record_login_failure(ip)
        self.assertTrue(nd._login_rate_limited(ip))
        nd._clear_login_failures(ip)
        self.assertFalse(nd._login_rate_limited(ip))


class LoopbackTests(unittest.TestCase):
    def test_loopback_detection(self):
        for host in ("127.0.0.1", "localhost", "::1"):
            self.assertTrue(nd._is_loopback_bind(host))
        for host in ("0.0.0.0", "", "::", "192.168.1.5"):
            self.assertFalse(nd._is_loopback_bind(host))


if __name__ == "__main__":
    unittest.main()
