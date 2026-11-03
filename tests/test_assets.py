"""The pages link their CSS, JS and logo as cacheable files (review F10).

The dashboard was 1.4 MB, the 160 KB logo inlined five times as base64 and
every stylesheet and script inline, so a browser fetched all of it on every
load. Now each file has a URL naming a hash of its bytes, served from a fixed
table of exact URLs (never a path mapped onto a file), so a cached copy is never
stale after an upgrade. The HTTP side is in tests/test_server_http.py.
"""
import base64
import hashlib
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from served import with_assets

from nwdash import ui
from nwdash.ui import (
    ASSET_CACHE_CONTROL,
    NETWORKER_LOGO,
    admin_page_html,
    asset_response,
    dashboard_html,
    networker_logo_src,
    tv_page_html,
)

PAGES = {"dashboard": dashboard_html, "admin": admin_page_html, "tv": tv_page_html}
SHIPPED = {"networker-logo.png", "app.css", "app.js", "design.css", "design.js",
           "admin.css", "admin.js", "tv.css", "tv.js"}
_URL = re.compile(r"/assets/(?P<stem>[a-z-]+)\.(?P<hash>[0-9a-f]{16})\.(?P<ext>css|js|png|svg)")


class TestAssetTable(unittest.TestCase):
    def test_it_serves_exactly_the_shipped_files(self):
        names = set()
        for url in ui._ASSET_ROUTES:
            match = _URL.fullmatch(url)
            self.assertIsNotNone(match, url)
            names.add(f"{match['stem']}.{match['ext']}")
        self.assertEqual(names, SHIPPED)

    def test_each_url_names_the_hash_of_its_bytes(self):
        for url, (body, _) in ui._ASSET_ROUTES.items():
            self.assertEqual(_URL.fullmatch(url)["hash"], hashlib.sha256(body).hexdigest()[:16], url)

    def test_text_is_served_with_lf_only(self):
        # The hash is over the served bytes, so a CRLF checkout must not change them.
        for url, (body, content_type) in ui._ASSET_ROUTES.items():
            if content_type.startswith("text/"):
                self.assertNotIn(b"\r", body, url)

    def test_any_other_path_is_not_an_asset(self):
        real = next(url for url in ui._ASSET_ROUTES if url.endswith(".js"))
        for path in ("/assets/app.js", "/assets/app.0000000000000000.js", real + "/", real.upper(),
                     "/assets/", "/assets/../nwdash/ui.py", "/nwdash/assets/app.js", real + "?v=1"):
            self.assertIsNone(asset_response(path), path)
        self.assertIsNotNone(asset_response(real))

    def test_cached_for_a_year_and_never_revalidated(self):
        self.assertEqual(ASSET_CACHE_CONTROL, "public, max-age=31536000, immutable")


class TestPagesLinkTheirAssets(unittest.TestCase):
    def test_pages_stay_small(self):
        for name, page in PAGES.items():
            self.assertLess(len(page().encode("utf-8")), 100_000, name)

    def test_no_inline_script_is_left(self):
        # Review S14 can then drop 'unsafe-inline' from script-src for these pages.
        for name, page in PAGES.items():
            self.assertEqual(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", page()), [], name)

    def test_the_logo_is_linked_not_inlined(self):
        for name, page in PAGES.items():
            loaded = with_assets(page())
            self.assertNotIn("data:image/", loaded, name)
            self.assertNotIn("__NETWORKER_LOGO_SRC__", loaded, name)

    def test_every_url_in_the_css_resolves(self):
        for url, (body, content_type) in ui._ASSET_ROUTES.items():
            if content_type.startswith("text/css"):
                for linked in re.findall(r'url\("(/[^"]*)"\)', body.decode("utf-8")):
                    self.assertIsNotNone(asset_response(linked), f"{url} links {linked}")

    def test_the_dashboard_css_shows_the_logo(self):
        # app.css draws the logo twice as a background image.
        css = next(body for url, (body, _) in ui._ASSET_ROUTES.items() if url.startswith("/assets/app.")
                   and url.endswith(".css"))
        logo = next(url for url in ui._ASSET_ROUTES if url.startswith("/assets/networker-logo."))
        self.assertEqual(css.decode("utf-8").count(f'url("{logo}")'), 2)


class TestLogoSource(unittest.TestCase):
    def _load(self, path):
        with mock.patch.object(ui, "NETWORKER_LOGO_PATH", path):
            return ui._load_logo()

    def test_a_logo_file_beside_the_app_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "networker-logo.png"
            path.write_bytes(b"\x89PNG custom")
            self.assertEqual(self._load(path), (b"\x89PNG custom", "png"))

    def test_a_missing_or_unreadable_file_falls_back_to_the_embedded_png(self):
        # It is read at import: raising here would stop the service starting.
        embedded = base64.b64decode(ui.NETWORKER_LOGO_PNG_BASE64.strip())
        with tempfile.TemporaryDirectory() as tmp:
            for path in (Path(tmp) / "absent.png", Path(tmp)):  # a folder cannot be read as a file
                self.assertEqual(self._load(path), (embedded, "png"), path)


class TestEmailReportsKeepTheInlineLogo(unittest.TestCase):
    def test_reports_get_a_data_uri_of_the_same_logo(self):
        # A mail has no server to fetch the logo from.
        src = networker_logo_src()
        self.assertTrue(src.startswith("data:image/png;base64,"))
        self.assertEqual(base64.b64decode(src.split(",", 1)[1]), NETWORKER_LOGO)
        self.assertTrue(NETWORKER_LOGO.startswith(b"\x89PNG"))


if __name__ == "__main__":
    unittest.main()
