"""The four designs, the sidebar and the admin console page, as shipped.

Static checks on the assembled pages and the asset files: the elements app.js
and design.js look up exist exactly once, the pages carry no inline event
handlers and load nothing from another host (air-gapped deployments, CSP),
and the scripts parse (when node is on PATH).
"""
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from nwdash.config import THEME_PALETTES, UI_DESIGNS
from nwdash.ui import admin_page_html, dashboard_html

ASSETS = Path(__file__).resolve().parent.parent / "nwdash" / "assets"
NODE = shutil.which("node")


def _count_id(html: str, element_id: str) -> int:
    return len(re.findall(r'\bid="' + re.escape(element_id) + r'"', html))


class TestDashboardFrame(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = dashboard_html()

    def test_moved_controls_exist_once(self):
        # Moved from the topbar account menu and the page into the sidebar;
        # app.js binds them by id.
        for element_id in ("showConnectionBtn", "addServerBtn", "shareBtn", "alertConfigBtn", "tvModeBtn", "logoutBtn",
                           "connectionPanel", "connectionForm", "connCollapsedBar", "collapsedEditBtn", "topStatus"):
            self.assertEqual(_count_id(self.html, element_id), 1, element_id)

    def test_sidebar_design_toggle_and_hero_are_present(self):
        for element_id in ("navSidebar", "navFloatBtn", "navCollapseBtn", "navFab", "navOpenBtn", "designBtn", "designMenu",
                           "schemeBtn", "flowHero", "fhFlow", "fhArc", "fhRibbon", "fhPipe", "healthSection", "snapshotSection"):
            self.assertEqual(_count_id(self.html, element_id), 1, element_id)

    def test_every_nav_target_exists(self):
        for target in re.findall(r'data-nav-go="([^"]+)"', self.html):
            if target != "top":
                self.assertEqual(_count_id(self.html, target), 1, target)
        tables = set(re.findall(r'class="tab(?: active)?" type="button" data-table="([^"]+)"', self.html))
        for table in re.findall(r'data-nav-table="([^"]+)"', self.html):
            self.assertIn(table, tables)

    def test_design_script_runs_before_app_script(self):
        self.assertLess(self.html.index("window.NWDesign = {"), self.html.index("function renderDashboard(data)"))

    def test_app_js_hands_each_dashboard_to_the_designs(self):
        app = (ASSETS / "app.js").read_text(encoding="utf-8")
        self.assertIn("window.NWDesign.onDashboard(data)", app)
        self.assertEqual(app.count("window.NWDesign.onReset()"), 2)


class TestDesigns(unittest.TestCase):
    def test_css_and_js_know_the_same_designs(self):
        css = (ASSETS / "design.css").read_text(encoding="utf-8")
        js = (ASSETS / "design.js").read_text(encoding="utf-8")
        for design in UI_DESIGNS:
            self.assertIn(f"{design}: {{label:", js)
            if design != "daylight":  # daylight takes the theme's palette
                self.assertIn(f'body[data-design="{design}"] {{', css)
                self.assertIn(f'body[data-design="{design}"][data-scheme="alt"] {{', css)

    def test_reduced_motion_turns_motion_off(self):
        js = (ASSETS / "design.js").read_text(encoding="utf-8")
        self.assertIn("prefers-reduced-motion: reduce", js)
        css = (ASSETS / "design.css").read_text(encoding="utf-8")
        self.assertIn('body[data-motion="off"] *', css)

    def test_admin_page_has_every_theme(self):
        html = admin_page_html()
        for name in THEME_PALETTES:
            if name != "default":
                self.assertIn(f'body[data-theme="{name}"]{{', html)


class TestAdminPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = admin_page_html()
        cls.js = (ASSETS / "admin.js").read_text(encoding="utf-8")

    def test_markers_are_all_replaced(self):
        self.assertNotRegex(self.html, r"__[A-Z_]+__")

    def test_every_tab_has_a_panel_and_a_loader(self):
        tabs = re.findall(r'\["([a-z]+)", "[^"]+"\]', self.js.split("const THEMES")[0])
        self.assertEqual(len(tabs), 12)
        for tab in tabs:
            self.assertEqual(self.html.count(f'class="adm-panel" data-tab="{tab}"'), 1, tab)
            self.assertRegex(self.js, rf"\b{tab}: ", tab)

    def test_admin_requests_send_the_csrf_header(self):
        self.assertIn('opts.headers["X-CSRF-Token"] = csrf', self.js)


class TestPagesStaySelfContained(unittest.TestCase):
    def test_no_inline_event_handlers(self):
        for name, html in (("dashboard", dashboard_html()), ("admin", admin_page_html())):
            self.assertIsNone(re.search(r"<[^>]+\son[a-z]+\s*=", html), name)

    def test_no_external_scripts_styles_or_fonts(self):
        for asset in ("design.css", "design.js", "admin.html", "admin.css", "admin.js", "dashboard.html"):
            text = (ASSETS / asset).read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"(src|href)=\"https?://", text), asset)
            self.assertNotIn("@import", text, asset)
            self.assertNotIn("fonts.googleapis", text, asset)

    @unittest.skipIf(NODE is None, "node is not on PATH")
    def test_scripts_parse(self):
        for asset in ("design.js", "admin.js", "app.js"):
            proc = subprocess.run(
                [NODE, "-e", "new Function(require('fs').readFileSync(process.argv[1], 'utf8'))", str(ASSETS / asset)],
                capture_output=True, text=True, timeout=30, check=False,
            )
            self.assertEqual(proc.returncode, 0, f"{asset}: {proc.stderr}")


if __name__ == "__main__":
    unittest.main()
