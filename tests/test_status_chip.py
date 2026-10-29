"""The Scheduled Reports list shows each schedule's connection state as a
status chip (live / reconnectable / waiting).

Rewritten from a pytest-style module (bare test functions) that
`python -m unittest discover` never collected — as unittest these run in CI.
"""
import unittest
from pathlib import Path

A = Path(__file__).resolve().parents[1] / "nwdash" / "assets"


class TestStatusChip(unittest.TestCase):
    def test_schedule_rows_render_status_chip(self):
        js = (A / "app.js").read_text(encoding="utf-8")
        self.assertIn("em-status", js)
        for status in ("live", "reconnectable", "waiting"):
            self.assertIn(status, js)

    def test_status_chip_css(self):
        css = (A / "app.css").read_text(encoding="utf-8")
        self.assertIn(".em-status", css)


if __name__ == "__main__":
    unittest.main()
