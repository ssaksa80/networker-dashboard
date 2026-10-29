"""Unit tests for client-side logic in nwdash/assets/app.js.

Functions under test are extracted from the asset source by regex and
evaluated in a Node.js subprocess with minimal stubs — no browser needed.
Skips cleanly when node is not on PATH.
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

APP_JS = (Path(__file__).resolve().parent.parent / "nwdash" / "assets" / "app.js").read_text(encoding="utf-8")

NODE = shutil.which("node")


def _extract(pattern: str) -> str:
    match = re.search(pattern, APP_JS, re.DOTALL)
    if not match:
        raise AssertionError(f"pattern not found in app.js: {pattern!r}")
    return match.group(0)


def _run_node(script: str) -> dict:
    proc = subprocess.run(
        [NODE, "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(f"node failed:\n{proc.stderr}")
    return json.loads(proc.stdout)


@unittest.skipIf(NODE is None, "node is not on PATH; skipping app.js logic tests")
class TestParseTs(unittest.TestCase):
    """parseTs must handle the backend's strftime '%d-%m-%Y %H:%M:%S %Z' output."""

    @classmethod
    def setUpClass(cls):
        source = (
            _extract(r"const DDMMYYYY_TS_RE[^\n]*\n")
            + _extract(r"function parseTs\(str\) \{.*?\n    \}")
        )
        script = source + r"""
const out = {};
out.full = parseTs("18-07-2026 08:45:12 +04");
out.namedTz = parseTs("18-07-2026 08:45:12 Arabian Standard Time");
out.noTz = parseTs("18-07-2026 08:45:12");
out.dateOnly = parseTs("18-07-2026");
out.singleDigits = parseTs("5-3-2026 7:08:09 +03");
out.iso = parseTs("2026-07-18T08:45:12");
out.epochMs = parseTs(1767000000000);
out.garbage = String(parseTs("not a date"));
out.empty = String(parseTs(""));
out.nullish = String(parseTs(null));
out.badMonth = String(parseTs("18-13-2026 08:45:12 +04"));
out.expectedFull = new Date(2026, 6, 18, 8, 45, 12).getTime();
out.expectedDateOnly = new Date(2026, 6, 18).getTime();
out.expectedSingle = new Date(2026, 2, 5, 7, 8, 9).getTime();
out.expectedIso = new Date("2026-07-18T08:45:12").getTime();
process.stdout.write(JSON.stringify(out));
"""
        cls.result = _run_node(script)

    def test_backend_display_format_with_numeric_tz(self):
        self.assertEqual(self.result["full"], self.result["expectedFull"])

    def test_backend_display_format_with_named_tz(self):
        self.assertEqual(self.result["namedTz"], self.result["expectedFull"])

    def test_datetime_without_tz(self):
        self.assertEqual(self.result["noTz"], self.result["expectedFull"])

    def test_date_only(self):
        self.assertEqual(self.result["dateOnly"], self.result["expectedDateOnly"])

    def test_single_digit_day_month_hour(self):
        self.assertEqual(self.result["singleDigits"], self.result["expectedSingle"])

    def test_iso_fallback(self):
        self.assertEqual(self.result["iso"], self.result["expectedIso"])

    def test_epoch_millis_fallback(self):
        self.assertEqual(self.result["epochMs"], 1767000000000)

    def test_garbage_is_nan(self):
        self.assertEqual(self.result["garbage"], "NaN")

    def test_empty_is_nan(self):
        self.assertEqual(self.result["empty"], "NaN")

    def test_null_is_nan(self):
        self.assertEqual(self.result["nullish"], "NaN")

    def test_out_of_range_month_falls_back_to_nan(self):
        # "18-13-2026" fails the month guard and Date() cannot parse it either.
        self.assertEqual(self.result["badMonth"], "NaN")


@unittest.skipIf(NODE is None, "node is not on PATH; skipping app.js logic tests")
class TestLivePushKeepsItsSession(unittest.TestCase):
    """Review F1: /api/stream broadcasts every page's dashboard to every page.

    The old handler rendered any push, so a page connected to one NetWorker
    server showed another page's server and took over its sessionId (Export,
    Share and email then acted on that server), and diffAndNotify ran twice.
    The real handler runs here against a fake EventSource.
    """

    @classmethod
    def setUpClass(cls):
        source = _extract(r"    // ── SSE live push ─+\n.*?\n    function stopSSE\(\) \{.*?\n    \}")
        script = r"""
let sessionId = "page-a";
let latestDashboard = {ok: true, sessionId: "page-a", summary: {failedJobs: 0}};
const rendered = [];
let directDiffs = 0;
function renderDashboard(data) { rendered.push(data.sessionId || null); latestDashboard = data; }
function diffAndNotify() { directDiffs++; }
const listeners = {};
globalThis.EventSource = class {
  constructor(url) { this.url = url; }
  addEventListener(name, fn) { listeners[name] = fn; }
  close() {}
};
const window = globalThis;
""" + source + r"""
startSSE();
const push = (data) => listeners.dashboard({data: JSON.stringify(data)});
const out = {};
push({ok: true, sessionId: "page-b", summary: {failedJobs: 9}});
out.afterForeign = {rendered: rendered.length, sessionId, latest: latestDashboard.sessionId};
push({ok: true, summary: {failedJobs: 9}});
out.afterUnnamed = rendered.length;
push({ok: false, sessionId: "page-a"});
out.afterFailed = rendered.length;
push({ok: true, sessionId: "page-a", summary: {failedJobs: 1}});
out.afterOwn = rendered.slice();
out.directDiffs = directDiffs;
sessionId = null;
push({ok: true, sessionId: "page-a"});
out.afterDisconnect = rendered.length;
process.stdout.write(JSON.stringify(out));
"""
        cls.result = _run_node(script)

    def test_push_for_another_session_is_ignored(self):
        self.assertEqual(self.result["afterForeign"], {"rendered": 0, "sessionId": "page-a", "latest": "page-a"})

    def test_push_without_a_session_or_ok_is_ignored(self):
        self.assertEqual(self.result["afterUnnamed"], 0)
        self.assertEqual(self.result["afterFailed"], 0)

    def test_push_for_own_session_renders_once(self):
        self.assertEqual(self.result["afterOwn"], ["page-a"])

    def test_handler_leaves_diff_and_notify_to_render_dashboard(self):
        self.assertEqual(self.result["directDiffs"], 0)
        self.assertIn("diffAndNotify(latestDashboard, data)", _extract(r"function renderDashboard\(data\) \{.*?\n    \}"))

    def test_page_without_a_session_applies_no_push(self):
        self.assertEqual(self.result["afterDisconnect"], 1)


class TestSessionIdSourceGuards(unittest.TestCase):
    """Review F1: the page's sessionId comes only from its own answers."""

    def test_render_dashboard_never_takes_a_session_id(self):
        # renderDashboard also renders pushes, so it must not assign sessionId.
        render = _extract(r"function renderDashboard\(data\) \{.*?\n    \}")
        self.assertNotRegex(render, r"\bsessionId\s*=[^=]")

    def test_load_dashboard_takes_the_session_from_its_own_answer(self):
        load = _extract(r"async function loadDashboard\(options = \{\}\) \{.*?\n    \}")
        self.assertIn("if (data.sessionId) sessionId = data.sessionId;", load)
        self.assertIn("if (retryData.sessionId) sessionId = retryData.sessionId;", load)


class TestPaginationSourceGuards(unittest.TestCase):
    """Source-level regression guards for the pagination reset bug."""

    def test_render_table_does_not_unconditionally_reset_page_limit(self):
        # The old code reset pageLimit = PAGE_SIZE on EVERY renderTable() call,
        # so each SSE push / auto-refresh collapsed the expanded list and the
        # Show more / Show all buttons appeared dead.
        render_table = _extract(r"function renderTable\(\) \{.*?\n    \}")
        self.assertIn("pageLimitTable !== activeTable", render_table)

    def test_pagination_bar_hidden_rule_present(self):
        css = (Path(__file__).resolve().parent.parent / "nwdash" / "assets" / "app.css").read_text(encoding="utf-8")
        self.assertIn(".pagination-bar.hidden", css)

    def test_snapshot_head_reserves_badge_space(self):
        css = (Path(__file__).resolve().parent.parent / "nwdash" / "assets" / "app.css").read_text(encoding="utf-8")
        self.assertIn(".snap-cell-head", css)
        self.assertIn("snap-cell-head", APP_JS)


if __name__ == "__main__":
    unittest.main()
