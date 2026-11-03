"""A share link that has expired must say so, and the page must never show its token.

The defect (review F5): view.html rendered whatever /api/view/<token> answered
without checking it, so an expired or revoked link (410 {"ok": false}) showed
as a healthy-looking dashboard of zeros, refreshed every minute for ever, and
the footer printed the token, the link's only secret, on screen. The page now
checks r.ok and data.ok, shows an "expired or invalid link" state and stops
polling on 410, and the token appears only in the URL it fetches.

The route tests run the real DashboardHandler on an in-process HTTPS server;
the page tests run view.html's own script in node against a fake fetch.
"""
import http.client
import json
import re
import shutil
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from nwdash import config as _cfg
from nwdash import models, server
from nwdash.certs import server_tls_context, write_embedded_dev_certificate
from nwdash.ui import read_only_view_html

NODE = shutil.which("node")
TOKEN = "0123456789abcdef0123456789abcdef"
SESSION = "share-session"


class TestShareRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cert, key = Path(cls._tmp.name) / "server.crt", Path(cls._tmp.name) / "server.key"
        write_embedded_dev_certificate(cert, key)
        cls.httpd = server.ExclusiveThreadingHTTPServer(("127.0.0.1", 0), server.DashboardHandler)
        cls.httpd.ssl_context = server_tls_context(cert, key)
        cls._serve = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls._serve.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls._serve.join(5)
        cls._tmp.cleanup()

    def setUp(self):
        self.dashboard = None
        for patcher in (
            # Signed-in or not makes no difference: the link is the capability.
            mock.patch.object(_cfg, "AUTH_ENABLED", True),
            mock.patch.dict(models.SHARE_TOKENS, clear=True),
            mock.patch.dict(models.SHARED_DASHBOARD_STATE, {"sessionId": "", "dashboard": None}),
            mock.patch.object(server, "cached_reliable_dashboard_for_session", lambda _sid: self.dashboard),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def get(self, path):
        conn = http.client.HTTPSConnection(
            "127.0.0.1", self.httpd.server_address[1], timeout=10, context=ssl._create_unverified_context()
        )
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            return response.status, response.getheader("Content-Type", ""), response.read()
        finally:
            conn.close()

    def issue(self, age_seconds=0):
        models.SHARE_TOKENS[TOKEN] = {"session_id": SESSION, "created_at": time.time() - age_seconds}

    def test_an_expired_link_answers_410_and_not_a_dashboard(self):
        self.issue(age_seconds=models.SHARE_TOKEN_TTL_SECONDS + 60)
        self.dashboard = {"ok": True, "summary": {"totalJobs": 5}}  # still cached: must not leak
        status, ctype, body = self.get(f"/api/view/{TOKEN}")
        self.assertEqual(status, 410)
        self.assertIn("application/json", ctype)
        answer = json.loads(body)
        self.assertIs(answer["ok"], False)
        self.assertNotIn("dashboard", answer)
        self.assertIn("expired", answer["error"])
        self.assertNotIn(TOKEN.encode(), body)
        self.assertNotIn(TOKEN, models.SHARE_TOKENS, "an expired token is forgotten")

    def test_an_expired_link_s_page_answers_410_without_the_token(self):
        self.issue(age_seconds=models.SHARE_TOKEN_TTL_SECONDS + 60)
        status, ctype, body = self.get(f"/view/{TOKEN}")
        self.assertEqual(status, 410)
        self.assertIn("text/html", ctype)
        self.assertIn(b"expired", body)
        self.assertNotIn(TOKEN.encode(), body)

    def test_a_revoked_or_never_issued_link_answers_410(self):
        for path in (f"/api/view/{TOKEN}", f"/view/{TOKEN}"):
            self.assertEqual(self.get(path)[0], 410, path)

    def test_a_malformed_token_is_not_found(self):
        for path in ("/api/view/not-a-token", "/view/not-a-token"):
            self.assertEqual(self.get(path)[0], 404, path)

    def test_a_live_link_with_no_data_yet_says_so(self):
        self.issue()
        status, _ctype, body = self.get(f"/api/view/{TOKEN}")
        self.assertEqual(status, 200)
        answer = json.loads(body)
        self.assertIs(answer["ok"], False)
        self.assertIn("No dashboard data", answer["message"])

    def test_a_live_link_serves_its_dashboard(self):
        self.issue()
        self.dashboard = {"ok": True, "summary": {"totalJobs": 5}, "generatedAt": "now"}
        status, _ctype, body = self.get(f"/api/view/{TOKEN}")
        self.assertEqual(status, 200)
        answer = json.loads(body)
        self.assertIs(answer["ok"], True)
        self.assertEqual(answer["dashboard"]["summary"]["totalJobs"], 5)

    def test_a_live_link_s_page_uses_the_token_only_to_fetch(self):
        self.issue()
        status, _ctype, body = self.get(f"/view/{TOKEN}")
        self.assertEqual(status, 200)
        self.assertEqual(body.decode().count(TOKEN), 1)
        self.assertIn(f"'/api/view/{TOKEN}'".encode(), body)


def _page_script(token):
    html = read_only_view_html(token)
    scripts = re.findall(r"<script>(.*?)</script>", html, re.DOTALL)
    if len(scripts) != 1:
        raise AssertionError(f"view.html should have one inline script, found {len(scripts)}")
    return scripts[0]


# Runs the page script against canned /api/view answers, one per load, then
# reports what the page showed after each and whether it is still polling.
_HARNESS = r"""
const answers = %(answers)s;
const content = {innerHTML: ""};
const document = {getElementById: (id) => (id === "content" ? content : {innerHTML: ""})};
const fetched = [];
let polling = null, tick = null;
function setInterval(fn, ms) { tick = fn; polling = ms; return 7; }
function clearInterval(id) { if (id === 7) polling = null; }
async function fetch(url) {
  fetched.push(url);
  const a = answers[fetched.length - 1];
  if (a.throws) throw new TypeError("Failed to fetch");
  return {ok: a.status >= 200 && a.status < 300, status: a.status,
          json: async () => { if (a.body === undefined) throw new SyntaxError("not JSON"); return a.body; }};
}
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
(async () => {
  %(script)s
  const shown = [];
  for (let i = 0; i < answers.length; i++) {
    if (i > 0) tick();
    for (let k = 0; k < 5; k++) await settle();
    shown.push({html: content.innerHTML, polling});
  }
  process.stdout.write(JSON.stringify({shown, fetched}));
})();
"""


def _run_page(answers, token=TOKEN):
    script = _HARNESS % {"answers": json.dumps(answers), "script": _page_script(token)}
    proc = subprocess.run(
        [NODE, "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=30, check=False
    )
    if proc.returncode != 0:
        raise AssertionError(f"node failed:\n{proc.stderr}")
    return json.loads(proc.stdout)


GOOD = {"status": 200, "body": {"ok": True, "updatedAt": "now", "dashboard": {
    "summary": {"health": "ok", "totalJobs": 12, "successfulJobs": 11, "failedJobs": 1},
    "target": {"restApiBase": "https://backup.example.test:9090"},
}}}
GONE = {"status": 410, "body": {"ok": False, "error": "This share link has expired or been revoked."}}


@unittest.skipIf(NODE is None, "node is not on PATH; skipping view.html logic tests")
class TestSharePage(unittest.TestCase):
    def test_an_expired_link_shows_expired_and_stops_polling(self):
        result = _run_page([GOOD, GONE])
        first, second = result["shown"]
        self.assertIn(">12<", first["html"])
        self.assertEqual(first["polling"], 60000)
        self.assertIn("expired or is invalid", second["html"])
        self.assertNotIn("metric-card", second["html"], "an expired link must not look like a dashboard")
        self.assertIsNone(second["polling"], "polling must stop on 410")

    def test_an_expired_link_on_first_load_never_draws_zeros(self):
        shown = _run_page([GONE])["shown"][0]
        self.assertIn("expired or is invalid", shown["html"])
        self.assertNotIn("metric-card", shown["html"])
        self.assertIsNone(shown["polling"])

    def test_no_data_yet_is_a_notice_and_polling_goes_on(self):
        answer = {"status": 200, "body": {"ok": False, "message": "No dashboard data available for this share link yet."}}
        shown = _run_page([answer])["shown"][0]
        self.assertIn("No dashboard data available", shown["html"])
        self.assertNotIn("metric-card", shown["html"])
        self.assertEqual(shown["polling"], 60000)

    def test_a_server_error_or_an_unreachable_server_keeps_polling(self):
        result = _run_page([{"status": 502}, {"throws": True}, GOOD])
        error, offline, back = result["shown"]
        self.assertIn("HTTP 502", error["html"])
        self.assertIn("Could not reach", offline["html"])
        self.assertNotIn("metric-card", error["html"] + offline["html"])
        self.assertIn(">12<", back["html"])
        self.assertEqual(back["polling"], 60000)

    def test_the_token_is_fetched_but_never_shown(self):
        result = _run_page([GOOD, {"status": 200, "body": {"ok": False}}, {"status": 502}, {"throws": True}, GONE])
        self.assertEqual(result["fetched"][0], f"/api/view/{TOKEN}")
        for shown in result["shown"]:
            self.assertNotIn(TOKEN, shown["html"])

    def test_server_text_is_escaped(self):
        answer = {"status": 200, "body": {"ok": False, "message": "<img src=x onerror=alert(1)>"}}
        shown = _run_page([answer])["shown"][0]
        self.assertNotIn("<img", shown["html"])
        self.assertIn("&lt;img", shown["html"])


if __name__ == "__main__":
    unittest.main()
