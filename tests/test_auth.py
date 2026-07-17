"""Password hashing, auth cookie, and CSRF token behavior."""
import base64
import hashlib
import hmac
import json
import tempfile
import time
import unittest
from pathlib import Path

from nwdash import auth
from nwdash.secrets import AUTH_SECRET_KEY


class TestPasswordRoundTrip(unittest.TestCase):
    """set_auth_password / verify_auth_password against a temp auth file."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp_dir = Path(self._tmp.name)
        self._orig_file = auth.AUTH_CONFIG_FILE
        self._orig_dir = auth.DATA_DIR
        auth.AUTH_CONFIG_FILE = tmp_dir / "auth.json"
        auth.DATA_DIR = tmp_dir

    def tearDown(self):
        auth.AUTH_CONFIG_FILE = self._orig_file
        auth.DATA_DIR = self._orig_dir
        self._tmp.cleanup()

    def test_round_trip_and_wrong_password(self):
        self.assertFalse(auth.auth_password_configured())
        auth.set_auth_password("correct horse battery staple")
        self.assertTrue(auth.auth_password_configured())
        self.assertTrue(auth.verify_auth_password("correct horse battery staple"))
        self.assertFalse(auth.verify_auth_password("wrong password"))
        self.assertFalse(auth.verify_auth_password(""))

    def test_hash_is_salted(self):
        h1 = auth._hash_password("pw", b"salt-one" * 2)
        h2 = auth._hash_password("pw", b"salt-two" * 2)
        self.assertNotEqual(h1, h2)
        self.assertEqual(h1, auth._hash_password("pw", b"salt-one" * 2))


def _forge_cookie(exp_offset_seconds: int) -> str:
    """Build a correctly signed cookie with a chosen expiry (mirrors _make_auth_cookie)."""
    now = int(time.time())
    payload = base64.urlsafe_b64encode(
        json.dumps({"iat": now, "exp": now + exp_offset_seconds}).encode("utf-8")
    ).decode("ascii").rstrip("=")
    sig = base64.urlsafe_b64encode(
        hmac.new(AUTH_SECRET_KEY, payload.encode("ascii"), hashlib.sha256).digest()
    ).decode("ascii").rstrip("=")
    return f"{payload}.{sig}"


class TestAuthCookie(unittest.TestCase):
    def test_make_verify_round_trip(self):
        cookie = auth._make_auth_cookie()
        self.assertTrue(auth._verify_auth_cookie(cookie))

    def test_tampered_signature_rejected(self):
        cookie = auth._make_auth_cookie()
        payload, _, sig = cookie.rpartition(".")
        flipped = ("A" if sig[0] != "A" else "B") + sig[1:]
        self.assertFalse(auth._verify_auth_cookie(f"{payload}.{flipped}"))

    def test_tampered_payload_rejected(self):
        cookie = auth._make_auth_cookie()
        payload, _, sig = cookie.rpartition(".")
        flipped = ("A" if payload[0] != "A" else "B") + payload[1:]
        self.assertFalse(auth._verify_auth_cookie(f"{flipped}.{sig}"))

    def test_expired_cookie_rejected(self):
        self.assertFalse(auth._verify_auth_cookie(_forge_cookie(-60)))
        self.assertTrue(auth._verify_auth_cookie(_forge_cookie(60)))

    def test_malformed_rejected(self):
        self.assertFalse(auth._verify_auth_cookie(""))
        self.assertFalse(auth._verify_auth_cookie("no-dot-here"))
        self.assertFalse(auth._verify_auth_cookie(".onlysig"))
        self.assertFalse(auth._verify_auth_cookie("onlypayload."))


class TestCsrfToken(unittest.TestCase):
    def test_same_cookie_same_token(self):
        cookie = auth._make_auth_cookie()
        self.assertEqual(auth._make_csrf_token(cookie), auth._make_csrf_token(cookie))
        self.assertTrue(auth._verify_csrf_token(cookie, auth._make_csrf_token(cookie)))

    def test_different_cookie_different_token(self):
        c1 = _forge_cookie(3600)
        c2 = _forge_cookie(7200)  # different exp -> different payload
        self.assertNotEqual(auth._make_csrf_token(c1), auth._make_csrf_token(c2))
        self.assertFalse(auth._verify_csrf_token(c1, auth._make_csrf_token(c2)))

    def test_rejects_empty_and_bad(self):
        cookie = auth._make_auth_cookie()
        token = auth._make_csrf_token(cookie)
        self.assertFalse(auth._verify_csrf_token(cookie, ""))
        self.assertFalse(auth._verify_csrf_token("", token))
        self.assertFalse(auth._verify_csrf_token(cookie, "bogus-token"))


if __name__ == "__main__":
    unittest.main()
