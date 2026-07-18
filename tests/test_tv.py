"""TV wall-display mode (v2.8.0): source guards for the validated design spec,
generated theme blocks, minimize wiring, and bundle allow-list coverage."""
import re
import unittest
from pathlib import Path

from nwdash.ui import tv_page_html

PROJECT = Path(__file__).resolve().parent.parent
ASSETS = PROJECT / "nwdash" / "assets"

EXPECTED_THEMES = {
    "default", "midnight", "graphite", "contrast", "ocean", "forest",
    "ruby", "steel", "arctic", "citrus", "harbor", "ember",
    "violet", "sandstone", "carbon",
}

# The user's submitted size spec (final_design_selections.json) is the contract.
SPEC_VALUES = (
    "--scale:.9", "--gap:1.5rem", "--r:.7rem",
    "--headH:4.2vh", "--clock:1.5rem",
    "--anaH:28vh", "--donut:11.5rem",
    "--kpiH:7vh", "--kpiNum:2.9rem",
    "--healthH:7.5vh", "--healthNum:2rem",
    "--snapH:10vh", "--snapNum:1.9rem",
    "--rows:14", "--tblFont:1.5rem",
)


class TestTvDesignSpec(unittest.TestCase):
    def setUp(self):
        self.css = (ASSETS / "tv.css").read_text(encoding="utf-8")
        self.js = (ASSETS / "tv.js").read_text(encoding="utf-8")
        self.html = (ASSETS / "tv.html").read_text(encoding="utf-8")

    def test_fluid_scaling_rule(self):
        # 1rem = 100vw/240 (4K reference) + zoom compensation, no scrolling.
        self.assertIn("font-size:calc(100vw / 240)", self.css)
        self.assertIn("width:calc(100vw / var(--scale))", self.css)
        self.assertIn("height:calc(100vh / var(--scale))", self.css)
        self.assertIn("overflow:hidden", self.css)
        self.assertIn("zoom:var(--scale)", self.css)

    def test_submitted_size_spec_values_present(self):
        for value in SPEC_VALUES:
            self.assertIn(value, self.css, f"tv.css missing spec value {value!r}")

    def test_auto_cycle_defaults_to_15s(self):
        self.assertIn("15000", self.js)
        self.assertIn("__TV_CYCLE_MS__", self.js)  # test-harness override hook

    def test_minimize_uses_flex_longhand_and_boost(self):
        # flex-grow/shrink/basis longhand on purpose: the flex shorthand drops
        # a var() basis, silently breaking the proportional auto-expansion.
        for section in ("--anaH", "--kpiH", "--healthH", "--snapH"):
            self.assertIn(
                f"flex-grow:1;flex-shrink:1;flex-basis:var({section})", self.css
            )
        self.assertIn("body.jobs-min{--boost:1.25}", self.css)
        self.assertIn("body.jobs-min .tblbody{display:none}", self.css)
        self.assertIn("nw_tv_jobs_min", self.js)  # persisted minimized state

    def test_jobs_table_has_real_columns_and_grid(self):
        self.assertIn("13% 17% 7% 9% 20% 8% 7% 19%", self.js)
        for column in ("Client", "Job", "Policy", "Status", "Started",
                       "Duration", "Size", "Message"):
            self.assertIn(f'"{column}"', self.js)

    def test_live_data_wiring(self):
        self.assertIn("/api/current-dashboard", self.js)
        self.assertIn('new EventSource("/api/stream")', self.js)
        self.assertIn("/api/snapshots?range=7d", self.js)
        self.assertIn("/api/ui-theme", self.js)
        self.assertIn("Waiting for dashboard data", self.html + self.js)


class TestTvPageHtml(unittest.TestCase):
    def setUp(self):
        self.page = tv_page_html()

    def test_all_markers_replaced(self):
        for marker in ("__TV_CSS__", "__TV_JS__", "__NETWORKER_LOGO_SRC__",
                       "/*__TV_THEME_CSS__*/"):
            self.assertNotIn(marker, self.page)

    def test_logo_embedded_as_data_uri(self):
        self.assertIn("data:image/", self.page)

    def test_all_15_theme_blocks_generated(self):
        named = set()
        for match in re.finditer(r'body\[data-theme="(\w+)"\]\{([^}]*)\}', self.page):
            body = match.group(2)
            if "--headbg:" in body and "--pillink:" in body and "--brand:" in body:
                named.add(match.group(1))
        self.assertEqual(named, EXPECTED_THEMES)

    def test_prototype_header_gradients_kept_exactly(self):
        self.assertIn("linear-gradient(135deg,#0d3b22,#1d5c35 60%,#1b5230)", self.page)
        self.assertIn("linear-gradient(135deg,#1d2b3d,#33506e 65%,#2c4560)", self.page)
        self.assertIn("linear-gradient(135deg,#04181c,#0f3d46 65%,#0c333b)", self.page)

    def test_maintainer_credit_present(self):
        self.assertIn("SHAIKH SHOAIB", self.page)
        self.assertIn("Sr. Advisor Delivery Specialist", self.page)


class TestTvShipsInBundle(unittest.TestCase):
    def test_bundle_allow_list_includes_tv_assets(self):
        script = (PROJECT / "scripts" / "build-bundle.ps1").read_text(encoding="utf-8")
        for name in ("tv.html", "tv.css", "tv.js"):
            self.assertIn(f"nwdash\\assets\\{name}", script)

    def test_dashboard_topbar_links_to_tv(self):
        html = (ASSETS / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn('id="tvModeBtn"', html)
        js = (ASSETS / "app.js").read_text(encoding="utf-8")
        self.assertIn('window.open("/tv"', js)


if __name__ == "__main__":
    unittest.main()
