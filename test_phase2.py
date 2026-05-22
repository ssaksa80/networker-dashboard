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


if __name__ == "__main__":
    unittest.main()
