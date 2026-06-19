import unittest
import networker_dashboard as nd


class AccountButtonContrastTests(unittest.TestCase):
    def test_topbar_collapse_toggle_has_explicit_light_style(self):
        html = nd.HTML_PAGE
        self.assertIn(".topbar .collapse-toggle", html)
        idx = html.index(".topbar .collapse-toggle")
        block = html[idx:idx + 220]
        self.assertIn("color: #ffffff", block)

    def test_collapse_toggle_uses_correct_surface_variable(self):
        html = nd.HTML_PAGE
        self.assertNotIn("var(--surface2)", html)


if __name__ == "__main__":
    unittest.main()
