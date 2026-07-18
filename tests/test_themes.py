"""Theme system stays in lockstep across CSS, picker JS, HTML select, and
backend palettes, and the large-display scaling breakpoints exist."""
import re
import unittest
from pathlib import Path

from nwdash.config import THEME_PALETTES
from nwdash.snapshots import parse_theme
from nwdash.ui import dashboard_html

ASSETS = Path(__file__).resolve().parent.parent / "nwdash" / "assets"

EXPECTED_THEMES = {
    "default", "midnight", "graphite", "contrast", "ocean", "forest",
    "ruby", "steel", "arctic", "citrus", "harbor", "ember",
    "violet", "sandstone", "carbon",
}
PALETTE_KEYS = {
    "bg", "surface", "surface2", "ink", "muted", "line",
    "brand", "brandInk", "green", "red", "amber", "blue",
}


class TestThemeLockstep(unittest.TestCase):
    def setUp(self):
        self.css = (ASSETS / "app.css").read_text(encoding="utf-8")
        self.js = (ASSETS / "app.js").read_text(encoding="utf-8")
        self.html = (ASSETS / "dashboard.html").read_text(encoding="utf-8")

    def test_css_has_all_15_theme_variable_blocks(self):
        # :root is the default theme; the other 14 are body[data-theme] blocks
        # that define --brand (topbar-only override blocks don't).
        named = set()
        for match in re.finditer(
            r'body\[data-theme="(\w+)"\]\s*\{([^}]*)\}', self.css
        ):
            if "--brand:" in match.group(2):
                named.add(match.group(1))
        self.assertEqual(named | {"default"}, EXPECTED_THEMES)
        self.assertIn(":root", self.css)
        # every named theme also styles the topbar
        for name in EXPECTED_THEMES - {"default"}:
            self.assertIn(f'body[data-theme="{name}"] .topbar', self.css, name)

    def test_backend_palettes_have_all_15_themes_with_full_key_set(self):
        self.assertEqual(set(THEME_PALETTES), EXPECTED_THEMES)
        for name, palette in THEME_PALETTES.items():
            self.assertEqual(set(palette), PALETTE_KEYS, name)

    def test_parse_theme_accepts_new_themes_and_rejects_garbage(self):
        for name in ("carbon", "violet", "sandstone", "ember"):
            self.assertEqual(parse_theme(name), name)
        self.assertEqual(parse_theme("Carbon"), "carbon")  # case-insensitive
        self.assertEqual(parse_theme("garbage"), "default")
        self.assertEqual(parse_theme(None), "default")
        self.assertEqual(parse_theme(""), "default")

    def test_js_theme_picker_names_match_backend(self):
        block = re.search(r"const THEMES = \[(.*?)\];", self.js, re.S)
        self.assertIsNotNone(block, "THEMES const missing from app.js")
        names = set(re.findall(r'\[\s*"(\w+)"', block.group(1)))
        self.assertEqual(names, EXPECTED_THEMES)
        self.assertIn("renderThemePicker", self.js)
        self.assertIn("syncThemeChips", self.js)

    def test_html_select_fallback_lists_all_15_themes(self):
        select = re.search(
            r'<select id="themeSelect".*?</select>', self.html, re.S
        )
        self.assertIsNotNone(select)
        options = set(re.findall(r'<option value="(\w+)"', select.group(0)))
        self.assertEqual(options, EXPECTED_THEMES)
        self.assertIn('id="themePicker"', self.html)

    def test_rendered_dashboard_ships_picker(self):
        html = dashboard_html()
        self.assertIn("themePicker", html)
        self.assertIn('data-theme="carbon"', self.css)


class TestLargeDisplayScaling(unittest.TestCase):
    def setUp(self):
        self.css = (ASSETS / "app.css").read_text(encoding="utf-8")

    def test_breakpoints_exist(self):
        for bp in ("1920px", "2560px", "3000px"):
            self.assertIn(f"@media (min-width: {bp})", self.css)

    def test_scaled_primitives_have_no_inline_font_size_left(self):
        # Inline styles override the media-query classes, so the scaled
        # elements must not carry inline font-size in markup templates.
        for asset in ("dashboard.html", "app.js"):
            text = (ASSETS / asset).read_text(encoding="utf-8")
            self.assertNotIn('style="font-size', text, asset)


if __name__ == "__main__":
    unittest.main()
