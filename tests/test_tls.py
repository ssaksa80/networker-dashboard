"""Outbound TLS verification defaults (fail-closed unless explicitly disabled)."""
import ssl
import unittest

from nwdash.restapi import ssl_context_for_api


class TestSslContextForApi(unittest.TestCase):
    def test_verify_true_is_fail_closed(self):
        ctx = ssl_context_for_api(True)
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)

    def test_verify_false_disables_verification(self):
        ctx = ssl_context_for_api(False)
        self.assertEqual(ctx.verify_mode, ssl.CERT_NONE)
        self.assertFalse(ctx.check_hostname)


if __name__ == "__main__":
    unittest.main()
