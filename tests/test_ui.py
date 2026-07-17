"""Embedded HTML pages keep their security-relevant client wiring."""
import unittest

from nwdash.ui import dashboard_html, login_page_html, read_only_view_html


class TestDashboardHtml(unittest.TestCase):
    def test_contains_csrf_header_wiring(self):
        self.assertIn("X-CSRF-Token", dashboard_html())

    def test_contains_verify_tls_checkbox(self):
        self.assertIn("asVerifyTls", dashboard_html())

    def test_logo_placeholder_replaced(self):
        self.assertNotIn("__NETWORKER_LOGO_SRC__", dashboard_html())


class TestLoginPageHtml(unittest.TestCase):
    def test_posts_to_login_api(self):
        self.assertIn("/api/login", login_page_html())


class TestReadOnlyViewHtml(unittest.TestCase):
    def test_embeds_share_token(self):
        html = read_only_view_html("tok-abc123")
        self.assertIn("tok-abc123", html)


if __name__ == "__main__":
    unittest.main()
