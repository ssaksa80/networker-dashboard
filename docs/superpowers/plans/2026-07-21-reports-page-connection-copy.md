# Scheduled Reports Page + Connection-by-Copy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Fix empty scheduled reports by making the reporting connection a full snapshot of a proven live session (nothing guessed), set by one click, validated with real row counts — and move all reporting settings onto a dedicated `/reports` page.

**Architecture:** `sessions.snapshot_latest_live_session()` returns the most-recently-used live session as the existing `_session_to_dict` record (every `ApiConfig` field + sealed password). That record is stored as the reporting connection. `report_cred.apiconfig_from_stored()` reads it verbatim (and still accepts the legacy flat shape), and `render_window` uses it. New `/reports` page (auth-gated like `/`) owns connection + SMTP + groups; drawer entries become launchers.

**Tech Stack:** Python 3.12 stdlib, existing `nwdash` package, pytest, vanilla-JS SPA.

**Reused primitives (verified):**
- `nwdash/models.py`: `DASHBOARD_SESSIONS`, `_get_session`, `_session_items_snapshot() -> list[(sid, session)]`; sessions have `.last_used`.
- `nwdash/sessions.py`: `_session_to_dict(session_id, session)` → `{session_id, created_at, last_used, encrypted_networker_password, encrypted_wmi_password, config:{...all ApiConfig fields...}}`.
- `nwdash/secrets.py`: `encrypt_process_secret` / `decrypt_process_secret` (the scheme session snapshots use).
- `nwdash/report_cred.py`: `credential_to_apiconfig` (legacy flat), `decrypt_credential_password` (DPAPI token).
- `nwdash/report_render.py`: `RenderResult`, `render_window(cred, window)`.
- `nwdash/display.py`: `save_connection` / `load_connection` (machine-DPAPI store).
- `nwdash/ui.py`: page pattern — asset HTML with `__PLACEHOLDER__` tokens; `_TV_PAGE` (line ~117), `tv_page_html()` (line ~143).
- `nwdash/server.py`: `do_GET` auth gate (`if _cfg.AUTH_ENABLED and not self._authenticated()`), `_send_bytes`, POST `allowed` set, `/api/report-groups` route.
- `nwdash/report_groups_api.py`: `handle_report_groups`, `_reporting_connection`, `_cfg`, `_smtp_config`.

Repo gotcha: any new `nwdash/*` asset or module must be in `deploy/build-bundle.ps1` `$shipFiles` or `tests/test_deploy.py::TestBundleAllowList` fails. Run the FULL suite before each commit.

---

## File Structure
- Modify `nwdash/sessions.py` — `snapshot_latest_live_session()`.
- Modify `nwdash/report_cred.py` — `apiconfig_from_stored()`.
- Modify `nwdash/report_render.py` — use `apiconfig_from_stored`.
- Modify `nwdash/report_groups_api.py` — connection actions.
- Create `nwdash/assets/reports.html`, `nwdash/assets/reports.js`; modify `nwdash/ui.py`, `nwdash/server.py`.
- Modify `nwdash/assets/dashboard.html`, `app.js` — launchers, remove moved panels.
- Modify `deploy/build-bundle.ps1`.

---

## Task 1: `snapshot_latest_live_session`

**Files:** Modify `nwdash/sessions.py`; Test `tests/test_session_snapshot.py`

- [ ] **Step 1: Write the failing test**
```python
# tests/test_session_snapshot.py
import importlib
from nwdash import sessions, models

class _Cfg:
    rest_api_host="10.0.0.9"; rest_api_port=9090; backup_server_host="bkp"; backup_server_port=9090
    username="administrator"; api_mode="nwui"; api_version="v3"; report_range="24h"
    custom_start_date=""; custom_end_date=""; use_wmi_health=True; wmi_username="wmiu"
    timeout_seconds=30; verify_tls=True; use_authc_header=True

class _S:
    def __init__(self, last_used):
        self.config=_Cfg(); self.encrypted_networker_password="enc-pw"
        self.encrypted_wmi_password="enc-wmi"; self.created_at=1.0; self.last_used=last_used

def test_returns_none_when_no_sessions(monkeypatch):
    importlib.reload(sessions)
    monkeypatch.setattr(sessions, "_session_items_snapshot", lambda: [])
    assert sessions.snapshot_latest_live_session() is None

def test_picks_most_recently_used(monkeypatch):
    importlib.reload(sessions)
    monkeypatch.setattr(sessions, "_session_items_snapshot",
                        lambda: [("old", _S(100.0)), ("new", _S(900.0)), ("mid", _S(500.0))])
    snap = sessions.snapshot_latest_live_session()
    assert snap["session_id"] == "new"

def test_snapshot_carries_every_config_field(monkeypatch):
    importlib.reload(sessions)
    monkeypatch.setattr(sessions, "_session_items_snapshot", lambda: [("a", _S(1.0))])
    snap = sessions.snapshot_latest_live_session()
    cfg = snap["config"]
    # exactly the fields that used to be guessed
    assert cfg["use_authc_header"] is True
    assert cfg["verify_tls"] is True
    assert cfg["api_version"] == "v3"
    assert cfg["backup_server_host"] == "bkp"
    assert snap["encrypted_networker_password"] == "enc-pw"
```

- [ ] **Step 2: Run → FAIL.** `python -m pytest tests/test_session_snapshot.py -v`

- [ ] **Step 3: Implement** — append to `nwdash/sessions.py`:
```python
def snapshot_latest_live_session() -> dict[str, Any] | None:
    """Full connection record for the most recently used live session, in the
    same shape persist_sessions writes. Used to establish the reporting
    connection by COPY, so nothing about the connection is guessed."""
    items = _session_items_snapshot()
    if not items:
        return None
    session_id, session = max(items, key=lambda kv: float(getattr(kv[1], "last_used", 0) or 0))
    return _session_to_dict(session_id, session)
```
(`_session_items_snapshot` and `_session_to_dict` are already available in this module.)

- [ ] **Step 4: Run → PASS (3).** Then FULL suite `python -m pytest -q`.

- [ ] **Step 5: Commit**
```bash
git add nwdash/sessions.py tests/test_session_snapshot.py
git commit -m "feat(reports): snapshot_latest_live_session — full proven connection record"
```

---

## Task 2: `apiconfig_from_stored` (nothing guessed)

**Files:** Modify `nwdash/report_cred.py`, `nwdash/report_render.py`; Test `tests/test_apiconfig_from_stored.py`

- [ ] **Step 1: Write the failing test**
```python
# tests/test_apiconfig_from_stored.py
from nwdash.report_cred import apiconfig_from_stored, encrypt_credential_password
from nwdash.secrets import encrypt_process_secret

def _snapshot():
    return {
        "session_id": "s1", "created_at": 1.0, "last_used": 2.0,
        "encrypted_networker_password": encrypt_process_secret("realpw"),
        "encrypted_wmi_password": "",
        "config": {
            "rest_api_host": "10.0.0.9", "rest_api_port": 9090,
            "backup_server_host": "bkp", "backup_server_port": 9091,
            "username": "administrator", "api_mode": "nwui", "api_version": "v3",
            "report_range": "24h", "custom_start_date": "", "custom_end_date": "",
            "use_wmi_health": True, "wmi_username": "wmiu",
            "timeout_seconds": 45, "verify_tls": True, "use_authc_header": True,
        },
    }

def test_snapshot_shape_carries_every_field_verbatim():
    cfg = apiconfig_from_stored(_snapshot())
    # the exact fields the old code guessed:
    assert cfg.use_authc_header is True
    assert cfg.verify_tls is True
    assert cfg.api_version == "v3"
    assert cfg.backup_server_host == "bkp" and cfg.backup_server_port == 9091
    assert cfg.use_wmi_health is True and cfg.wmi_username == "wmiu"
    assert cfg.timeout_seconds == 45
    assert cfg.username == "administrator"
    assert cfg.password == "realpw"          # decrypted with the PROCESS cipher

def test_legacy_flat_shape_still_works():
    flat = {"rest_api_host": "h", "rest_api_port": 9090, "username": "u",
            "encrypted_password": encrypt_credential_password("pw"), "api_mode": "nwui"}
    cfg = apiconfig_from_stored(flat)
    assert cfg.rest_api_host == "h" and cfg.username == "u" and cfg.password == "pw"

def test_empty_returns_blank_host():
    cfg = apiconfig_from_stored({})
    assert cfg.rest_api_host == ""
```

- [ ] **Step 2: Run → FAIL** (`ImportError: cannot import name 'apiconfig_from_stored'`).

- [ ] **Step 3: Implement** — add to `nwdash/report_cred.py`:
```python
from .secrets import decrypt_process_secret


def apiconfig_from_stored(stored: dict[str, Any]) -> ApiConfig:
    """Build an ApiConfig from a stored reporting connection.

    Two shapes are supported:
    * session snapshot (has a "config" block) — every field is carried VERBATIM
      and the password is sealed with the process cipher. Nothing is guessed.
    * legacy flat dict — the older hand-entered shape, via credential_to_apiconfig.
    """
    if not isinstance(stored, dict):
        return credential_to_apiconfig({})
    cfg = stored.get("config")
    if not isinstance(cfg, dict):
        return credential_to_apiconfig(stored)
    return ApiConfig(
        rest_api_host=str(cfg.get("rest_api_host") or ""),
        rest_api_port=int(cfg.get("rest_api_port") or 0),
        backup_server_host=str(cfg.get("backup_server_host") or ""),
        backup_server_port=int(cfg.get("backup_server_port") or 0),
        username=str(cfg.get("username") or ""),
        password=decrypt_process_secret(str(stored.get("encrypted_networker_password") or "")),
        api_mode=str(cfg.get("api_mode") or "nwui"),
        api_version=str(cfg.get("api_version") or "auto"),
        report_range=str(cfg.get("report_range") or "24h"),
        custom_start_date=str(cfg.get("custom_start_date") or ""),
        custom_end_date=str(cfg.get("custom_end_date") or ""),
        use_wmi_health=bool(cfg.get("use_wmi_health") or False),
        wmi_username=str(cfg.get("wmi_username") or ""),
        wmi_password="",
        timeout_seconds=int(cfg.get("timeout_seconds") or 30),
        verify_tls=bool(cfg.get("verify_tls") or False),
        use_authc_header=bool(cfg.get("use_authc_header") or False),
    )
```

- [ ] **Step 4: Point the renderer at it.** In `nwdash/report_render.py::render_window`, replace `cfg = credential_to_apiconfig(cred)` with `cfg = apiconfig_from_stored(cred)` and update the import to bring in `apiconfig_from_stored` (keep `credential_to_apiconfig` imported only if still used elsewhere in the file; if `render` also uses it, leave that one alone).

- [ ] **Step 5: Run → PASS (3).** Then FULL suite. The existing `tests/test_render_window.py` must still pass (it passes a flat cred → legacy path).

- [ ] **Step 6: Commit**
```bash
git add nwdash/report_cred.py nwdash/report_render.py tests/test_apiconfig_from_stored.py
git commit -m "fix(reports): apiconfig_from_stored — carry a session snapshot verbatim, stop guessing connection fields"
```

---

## Task 3: Connection actions (copy + validate with row counts)

**Files:** Modify `nwdash/report_groups_api.py`; Test `tests/test_connection_actions.py`

- [ ] **Step 1: Write the failing test**
```python
# tests/test_connection_actions.py
import importlib
from http import HTTPStatus
from nwdash import report_groups_api as api

def test_use_current_connection_without_session(monkeypatch):
    importlib.reload(api)
    monkeypatch.setattr(api, "snapshot_latest_live_session", lambda: None)
    saved = {}
    monkeypatch.setattr(api.display, "save_connection", lambda s: saved.setdefault("saved", True))
    st, body = api.handle_report_groups({"action": "use-current-connection"})
    assert st == HTTPStatus.BAD_REQUEST and body["ok"] is False
    assert "connect" in body["message"].lower()
    assert "saved" not in saved            # nothing stored

def test_use_current_connection_saves_and_validates(monkeypatch):
    importlib.reload(api)
    snap = {"session_id": "s1", "config": {"rest_api_host": "h", "username": "u"},
            "encrypted_networker_password": ""}
    monkeypatch.setattr(api, "snapshot_latest_live_session", lambda: snap)
    saved = {}
    monkeypatch.setattr(api.display, "save_connection", lambda s: saved.setdefault("snap", s))
    from nwdash import report_render
    monkeypatch.setattr(api.report_render, "render_window",
                        lambda c, w: report_render.RenderResult(True, {"summary": {"totalJobs": 2245, "totalAlerts": 3}}, ""))
    st, body = api.handle_report_groups({"action": "use-current-connection"})
    assert st == HTTPStatus.OK and body["ok"] is True
    assert saved["snap"]["session_id"] == "s1"
    assert body["jobs"] == 2245
    assert "2245" in body["message"] or "2,245" in body["message"]

def test_validate_flags_zero_data(monkeypatch):
    importlib.reload(api)
    monkeypatch.setattr(api, "_reporting_connection", lambda: {"config": {"rest_api_host": "h", "username": "u"}})
    from nwdash import report_render
    monkeypatch.setattr(api.report_render, "render_window",
                        lambda c, w: report_render.RenderResult(True, {"summary": {"totalJobs": 0, "totalAlerts": 0}}, ""))
    st, body = api.handle_report_groups({"action": "validate-connection"})
    assert st == HTTPStatus.OK and body["ok"] is True
    assert body["zeroData"] is True
    assert "0 jobs" in body["message"]

def test_validate_render_failure(monkeypatch):
    importlib.reload(api)
    monkeypatch.setattr(api, "_reporting_connection", lambda: {"config": {"rest_api_host": "h", "username": "u"}})
    from nwdash import report_render
    monkeypatch.setattr(api.report_render, "render_window",
                        lambda c, w: report_render.RenderResult(False, {}, "login rejected"))
    st, body = api.handle_report_groups({"action": "validate-connection"})
    assert body["ok"] is False and "login rejected" in body["message"]

def test_connection_status_never_leaks_password(monkeypatch):
    importlib.reload(api)
    monkeypatch.setattr(api, "_reporting_connection",
                        lambda: {"config": {"rest_api_host": "h", "username": "u", "api_mode": "nwui"},
                                 "encrypted_networker_password": "SECRET-TOKEN"})
    st, body = api.handle_report_groups({"action": "connection-status"})
    assert st == HTTPStatus.OK and body["hasConnection"] is True
    assert body["host"] == "h" and body["username"] == "u"
    assert "SECRET-TOKEN" not in str(body)
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement** — in `nwdash/report_groups_api.py` add imports and three actions.

Imports at top:
```python
from . import report_render
from .sessions import snapshot_latest_live_session
from .report_window import compute_window
from datetime import datetime
```
Helper + actions (insert into `handle_report_groups` before the `create/update` branch):
```python
def _validate_stored_connection() -> tuple[int, dict]:
    """Render the daily window through the stored connection and report REAL row
    counts, so a connection that authenticates but returns nothing is caught at
    setup instead of arriving as an empty report."""
    stored = _reporting_connection()
    if not stored:
        return HTTPStatus.BAD_REQUEST, {"ok": False, "message": "No reporting connection is set."}
    window = compute_window("daily", datetime.now().astimezone())
    res = report_render.render_window(stored, window)
    if not res.ok:
        return HTTPStatus.OK, {"ok": False, "message": res.error or "Connection test failed."}
    summary = res.dashboard.get("summary") if isinstance(res.dashboard, dict) else {}
    jobs = int((summary or {}).get("totalJobs") or 0)
    alerts = int((summary or {}).get("totalAlerts") or 0)
    if jobs == 0:
        return HTTPStatus.OK, {"ok": True, "zeroData": True, "jobs": 0, "alerts": alerts,
                               "message": ("Connected, but returned 0 jobs in the last 24h — reports would be "
                                           "empty. Check the account's permissions and the backup server.")}
    return HTTPStatus.OK, {"ok": True, "zeroData": False, "jobs": jobs, "alerts": alerts,
                           "message": f"Validated — {jobs:,} jobs in the last 24h."}
```
In `handle_report_groups`:
```python
    if action == "connection-status":
        stored = _reporting_connection()
        cfg = (stored or {}).get("config") if isinstance(stored, dict) else None
        cfg = cfg if isinstance(cfg, dict) else (stored or {})
        return HTTPStatus.OK, {"ok": True, "hasConnection": bool(stored),
                               "host": str(cfg.get("rest_api_host") or ""),
                               "username": str(cfg.get("username") or ""),
                               "apiMode": str(cfg.get("api_mode") or "")}
    if action == "use-current-connection":
        snap = snapshot_latest_live_session()
        if not snap:
            return HTTPStatus.BAD_REQUEST, {"ok": False,
                "message": "No live dashboard connection. Connect on the dashboard first, then click this."}
        display.save_connection(snap)
        return _validate_stored_connection()
    if action == "validate-connection":
        return _validate_stored_connection()
```
NOTE: `display.save_connection` currently strips a `password` key and adds `encrypted_password`; a snapshot has neither, so it stores the snapshot unchanged — verify by reading `display.save_connection` and, if it would mangle the snapshot, add a `save_connection_raw(record)` that writes the dict as-is and use that here. Report which you did.

- [ ] **Step 4: Run → PASS (5).** FULL suite.

- [ ] **Step 5: Commit**
```bash
git add nwdash/report_groups_api.py tests/test_connection_actions.py
git commit -m "feat(reports): use-current-connection + validate with real row counts (zero-data guard)"
```

---

## Task 4: `/reports` page

**Files:** Create `nwdash/assets/reports.html`, `nwdash/assets/reports.js`; Modify `nwdash/ui.py`, `nwdash/server.py`, `deploy/build-bundle.ps1`; Test `tests/test_reports_page.py`

- [ ] **Step 1: Write the failing test**
```python
# tests/test_reports_page.py
from pathlib import Path
from nwdash import ui
A = Path(__file__).resolve().parents[1] / "nwdash" / "assets"

def test_reports_assets_exist():
    assert (A / "reports.html").is_file() and (A / "reports.js").is_file()

def test_reports_page_html_renders_sections():
    html = ui.reports_page_html()
    assert 'id="connPanel"' in html and 'id="smtpPanel"' in html and 'id="groupsPanel"' in html
    assert "__REPORTS_JS__" not in html   # placeholder substituted

def test_reports_js_wires_endpoints():
    js = (A / "reports.js").read_text(encoding="utf-8")
    assert "use-current-connection" in js and "validate-connection" in js
    assert "/api/report-groups" in js and "/api/email-config" in js
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Create `nwdash/assets/reports.html`** (mirrors the `tv.html` placeholder pattern; reuses app.css):
```html
<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Scheduled Reports — NetWorker Dashboard</title>
<style>__APP_CSS__</style>
</head><body>
<main class="dashboard">
  <h1>Scheduled Reports</h1>

  <section id="connPanel" class="panel">
    <header class="panel-head"><h2>Reporting connection</h2></header>
    <p>Reports pull live data through this connection. The quickest setup is to copy the connection you are already using on the dashboard.</p>
    <div id="connStatus" class="rj-sub">Loading…</div>
    <button id="connCopyBtn" type="button">Use my current dashboard connection</button>
    <button id="connValidateBtn" type="button">Re-validate</button>
    <div id="connMsg" class="form-error" role="alert"></div>
  </section>

  <section id="smtpPanel" class="panel">
    <header class="panel-head"><h2>Email (SMTP)</h2></header>
    <form id="smtpForm" class="report-form">
      <label>SMTP host <input name="host" required></label>
      <label>Port <input name="port" type="number" value="25"></label>
      <label>Security <select name="security"><option value="none">None</option><option value="starttls">STARTTLS</option><option value="ssl">SSL</option></select></label>
      <label>Username <input name="username" autocomplete="off"></label>
      <label>Password <input name="password" type="password" placeholder="(unchanged)" autocomplete="new-password"></label>
      <label>From address <input name="from" required></label>
      <label>Ops-alert address <input name="opsAlertAddress"></label>
      <div id="smtpMsg" class="form-error" role="alert"></div>
      <button type="submit">Save SMTP settings</button>
    </form>
  </section>

  <section id="groupsPanel" class="panel">
    <header class="panel-head"><h2>Report groups</h2></header>
    <div id="reportGroupsConn" class="form-error"></div>
    <div id="reportGroupsList" class="report-jobs"></div>
    <button id="reportGroupAddBtn" type="button">New group</button>
    <form id="reportGroupForm" class="report-form" hidden>
      <input type="hidden" name="id">
      <label>Name <input name="name" required></label>
      <fieldset id="reportSectionChecks"><legend>Sections</legend>
        <label><input type="checkbox" value="backup_sla">Backup SLA</label>
        <label><input type="checkbox" value="management">Management Overview</label>
        <label><input type="checkbox" value="recovery">Recovery Health</label>
        <label><input type="checkbox" value="clone">Clone Jobs</label>
        <label><input type="checkbox" value="alerts">Alerts</label>
        <label><input type="checkbox" value="server_protection">Server Protection</label>
        <label><input type="checkbox" value="health">Health</label>
      </fieldset>
      <label>Recipients <input name="recipients" required placeholder="a@x.com, b@x.com"></label>
      <label>Cadence <select name="cadence"><option value="daily">Daily</option><option value="weekly">Weekly (Sun)</option><option value="monthly">Monthly (1st)</option></select></label>
      <label>Send time <input name="sendTime" value="08:00" placeholder="HH:MM"></label>
      <label class="check-row"><input type="checkbox" name="enabled" checked> Enabled</label>
      <div id="reportGroupErr" class="form-error" role="alert"></div>
      <button type="submit">Save group</button>
      <button id="reportGroupCancel" type="button">Cancel</button>
    </form>
  </section>
</main>
<script>__REPORTS_JS__</script>
</body></html>
```

- [ ] **Step 4: Create `nwdash/assets/reports.js`.** Move the group-manager functions here (they are being removed from app.js in Task 5) and add the connection + SMTP wiring. Include a CSRF-safe POST helper: this page has no app.js wrapper, so fetch the token from `/api/csrf` once and attach it.
```javascript
(function () {
  var CSRF = "";
  async function api(path, body) {
    if (!CSRF) { try { CSRF = (await (await fetch("/api/csrf", {cache:"no-store"})).json()).csrfToken || ""; } catch (e) {} }
    var r = await fetch(path, {method:"POST", headers:{"Content-Type":"application/json","X-CSRF-Token":CSRF},
                              body: JSON.stringify(body)});
    return r.json();
  }
  // ---- connection ----
  async function loadConn() {
    var d = await api("/api/report-groups", {action:"connection-status"});
    document.getElementById("connStatus").textContent = d.hasConnection
      ? ("Connected as " + d.username + " @ " + d.host + " (" + d.apiMode + ")")
      : "No reporting connection set.";
  }
  async function copyConn() {
    var m = document.getElementById("connMsg"); m.textContent = "Copying and validating…";
    var d = await api("/api/report-groups", {action:"use-current-connection"});
    m.textContent = d.message || (d.ok ? "Done." : "Failed.");
    loadConn(); renderGroups();
  }
  async function validateConn() {
    var m = document.getElementById("connMsg"); m.textContent = "Validating…";
    var d = await api("/api/report-groups", {action:"validate-connection"});
    m.textContent = d.message || (d.ok ? "OK" : "Failed");
  }
  // ---- smtp ----
  async function loadSmtp() {
    var f = document.getElementById("smtpForm");
    try {
      var d = await (await fetch("/api/email-config", {cache:"no-store"})).json();
      var s = d.smtp || {};
      f.host.value=s.host||""; f.port.value=s.port||25; f.security.value=s.security||"none";
      f.username.value=s.username||""; f.from.value=s.from||""; f.opsAlertAddress.value=d.opsAlertAddress||"";
      f.password.placeholder = s.passwordSaved ? "(unchanged — leave blank to keep)" : "";
    } catch (e) {}
  }
  async function saveSmtp(ev) {
    ev.preventDefault(); var f = ev.target, m = document.getElementById("smtpMsg"); m.textContent = "Saving…";
    var d = await api("/api/email-config", {host:f.host.value, port:Number(f.port.value), security:f.security.value,
      username:f.username.value, password:f.password.value, from:f.from.value, opsAlertAddress:f.opsAlertAddress.value});
    m.textContent = d.ok ? "Saved." : "Save failed."; if (d.ok) { f.password.value=""; loadSmtp(); }
  }
  // ---- groups ----
  async function renderGroups() {
    var list = document.getElementById("reportGroupsList");
    var d = await api("/api/report-groups", {action:"list"});
    document.getElementById("reportGroupsConn").textContent = d.hasConnection ? "" : "Set the reporting connection above before enabling groups.";
    var order = (d.groups||[]).map(function(g){return g.id;});
    list.innerHTML = (d.groups||[]).map(function(g){
      var badge = '<span class="health-badge health-'+({healthy:"ok",unhealthy:"bad",never_run:"idle"}[g.health.state]||"idle")+'">'+g.health.state.replace("_"," ")+'</span>';
      return '<div class="report-job" data-id="'+g.id+'">'
        + '<div class="rj-main"><strong>'+g.name+'</strong> · '+g.cadence+' '+g.sendTime+' · '+g.recipients.length+' recipient(s) '+badge+'</div>'
        + '<div class="rj-sub">'+g.sections.join(", ")+' · last: '+(g.health.lastResult||"—")+'</div>'
        + '<label class="check-row"><input type="checkbox" class="rg-toggle" '+(g.enabled?"checked":"")+'> on</label> '
        + '<label class="check-row"><input type="checkbox" class="rg-test"> test</label> '
        + '<button class="rg-send" type="button">Send now</button> <button class="rg-edit" type="button">Edit</button> '
        + '<button class="rg-del" type="button">Delete</button> <button class="rg-up" type="button">↑</button><button class="rg-down" type="button">↓</button></div>';
    }).join("") || "<p>No report groups yet.</p>";
    list.querySelectorAll(".report-job").forEach(function(card){
      var id = card.getAttribute("data-id");
      var g = (d.groups||[]).find(function(x){return x.id===id;});
      card.querySelector(".rg-toggle").addEventListener("change", function(e){ act({action:"toggle", id:id, enabled:e.target.checked}); });
      card.querySelector(".rg-del").addEventListener("click", function(){ act({action:"delete", id:id}); });
      card.querySelector(".rg-send").addEventListener("click", async function(){
        var t = card.querySelector(".rg-test").checked;
        var dd = await api("/api/report-groups", {action:"send", id:id, test:t});
        alert(dd.message || (dd.ok?"Sent":"Failed"));
      });
      card.querySelector(".rg-edit").addEventListener("click", function(){ editGroup(id, g); });
      card.querySelector(".rg-up").addEventListener("click", function(){ move(order, id, -1); });
      card.querySelector(".rg-down").addEventListener("click", function(){ move(order, id, 1); });
    });
  }
  async function act(body){ await api("/api/report-groups", body); renderGroups(); }
  function move(order, id, delta){ var i=order.indexOf(id), j=i+delta; if(i<0||j<0||j>=order.length) return; order.splice(j,0,order.splice(i,1)[0]); act({action:"reorder", order:order}); }
  function editGroup(id, g){
    var f = document.getElementById("reportGroupForm"); f.hidden=false;
    f.id.value=id||""; f.name.value=g?g.name:""; f.recipients.value=g?g.recipients.join(", "):"";
    f.cadence.value=g?g.cadence:"daily"; f.sendTime.value=g?g.sendTime:"08:00"; f.enabled.checked=g?g.enabled:true;
    var sel=g?g.sections:[]; document.querySelectorAll("#reportSectionChecks input").forEach(function(c){ c.checked=sel.indexOf(c.value)>=0; });
    document.getElementById("reportGroupErr").textContent="";
  }
  async function saveGroup(ev){
    ev.preventDefault(); var f=ev.target, err=document.getElementById("reportGroupErr");
    var sections = Array.prototype.slice.call(document.querySelectorAll("#reportSectionChecks input:checked")).map(function(c){return c.value;});
    var d = await api("/api/report-groups", {action: f.id.value?"update":"create", id:f.id.value||undefined,
      name:f.name.value, sections:sections, recipients:f.recipients.value, cadence:f.cadence.value,
      sendTime:f.sendTime.value, enabled:f.enabled.checked});
    if(!d.ok){ err.textContent = "Fix: " + Object.values(d.errors||{message:d.message}).join(" "); return; }
    if(d.warning) err.textContent = d.warning;
    f.hidden = true; renderGroups();
  }
  document.getElementById("connCopyBtn").addEventListener("click", copyConn);
  document.getElementById("connValidateBtn").addEventListener("click", validateConn);
  document.getElementById("smtpForm").addEventListener("submit", saveSmtp);
  document.getElementById("reportGroupAddBtn").addEventListener("click", function(){ editGroup("", null); });
  document.getElementById("reportGroupForm").addEventListener("submit", saveGroup);
  document.getElementById("reportGroupCancel").addEventListener("click", function(){ document.getElementById("reportGroupForm").hidden = true; });
  loadConn(); loadSmtp(); renderGroups();
})();
```

- [ ] **Step 5: `nwdash/ui.py`** — mirror the `_TV_PAGE` pattern. Near the other `_load_asset` lines add:
```python
_REPORTS_JS = _load_asset("reports.js")
_REPORTS_PAGE = _load_asset("reports.html").replace("__APP_CSS__", _APP_CSS).replace("__REPORTS_JS__", _REPORTS_JS)


def reports_page_html() -> str:
    """Scheduled Reports settings page served at /reports (auth-gated like /)."""
    return _REPORTS_PAGE
```

- [ ] **Step 6: `nwdash/server.py`** — import `reports_page_html` alongside the other ui imports, and add a `do_GET` branch AFTER the auth gate (so unauthenticated users get the login page, same as `/`):
```python
            if path == "/reports":
                self._send_bytes(HTTPStatus.OK, reports_page_html().encode("utf-8"), "text/html; charset=utf-8")
                return
```
Place it with the other authed GET routes (after the `if _cfg.AUTH_ENABLED and not self._authenticated():` gate).

- [ ] **Step 7:** Add `'nwdash\assets\reports.html',` and `'nwdash\assets\reports.js',` to `deploy/build-bundle.ps1` `$shipFiles`.

- [ ] **Step 8:** `node -c nwdash/assets/reports.js`. Run `python -m pytest tests/test_reports_page.py -v` (3 passed), then FULL suite.

- [ ] **Step 9: Commit**
```bash
git add nwdash/assets/reports.html nwdash/assets/reports.js nwdash/ui.py nwdash/server.py deploy/build-bundle.ps1 tests/test_reports_page.py
git commit -m "feat(reports): /reports settings page (connection + SMTP + groups), auth-gated"
```

---

## Task 5: Drawer becomes launchers

**Files:** Modify `nwdash/assets/dashboard.html`, `nwdash/assets/app.js`; Test `tests/test_drawer_launchers.py`

- [ ] **Step 1: Write the failing test**
```python
# tests/test_drawer_launchers.py
from pathlib import Path
A = Path(__file__).resolve().parents[1] / "nwdash" / "assets"

def test_drawer_panels_removed():
    html = (A / "dashboard.html").read_text(encoding="utf-8")
    assert 'id="scheduledReportsPanel"' not in html
    assert 'id="smtpSettingsPanel"' not in html

def test_buttons_open_reports_page():
    js = (A / "app.js").read_text(encoding="utf-8")
    assert '"/reports"' in js or "'/reports'" in js
    assert "renderReportGroups" not in js      # group manager moved to reports.js
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: dashboard.html** — delete the `<section id="smtpSettingsPanel">` and `<section id="scheduledReportsPanel">` blocks entirely (they now live on `/reports`). In the TV / Display panel (`#tvDisplayPanel`), delete the `#tvConnForm` connection form and its heading, replacing it with:
```html
  <p class="rj-sub">The reporting connection is shared with Scheduled Reports — set it on the <a href="/reports" target="_blank" rel="noopener">Scheduled Reports page</a>.</p>
```
Keep the token URL row (`#tvDisplayUrl`, Rotate, Revoke) and `#tvConnState` may be removed with the form.

- [ ] **Step 4: app.js** — remove the moved code and wire launchers:
  - Delete `renderReportGroups`, `act`/`_groupAction`, `_move`, `_editGroup`, `submitReportGroup`, `initReportGroups` and the `initReportGroups();` call.
  - Delete `loadSmtpSettings`, `submitSmtpSettings`, `initSmtpSettings` and its call.
  - Delete `submitDisplayConn` and the `#tvConnForm` listener inside `initDisplayConfig` (keep the token rotate/revoke wiring and `renderDisplayConfig`'s token URL handling; drop the `tvConnState` lines).
  - Change the two config-panel buttons to open the page. In `initConfigPanelButtons`, replace the mapping so:
```javascript
  var openReports = function () { window.open("/reports", "_blank", "noopener"); };
  var b1 = document.getElementById("emailSettingsBtn"); if (b1) b1.addEventListener("click", openReports);
  var b2 = document.getElementById("reportsPanelBtn"); if (b2) b2.addEventListener("click", openReports);
  var b3 = document.getElementById("tvDisplayBtn"); if (b3) b3.addEventListener("click", function () { revealTvPanel(); });
```
where `revealTvPanel()` is the existing drawer-open logic kept for `#tvDisplayPanel` only (rename/trim `openConfigDrawer` so it only ever targets `tvDisplayPanel`, since the other two panels are gone).
  - CRITICAL: after editing, grep for every removed id (`smtpSettingsForm`, `reportGroupsList`, `reportGroupForm`, `reportGroupAddBtn`, `reportGroupCancel`, `tvConnForm`, `tvConnState`, `reportSectionChecks`) in app.js — ZERO `getElementById(...)` + `addEventListener` pairs may remain, or SPA init throws.

- [ ] **Step 5:** `node -c nwdash/assets/app.js`. Re-grep the removed ids → zero hits in app.js. Run `python -m pytest tests/test_drawer_launchers.py -v` (2 passed). Existing asset tests that assert the removed markers (`tests/test_smtp_settings_ui.py`, `tests/test_groups_ui.py`, `tests/test_config_drawer.py`, `tests/test_config_menu_buttons.py`, `tests/test_tv_admin_assets.py`) will now fail — update each to the new reality or delete the ones that only asserted moved markup. Report exactly what you changed for each. FULL suite must end GREEN.

- [ ] **Step 6: Commit**
```bash
git add -A
git commit -m "refactor(reports): drawer buttons launch /reports; move SMTP + groups off the dashboard"
```

---

## Task 6: Version bump 2.12.0 + build

- [ ] **Step 1:** `APP_VERSION = "2.12.0"` (`nwdash/config.py`), `version = "2.12.0"` (`pyproject.toml`).
- [ ] **Step 2:** README: describe the `/reports` page and the one-click "Use my current dashboard connection" setup.
- [ ] **Step 3:** FULL suite `python -m pytest -q` (0 failures).
- [ ] **Step 4:** `pwsh -ExecutionPolicy Bypass -File deploy\build-bundle.ps1 -SkipRuntimeFetch` → `done -> dist\nwdash-bundle-2.12.0-win-x64.zip`.
- [ ] **Step 5: Commit**
```bash
git add nwdash/config.py pyproject.toml README.md
git commit -m "chore(reports): bump to 2.12.0 — reports page + connection by copy"
```

---

## Deployment note
Ships as 2.12.0. `Setup-NWDash.cmd -Upgrade` + Ctrl+F5. Then: connect on the dashboard as normal → Account → Scheduled Reports (opens `/reports`) → **Use my current dashboard connection** → confirm the "Validated — N jobs in the last 24h" banner (if it says 0 jobs, the account/backup server is wrong — fix before relying on it) → set SMTP → enable groups → **Send now** with **test** ticked to confirm delivery.
