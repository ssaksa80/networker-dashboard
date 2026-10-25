"""Password hashing, auth cookie, and CSRF token behavior."""
import base64
import hashlib
import hmac
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

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


def _forge_cookie(exp_offset_seconds: int, sid: str | None = "forged-session") -> str:
    """Build a correctly signed cookie with a chosen expiry (mirrors _make_auth_cookie).
    The session id is registered as live for an hour, whatever the cookie's own
    expiry, so the cookie's exp check is what a test exercises; sid=None leaves
    it out, like a cookie issued before session ids existed."""
    now = int(time.time())
    claims = {"iat": now, "exp": now + exp_offset_seconds}
    if sid is not None:
        claims["sid"] = sid
        auth.LOGIN_SESSIONS.add(sid, now + 3600)
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode("utf-8")).decode("ascii").rstrip("=")
    sig = base64.urlsafe_b64encode(
        hmac.new(AUTH_SECRET_KEY, payload.encode("ascii"), hashlib.sha256).digest()
    ).decode("ascii").rstrip("=")
    return f"{payload}.{sig}"


class LoginSessionsMixin:
    """A fresh login-session table backed by a throwaway file, per test."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sessions_file = Path(tmp.name) / "auth_sessions.json"
        for attr, value in (
            ("DATA_DIR", Path(tmp.name)),
            ("AUTH_SESSIONS_FILE", self.sessions_file),
            ("LOGIN_SESSIONS", auth._LoginSessions()),
        ):
            patcher = mock.patch.object(auth, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)


class TestAuthCookie(LoginSessionsMixin, unittest.TestCase):
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
        self.assertFalse(auth._verify_auth_cookie("päyload.sig"))


class TestLogoutRevokes(LoginSessionsMixin, unittest.TestCase):
    """Logout ends the session on the server, not just in one browser (review S6).

    The defect: _verify_auth_cookie checked only the HMAC and exp, and logout
    merely told the browser to drop the cookie, so a captured cookie stayed
    valid for the rest of its 12 h.
    """

    def test_revoked_cookie_is_rejected(self):
        cookie = auth._make_auth_cookie()
        self.assertTrue(auth.revoke_auth_cookie(cookie))
        self.assertFalse(auth._verify_auth_cookie(cookie))
        self.assertFalse(auth.revoke_auth_cookie(cookie), "already revoked")

    def test_other_logins_survive_one_logout(self):
        first, second = auth._make_auth_cookie(), auth._make_auth_cookie()
        auth.revoke_auth_cookie(first)
        self.assertTrue(auth._verify_auth_cookie(second))

    def test_forged_or_malformed_cookies_revoke_nothing(self):
        cookie = auth._make_auth_cookie()
        payload, _, sig = cookie.rpartition(".")
        self.assertFalse(auth.revoke_auth_cookie(f"{payload}.{'A' * len(sig)}"))
        self.assertFalse(auth.revoke_auth_cookie(""))
        self.assertTrue(auth._verify_auth_cookie(cookie))

    def test_cookie_without_a_session_id_is_rejected(self):
        self.assertFalse(auth._verify_auth_cookie(_forge_cookie(60, sid=None)))

    def test_logins_and_logouts_survive_a_restart(self):
        kept, revoked = auth._make_auth_cookie(), auth._make_auth_cookie()
        auth.revoke_auth_cookie(revoked)
        with mock.patch.object(auth, "LOGIN_SESSIONS", auth._LoginSessions()):  # fresh process
            self.assertTrue(auth._verify_auth_cookie(kept))
            self.assertFalse(auth._verify_auth_cookie(revoked))

    def test_expired_sessions_are_pruned_from_disk(self):
        auth.LOGIN_SESSIONS.add("stale", int(time.time()) - 1)
        auth._make_auth_cookie()
        stored = json.loads(self.sessions_file.read_text(encoding="utf-8"))
        self.assertNotIn("stale", stored)
        self.assertEqual(len(stored), 1)

    def test_table_is_capped(self):
        with mock.patch.object(auth._LoginSessions, "MAX_ENTRIES", 3):
            cookies = [auth._make_auth_cookie() for _ in range(5)]
            self.assertEqual(len(json.loads(self.sessions_file.read_text(encoding="utf-8"))), 3)
            self.assertTrue(auth._verify_auth_cookie(cookies[-1]))


class TestCsrfToken(LoginSessionsMixin, unittest.TestCase):
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
