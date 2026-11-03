"""A page as the browser gets it: the HTML plus the CSS and JS it links.

The pages link their stylesheets and scripts (review F10) instead of inlining
them. Tests that check what a page ships read it through with_assets(), which
puts each linked file back in place and fails on a link the server would not
answer, so they still see the whole page in its load order.

Not a test module itself: discovery only collects test*.py.
"""
import re

from nwdash.ui import asset_response

_LINKS = re.compile(r'<link rel="stylesheet" href="([^"]+)">|<script src="([^"]+)"></script>')


def _inline(match: re.Match) -> str:
    stylesheet, script = match.groups()
    asset = asset_response(stylesheet or script)
    if asset is None:
        raise AssertionError(f"the page links {stylesheet or script}, which the server does not serve")
    text = asset[0].decode("utf-8")
    return f"<style>{text}</style>" if stylesheet else f"<script>{text}</script>"


def with_assets(html: str) -> str:
    return _LINKS.sub(_inline, html)
