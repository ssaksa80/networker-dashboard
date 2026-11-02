"""Unit tests for client-side logic in nwdash/assets/app.js and admin.js.

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

ASSETS = Path(__file__).resolve().parent.parent / "nwdash" / "assets"
APP_JS = (ASSETS / "app.js").read_text(encoding="utf-8")
ADMIN_JS = (ASSETS / "admin.js").read_text(encoding="utf-8")

NODE = shutil.which("node")


def _extract(pattern: str, source: str = APP_JS) -> str:
    match = re.search(pattern, source, re.DOTALL)
    if not match:
        name = "admin.js" if source is ADMIN_JS else "app.js"
        raise AssertionError(f"pattern not found in {name}: {pattern!r}")
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


@unittest.skipIf(NODE is None, "node is not on PATH; skipping app.js logic tests")
class TestFetchWrapperReloadsOnlyForGatewayLogin(unittest.TestCase):
    """Review F2: the fetch wrapper reloaded the page on any /api 401.

    NetWorker refusing its credentials and an expired dashboard session also
    answered 401, so their errors never showed and pages could reload in a loop.
    Only the gateway's 401 carries X-NWDash-Login (tests/test_login_required.py
    checks the server side). The real wrapper runs here against a fake fetch.
    """

    @classmethod
    def setUpClass(cls):
        wrapper = _extract(r"    \(function\(\)\{\n      const _fetch = window\.fetch;.*?\n    \}\)\(\);")
        script = r"""
let reloads = 0;
globalThis.location = {reload() { reloads++; }};
let reply = null;
globalThis.fetch = async (url) => String(url) === "/api/csrf"
  ? new Response('{"csrfToken":"t"}', {status: 200})
  : reply();
const window = globalThis;
""" + wrapper + r"""
(async () => {
  const out = {};
  const call = async (name, url, method, status, headers) => {
    reply = () => new Response("{}", {status, headers});
    const before = reloads;
    const r = await fetch(url, {method, headers: {"Content-Type": "application/json"}});
    out[name] = {status: r.status, reloads: reloads - before};
  };
  await call("unmarked401", "/api/dashboard", "POST", 401, {});
  await call("sessionExpired", "/api/dashboard", "POST", 409, {});
  await call("networkerRefused", "/api/dashboard", "POST", 502, {});
  await call("loginGonePost", "/api/dashboard", "POST", 401, {"X-NWDash-Login": "required"});
  await call("loginGoneGet", "/api/status", "GET", 401, {"X-NWDash-Login": "required"});
  await call("notApi", "/tv", "GET", 401, {"X-NWDash-Login": "required"});
  process.stdout.write(JSON.stringify(out));
})();
"""
        cls.result = _run_node(script)

    def test_unmarked_401_is_left_to_the_caller(self):
        self.assertEqual(self.result["unmarked401"], {"status": 401, "reloads": 0})

    def test_upstream_failures_do_not_reload(self):
        self.assertEqual(self.result["sessionExpired"], {"status": 409, "reloads": 0})
        self.assertEqual(self.result["networkerRefused"], {"status": 502, "reloads": 0})

    def test_gateway_401_reloads_to_the_sign_in_page(self):
        self.assertEqual(self.result["loginGonePost"], {"status": 401, "reloads": 1})
        self.assertEqual(self.result["loginGoneGet"], {"status": 401, "reloads": 1})

    def test_non_api_401_does_not_reload(self):
        self.assertEqual(self.result["notApi"], {"status": 401, "reloads": 0})


@unittest.skipIf(NODE is None, "node is not on PATH; skipping admin.js logic tests")
class TestAdminConsoleSignsOutOnlyForGatewayLogin(unittest.TestCase):
    """The admin console's api() helper took every 401 for a lost sign-in.

    POST /api/admin/password answers 401 "The current password is wrong.", so a
    mistyped current password threw the admin back to the sign-in card and the
    error never showed. The unlock card had the opposite fault: it went back to
    sign-in only on a 401 without an error, and the gateway's 401 always has
    one. Only the gateway's 401 carries X-NWDash-Login (review F2). The real
    api() and unlock card run here against a fake fetch.
    """

    @classmethod
    def setUpClass(cls):
        api = _extract(r"      // ── Talking to the server ─+\n.*?\n      function fail\(e\) \{[^\n]*\n", ADMIN_JS)
        stepup = _extract(r"        stepup: \(\) => \(\{\n.*?\n        \}\),\n", ADMIN_JS)
        script = r"""
let boots = 0;
const shown = [];
async function boot() { boots++; }
function showGate(kind) { shown.push(kind); }
function showConsole() { shown.push("console"); }
function toast() {}
const $ = () => ({value: "typed"});
let csrf = "t";
let state = {access: "ok", passwordSet: true, unlocked: true, unlockedUntil: 0, unlockSeconds: 900};
let setupCode = "";
let continuedWithoutPassword = false;
let expiredNotice = "";
let reply = null;
globalThis.fetch = async (url) => String(url) === "/api/csrf"
  ? new Response('{"csrfToken":"t"}', {status: 200})
  : reply();
""" + api + "const GATES = {\n" + stepup + "};\n" + r"""
const answer = (status, body, headers) => { reply = () => new Response(JSON.stringify(body), {status, headers}); };
const SIGNED_OUT = [401, {ok: false, code: "login_required", error: "Authentication required."}, {"X-NWDash-Login": "required"}];
const out = {};
const run = async (name, call) => {
  const before = boots;
  expiredNotice = "";
  let result;
  try { await call(); result = {ok: true}; } catch (e) { result = {gate: e instanceof GateError, error: e.message}; }
  out[name] = {...result, boots: boots - before, notice: expiredNotice};
};
(async () => {
  answer(401, {ok: false, error: "The current password is wrong."}, {});
  await run("wrongCurrentPassword", () => post("/api/admin/password", {current: "x", new: "y"}));
  answer(...SIGNED_OUT);
  await run("signedOutPost", () => post("/api/admin/password", {current: "x", new: "y"}));
  await run("signedOutGet", () => get("/api/admin/overview"));
  answer(401, {ok: false, error: "Wrong password."}, {});
  await run("unlockWrongPassword", () => GATES.stepup().submit({}));
  answer(...SIGNED_OUT);
  await run("unlockSignedOut", () => GATES.stepup().submit({}));
  state.unlocked = false;
  answer(200, {ok: true, unlockedUntil: 123}, {});
  await run("unlockOk", () => GATES.stepup().submit({}));
  out.unlocked = state.unlocked;
  out.shown = shown;
  process.stdout.write(JSON.stringify(out));
})();
"""
        cls.result = _run_node(script)

    def test_wrong_current_password_shows_its_error(self):
        self.assertEqual(self.result["wrongCurrentPassword"],
                         {"gate": False, "error": "The current password is wrong.", "boots": 0, "notice": ""})

    def test_gateway_401_goes_back_to_sign_in(self):
        signed_out = {"gate": True, "error": "signed out", "boots": 1, "notice": "Your session has expired. Please sign in again."}
        self.assertEqual(self.result["signedOutPost"], signed_out)
        self.assertEqual(self.result["signedOutGet"], signed_out)

    def test_unlock_with_a_wrong_password_shows_its_error(self):
        self.assertEqual(self.result["unlockWrongPassword"], {"gate": False, "error": "Wrong password.", "boots": 0, "notice": ""})

    def test_unlock_when_signed_out_goes_back_to_sign_in(self):
        self.assertEqual(self.result["unlockSignedOut"],
                         {"ok": True, "boots": 1, "notice": "Your session has expired. Please sign in again."})

    def test_unlock_with_the_password_opens_the_console(self):
        self.assertEqual(self.result["unlockOk"], {"ok": True, "boots": 0, "notice": ""})
        self.assertTrue(self.result["unlocked"])
        self.assertEqual(self.result["shown"], ["console"])


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
