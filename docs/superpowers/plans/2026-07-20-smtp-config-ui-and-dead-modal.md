# SMTP settings UI + remove dead email-automation modal — Plan

> Execute via superpowers:subagent-driven-development. Steps use `- [ ]`.

**Goal:** Fix 2.10.0: remove the dead Email Alert Automation modal (every button hits the retired `/api/alert-automation` → HTTP 410), and add a working SMTP settings UI so the new Scheduled Reports subsystem is self-sufficient (SMTP host/port/security/user/password/from + ops-alert address, saved to `email_config.json`).

**Architecture:** The save/read logic already exists in `nwdash/snapshots.py` (`email_config_public`, `parse_smtp_settings`, `load_email_config`, atomic write). Add a dedicated `save_smtp_config` (SMTP block + ops address only, preserving `types` and a blank password) and wire an authed `POST /api/email-config`. Add an SMTP settings form to the dashboard. Delete the dead modal entirely.

**Reused:** `nwdash/snapshots.py`: `email_config_public()`, `load_email_config()`, `EMAIL_CONFIG_LOCK`, `EMAIL_CONFIG_FILE`, `encrypt_process_secret`. `server.py`: `email_config_public` already imported + GET `/api/email-config` at ~line 510; POST dispatch + `allowed` set + `_send_json`.

---

## Task 1: `save_smtp_config` + ops-address in `email_config_public`

**Files:** Modify `nwdash/snapshots.py`; Test `tests/test_smtp_config_save.py`

- [ ] **Step 1: Failing test** `tests/test_smtp_config_save.py`:
```python
import importlib
from nwdash import config, snapshots

def _fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EMAIL_CONFIG_FILE", tmp_path / "email_config.json")
    importlib.reload(snapshots)
    return snapshots

def test_save_smtp_config_writes_block_and_ops(tmp_path, monkeypatch):
    s = _fresh(tmp_path, monkeypatch)
    out = s.save_smtp_config({"host": "203.0.113.7", "port": 25, "security": "none",
                              "username": "", "from": "r@x.com", "password": "",
                              "opsAlertAddress": "ops@x.com"})
    assert out["ok"] is True
    pub = s.email_config_public()
    assert pub["smtp"]["host"] == "203.0.113.7" and pub["smtp"]["port"] == 25
    assert pub["smtp"]["from"] == "r@x.com"
    assert pub["opsAlertAddress"] == "ops@x.com"

def test_save_smtp_config_preserves_password_when_blank(tmp_path, monkeypatch):
    s = _fresh(tmp_path, monkeypatch)
    s.save_smtp_config({"host": "h", "port": 25, "security": "none", "from": "r@x.com",
                        "password": "secret", "opsAlertAddress": ""})
    assert s.saved_email_smtp_password() == "secret"
    # save again with blank password -> keeps it
    s.save_smtp_config({"host": "h2", "port": 25, "security": "none", "from": "r@x.com",
                        "password": "", "opsAlertAddress": ""})
    assert s.saved_email_smtp_password() == "secret"
    assert s.email_config_public()["smtp"]["host"] == "h2"

def test_save_smtp_config_preserves_types(tmp_path, monkeypatch):
    s = _fresh(tmp_path, monkeypatch)
    # seed a types block via the existing full save, then ensure smtp-only save keeps it
    import json
    (tmp_path / "email_config.json").write_text(json.dumps({"smtp": {}, "types": {"alert": {"recipients": ["a@x.com"]}}}), encoding="utf-8")
    s.save_smtp_config({"host": "h", "port": 25, "security": "none", "from": "r@x.com", "password": "", "opsAlertAddress": ""})
    cfg = json.loads((tmp_path / "email_config.json").read_text(encoding="utf-8"))
    assert cfg["types"]["alert"]["recipients"] == ["a@x.com"]
```

- [ ] **Step 2:** Run → FAIL (`save_smtp_config` missing). `python -m pytest tests/test_smtp_config_save.py -v`

- [ ] **Step 3: Implement** in `nwdash/snapshots.py` — add `save_smtp_config` near `save_email_config_from_payload`, and extend `email_config_public` to return `opsAlertAddress`:
```python
def save_smtp_config(payload: dict[str, Any]) -> dict[str, Any]:
    """Persist ONLY the shared SMTP transport + ops-alert address, preserving the
    per-type recipients. A blank password keeps the previously saved one."""
    with EMAIL_CONFIG_LOCK:
        cfg = load_email_config()
        prev = cfg.get("smtp") if isinstance(cfg.get("smtp"), dict) else {}
        encrypted = str(prev.get("encrypted_password") or "")
        pw = str(payload.get("password") or "")
        if pw:
            encrypted = encrypt_process_secret(pw)
        smtp = {
            "host": str(payload.get("host") or ""),
            "port": int(payload.get("port") or 587),
            "security": str(payload.get("security") or "starttls"),
            "username": str(payload.get("username") or ""),
            "from": str(payload.get("from") or ""),
            "encrypted_password": encrypted,
            "opsAlertAddress": str(payload.get("opsAlertAddress") or ""),
        }
        cfg["smtp"] = smtp
        _write_email_config(cfg)   # use the existing atomic writer near line 420; if it is inlined, replicate the tmp-write+replace used by save_email_config_from_payload
    return email_config_public()
```
Check how `save_email_config_from_payload` writes the file (atomic tmp+replace around line 420) and reuse the SAME mechanism (extract a small `_write_email_config(cfg)` helper if one does not exist, and have both callers use it).
In `email_config_public()`, add to the returned dict: `"opsAlertAddress": str(smtp.get("opsAlertAddress") or ""),`.

- [ ] **Step 4:** Run tests (3 passed), then FULL suite `python -m pytest -q`.

- [ ] **Step 5: Commit**
```bash
git add nwdash/snapshots.py tests/test_smtp_config_save.py
git commit -m "feat(email): save_smtp_config (SMTP block + ops address) + expose opsAlertAddress"
```

---

## Task 2: authed `POST /api/email-config`

**Files:** Modify `nwdash/server.py`; Test `tests/test_email_config_post.py`

- [ ] **Step 1: Locate** the GET `/api/email-config` (~line 510) and the POST dispatch + `allowed` set:
`grep -n "email-config\|email_config_public\|allowed = \|def do_POST" nwdash/server.py`
Confirm `save_smtp_config` import path: add `save_smtp_config` to the existing `from .snapshots import ...` block (or `from . import snapshots`).

- [ ] **Step 2: Failing test** `tests/test_email_config_post.py`:
```python
import importlib
from http import HTTPStatus
from nwdash import config, snapshots, server

def test_handle_email_config_post_saves(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EMAIL_CONFIG_FILE", tmp_path / "email_config.json")
    importlib.reload(snapshots); importlib.reload(server)
    status, body = server.handle_email_config_post({"host": "203.0.113.7", "port": 25,
        "security": "none", "from": "r@x.com", "password": "", "opsAlertAddress": "ops@x.com"})
    assert status == HTTPStatus.OK and body["ok"] is True
    assert body["smtp"]["host"] == "203.0.113.7"
    assert body["opsAlertAddress"] == "ops@x.com"
    assert "encrypted_password" not in str(body)   # never echoed
```

- [ ] **Step 3: Implement** in `server.py`. Add a module function:
```python
def handle_email_config_post(payload: dict) -> tuple[int, dict]:
    from .snapshots import save_smtp_config
    return HTTPStatus.OK, save_smtp_config(payload)
```
Route it in `do_POST` AFTER the auth gate (with other authed endpoints), and add `/api/email-config` to the POST `allowed` set:
```python
        if path == "/api/email-config":
            status, body = handle_email_config_post(payload)
            return self._send_json(status, body)
```

- [ ] **Step 4:** Run tests (1 passed), then FULL suite. `python -c "import nwdash.server"` clean.

- [ ] **Step 5: Commit**
```bash
git add nwdash/server.py tests/test_email_config_post.py
git commit -m "feat(email): authed POST /api/email-config to save SMTP settings"
```

---

## Task 3: remove the dead Email Alert Automation modal

**Files:** Modify `nwdash/assets/dashboard.html`, `nwdash/assets/app.js`; Test `tests/test_dead_modal_removed.py`

- [ ] **Step 1: Failing test** `tests/test_dead_modal_removed.py`:
```python
from pathlib import Path
A = Path(__file__).resolve().parents[1] / "nwdash" / "assets"

def test_old_modal_markup_gone():
    html = (A / "dashboard.html").read_text(encoding="utf-8")
    assert 'id="alertConfigBtn"' not in html
    assert 'id="alertAutomationModal"' not in html

def test_old_modal_js_gone():
    js = (A / "app.js").read_text(encoding="utf-8")
    assert "/api/alert-automation" not in js
    assert "openAlertAutomationModal" not in js
    assert "alertAutomationModal" not in js
```

- [ ] **Step 2:** Run → FAIL.

- [ ] **Step 3: Remove.** In `nwdash/assets/dashboard.html`:
  - Delete the `<button id="alertConfigBtn" ...>Email</button>` (topbar).
  - Delete the entire `id="alertAutomationModal"` modal section (the whole `.modal-backdrop` block for it, including the Saved schedules / Saved profiles / SMTP-fields / Schedule/Save/Test/Stop buttons markup — all element ids prefixed `email*`, `alert*` that belong to this modal).
In `nwdash/assets/app.js`, remove ALL code for the dead modal — grep first to get every line:
`grep -nE "alertConfigBtn|alertAutomationModal|alertModalCloseBtn|alertScheduleBtn|alertTestBtn|alertStopBtn|emailSaveConfigBtn|emailProfile|emailSchedule|openAlertAutomationModal|closeAlertAutomationModal|submitAlertAutomation|saveEmailProfile|loadEmailProfile|deleteEmailProfile|editEmailRow|deleteEmailRow|applyEmailTypeBlock|alertAutomationStatus|alertAutomationPayload|/api/alert-automation|bindBackdropDismiss\(alertAutomationModal" nwdash/assets/app.js`
Remove: the `const` lookups (lines ~110-116 and any `emailProfile*`/`emailSchedule*` consts), the functions (`openAlertAutomationModal`, `closeAlertAutomationModal`, `submitAlertAutomation`, `alertAutomationPayload`, `saveEmailProfile`, `loadEmailProfile`, `deleteEmailProfile`, `editEmailRow`, `deleteEmailRow`, `applyEmailTypeBlock`, and any helper only they use), the event listeners (`alertConfigBtn`, `alertModalCloseBtn`, `bindBackdropDismiss(alertAutomationModal…)`, schedule/test/stop/save/profile buttons, `emailScheduleType` change), and the `closeTopmostPopup()` reference to `alertAutomationModal` (remove just that branch, keep the function + other popups).
CRITICAL: a leftover `const x = document.getElementById("alertConfigBtn"); ... x.addEventListener(...)` on a now-removed element throws at init and kills the whole SPA. Remove BOTH the const and its listener. After editing, `grep` the removal list again and confirm ZERO matches remain (except unrelated names). Keep `bindBackdropDismiss` itself (used by Scheduled Reports/other modals) — only remove its `alertAutomationModal` call.

- [ ] **Step 4: Verify no dangling references + syntax.**
`node -c nwdash/assets/app.js` (must pass).
Re-grep the removal list — only `bindBackdropDismiss` (the definition + its non-alert callers) may remain.
Run `python -m pytest tests/test_dead_modal_removed.py -v` (2 passed), then FULL suite.

- [ ] **Step 5: Commit**
```bash
git add nwdash/assets/dashboard.html nwdash/assets/app.js tests/test_dead_modal_removed.py
git commit -m "fix(email): remove dead Email Alert Automation modal (retired /api/alert-automation)"
```

---

## Task 4: SMTP settings UI form

**Files:** Modify `nwdash/assets/dashboard.html`, `nwdash/assets/app.js`, `nwdash/assets/app.css`; Test `tests/test_smtp_settings_ui.py`

- [ ] **Step 1: Failing test** `tests/test_smtp_settings_ui.py`:
```python
from pathlib import Path
A = Path(__file__).resolve().parents[1] / "nwdash" / "assets"

def test_smtp_settings_markup():
    assert 'id="smtpSettingsForm"' in (A / "dashboard.html").read_text(encoding="utf-8")

def test_smtp_settings_js():
    js = (A / "app.js").read_text(encoding="utf-8")
    assert "loadSmtpSettings" in js and "/api/email-config" in js
```

- [ ] **Step 2:** Run → FAIL.

- [ ] **Step 3: Markup** in `dashboard.html` — add inside the `#scheduledReportsPanel` (top of it, before the jobs list) OR as a sibling panel right before it:
```html
<section id="smtpSettingsPanel" class="panel">
  <header class="panel-head"><h2>Email (SMTP) settings</h2></header>
  <p>Shared SMTP transport used by all scheduled reports and failure alerts.</p>
  <form id="smtpSettingsForm" class="report-form">
    <label>SMTP host <input name="host" required></label>
    <label>Port <input name="port" type="number" value="25"></label>
    <label>Security
      <select name="security"><option value="none">None</option><option value="starttls">STARTTLS</option><option value="ssl">SSL</option></select>
    </label>
    <label>Username <input name="username" autocomplete="off"></label>
    <label>Password <input name="password" type="password" placeholder="(unchanged)" autocomplete="new-password"></label>
    <label>From address <input name="from" required></label>
    <label>Ops-alert address <input name="opsAlertAddress" placeholder="who gets failure alerts"></label>
    <div id="smtpSettingsMsg" class="form-error" role="alert"></div>
    <button type="submit">Save SMTP settings</button>
  </form>
</section>
```

- [ ] **Step 4: JS** in `app.js`:
```javascript
async function loadSmtpSettings() {
  const f = document.getElementById("smtpSettingsForm");
  if (!f) return;
  try {
    const r = await fetch("/api/email-config", {cache: "no-store"});
    const d = await r.json();
    const s = d.smtp || {};
    f.host.value = s.host || ""; f.port.value = s.port || 25;
    f.security.value = s.security || "none"; f.username.value = s.username || "";
    f.from.value = s.from || ""; f.opsAlertAddress.value = d.opsAlertAddress || "";
    f.password.placeholder = s.passwordSaved ? "(unchanged — leave blank to keep)" : "";
  } catch (e) {}
}
async function submitSmtpSettings(ev) {
  ev.preventDefault();
  const f = ev.target, msg = document.getElementById("smtpSettingsMsg");
  msg.textContent = "Saving…"; msg.style.color = "";
  const r = await fetch("/api/email-config", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({host: f.host.value, port: Number(f.port.value), security: f.security.value,
      username: f.username.value, password: f.password.value, from: f.from.value,
      opsAlertAddress: f.opsAlertAddress.value})});
  const d = await r.json();
  if (d.ok) { msg.textContent = "Saved."; f.password.value = ""; loadSmtpSettings(); }
  else { msg.textContent = "Save failed."; }
}
function initSmtpSettings() {
  const f = document.getElementById("smtpSettingsForm");
  if (!f) return;
  f.addEventListener("submit", submitSmtpSettings);
  loadSmtpSettings();
}
```
Call `initSmtpSettings();` from the same top-level init path as `initScheduledReports();`.

- [ ] **Step 5:** `node -c nwdash/assets/app.js`. Run `python -m pytest tests/test_smtp_settings_ui.py -v` (2 passed), then FULL suite.

- [ ] **Step 6: Commit**
```bash
git add nwdash/assets/dashboard.html nwdash/assets/app.js nwdash/assets/app.css tests/test_smtp_settings_ui.py
git commit -m "feat(email): SMTP settings UI form (load + save via /api/email-config)"
```

---

## Task 5: version bump 2.10.1 + build

- [ ] **Step 1:** `APP_VERSION = "2.10.1"` in `nwdash/config.py`; `version = "2.10.1"` in `pyproject.toml`.
- [ ] **Step 2:** FULL suite `python -m pytest -q` (0 failures).
- [ ] **Step 3:** `pwsh -ExecutionPolicy Bypass -File deploy\build-bundle.ps1 -SkipRuntimeFetch` → `done -> dist\nwdash-bundle-2.10.1-win-x64.zip`.
- [ ] **Step 4: Commit**
```bash
git add nwdash/config.py pyproject.toml
git commit -m "chore(email): bump to 2.10.1 — SMTP settings UI + dead-modal removal"
```

---

## Deployment note
Ships as 2.10.1 (patch). `Setup-NWDash.cmd -Upgrade`. After upgrade: the dead Email modal is gone; configure SMTP under **Email (SMTP) settings**; scheduled reports + ops alerts then use it.
