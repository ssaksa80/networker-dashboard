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

    def test_contains_email_profile_wiring(self):
        """Email Alert Automation modal ships the saved-profiles CARD list
        (v2.7.0: cards + ON/OFF toggles replaced the select/Load/Delete bar)
        and the client-side calls for the profile CRUD + toggle actions."""
        html = dashboard_html()
        for marker in (
            "emailProfileCards",
            "emailProfileName",
            "emailProfileSaveBtn",
            "ep-edit",
            "ep-del",
            "ep-toggle",
            "save-profile",
            "list-profiles",
            "get-profile",
            "delete-profile",
            "toggle-profile",
            "emailProfileName: loadedEmailProfileName",
        ):
            self.assertIn(marker, html, f"dashboard html missing {marker!r}")


class TestLoginPageHtml(unittest.TestCase):
    def test_posts_to_login_api(self):
        self.assertIn("/api/login", login_page_html())


class TestReadOnlyViewHtml(unittest.TestCase):
    def test_embeds_share_token(self):
        html = read_only_view_html("tok-abc123")
        self.assertIn("tok-abc123", html)


if __name__ == "__main__":
    unittest.main()
