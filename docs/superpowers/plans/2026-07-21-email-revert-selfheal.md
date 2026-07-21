# Email Revert + Self-Healing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Revert all app code to the proven v2.8.3 email system (original Email Alert Automation modal + engine; the 2.9.0–2.12.0 reporting stack removed), then add exactly two enhancements: a self-healing connection (schedules adopt the newest live/persisted session instead of dying silently) and a per-schedule status chip.

**Architecture:** Task 1 is a wholesale tree revert (`git rm` + `git checkout v2.8.3 --`) of `nwdash/`, `tests/`, `deploy/build-bundle.ps1`, `README.md` — restoring the original engine, modal, wiring, and test suite in one deterministic step. Tasks 2–4 are small TDD diffs on top of the restored code: `sessions.latest_session_record()`, its adoption at arm+fire time inside `emailer.py`, and the status chip in the modal's schedule list. Task 5 bumps to 2.13.0 and builds the bundle.

**Tech Stack:** Python 3.12 stdlib, existing `nwdash` package, pytest, vanilla-JS SPA.

**Verified v2.8.3 facts the tasks rely on:**
- `git show v2.8.3:nwdash/emailer.py` imports resolve against v2.8.3 modules (the revert restores all of them together).
- `sessions.py`@v2.8.3 has `_session_to_dict(session_id, session)` and `_session_items_snapshot` (imported from models); `SESSION_PERSISTENCE_FILE` in config.
- Automation connection snapshots ARE `_session_to_dict` records; `recreate_session_from_snapshot` consumes that shape. So `latest_session_record()` returning the same shape plugs straight in.
- Fire-time give-up branches live in `emailer._ensure_automation_session` (the `if not snapshot:` branch and the undecryptable-credentials branch).
- Arm-time resolutions: `_arm_profile_automation` (`connection = connection_snapshot_for_session(resolved_session) or dict(stored_connection) or (dict(existing.connection) ...) `) and action=start (`connection = connection_snapshot_for_session(session_id) or (dict(existing.connection) ...)`).
- Modal list renderer: `app.js refreshEmailScheduleList()`; rows already carry `sessionLive`, `reconnectable`, `lastResult`.
- `deploy/install.ps1`/`lib/common.ps1`/`scripts/Setup-NWDash.cmd` are UNCHANGED since v2.8.3 — do not touch them.

---

## Task 1: Wholesale revert to v2.8.3

**Files:** `nwdash/` (entire tree), `tests/` (entire tree), `deploy/build-bundle.ps1`, `README.md`

- [ ] **Step 1: Revert the trees deterministically** (removes post-2.8.3 files AND restores originals in one shot):
```bash
cd /c/dev/networker-dashboard
git rm -rq nwdash tests
git checkout v2.8.3 -- nwdash tests deploy/build-bundle.ps1 README.md
```

- [ ] **Step 2: Verify the tree matches v2.8.3 exactly for those paths:**
```bash
git diff v2.8.3 --stat -- nwdash tests deploy/build-bundle.ps1 README.md
```
Expected: EMPTY output (no differences). Also confirm the new-stack files are gone:
```bash
ls nwdash/report_groups.py nwdash/display.py nwdash/assets/reports.html 2>&1
```
Expected: three "No such file" errors.

- [ ] **Step 3: App imports + suite green:**
```bash
python -c "import nwdash.main, nwdash.server, nwdash.emailer, nwdash.sessions"
python -m pytest -q
```
Expected: imports clean; suite = the v2.8.3 suite (~85 passed, 17 subtests). `tests/test_profile_toggle.py` E2E is a KNOWN flaky (restart/port race) — if it fails, re-run it alone once to confirm flake, and report it; do not chase it.

- [ ] **Step 4: Commit**
```bash
git add -A
git commit -m "revert(email): restore the v2.8.3 email engine, modal, and test suite; remove the 2.9.0-2.12.0 reporting stack"
```

---

## Task 2: `latest_session_record()`

**Files:** Modify `nwdash/sessions.py`; Test `tests/test_latest_session_record.py`

- [ ] **Step 1: Write the failing test**
```python
# tests/test_latest_session_record.py
import importlib, json
from nwdash import sessions, config

class _Cfg:
    rest_api_host="10.0.0.9"; rest_api_port=9090; backup_server_host="bkp"; backup_server_port=9090
    username="administrator"; api_mode="nwui"; api_version="auto"; report_range="24h"
    custom_start_date=""; custom_end_date=""; use_wmi_health=False; wmi_username=""
    timeout_seconds=30; verify_tls=False; use_authc_header=False

class _S:
    def __init__(self, last_used):
        self.config=_Cfg(); self.encrypted_networker_password="enc"; self.encrypted_wmi_password=""
        self.created_at=1.0; self.last_used=last_used

def test_live_sessions_win_and_newest_picked(monkeypatch):
    importlib.reload(sessions)
    monkeypatch.setattr(sessions, "_session_items_snapshot",
                        lambda: [("old", _S(10.0)), ("new", _S(99.0))])
    rec = sessions.latest_session_record()
    assert rec["session_id"] == "new"
    assert rec["config"]["rest_api_host"] == "10.0.0.9"

def test_disk_fallback_when_no_live(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SESSION_PERSISTENCE_FILE", tmp_path / "sessions.json")
    importlib.reload(sessions)
    monkeypatch.setattr(sessions, "_session_items_snapshot", lambda: [])
    (tmp_path / "sessions.json").write_text(json.dumps({
        "a": {"session_id": "a", "last_used": 5.0, "encrypted_networker_password": "e",
              "config": {"rest_api_host": "h1", "username": "u"}},
        "b": {"session_id": "b", "last_used": 50.0, "encrypted_networker_password": "e",
              "config": {"rest_api_host": "h2", "username": "u"}},
    }), encoding="utf-8")
    rec = sessions.latest_session_record()
    assert rec["session_id"] == "b" and rec["config"]["rest_api_host"] == "h2"

def test_none_when_nothing_anywhere(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SESSION_PERSISTENCE_FILE", tmp_path / "none.json")
    importlib.reload(sessions)
    monkeypatch.setattr(sessions, "_session_items_snapshot", lambda: [])
    assert sessions.latest_session_record() is None
```
NOTE: `sessions.py`@v2.8.3 imports `SESSION_PERSISTENCE_FILE` — check HOW (grep `SESSION_PERSISTENCE_FILE` in the file). If it's a `from .config import SESSION_PERSISTENCE_FILE` name-import, the monkeypatch on `config` won't reach it — in the implementation read it via `from . import config` → `config.SESSION_PERSISTENCE_FILE` at call time (as the test assumes). Do that in the new function regardless of how the rest of the module imports it.

- [ ] **Step 2: Run → FAIL.** `python -m pytest tests/test_latest_session_record.py -v`

- [ ] **Step 3: Implement** — append to `nwdash/sessions.py`:
```python
def latest_session_record() -> dict[str, Any] | None:
    """Newest usable connection record for self-healing schedules: prefer the
    most-recently-used LIVE session; else the newest record persisted in
    sessions.json; else None. Both sources share the _session_to_dict shape,
    which is exactly what automation connection snapshots store."""
    from . import config as _config
    items = _session_items_snapshot()
    if items:
        session_id, session = max(items, key=lambda kv: float(getattr(kv[1], "last_used", 0) or 0))
        return _session_to_dict(session_id, session)
    try:
        records = json.loads(_config.SESSION_PERSISTENCE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(records, dict) or not records:
        return None
    best = max(records.values(), key=lambda r: float((r or {}).get("last_used") or 0) if isinstance(r, dict) else 0)
    return best if isinstance(best, dict) and best.get("config") else None
```
(`json` and `Any` are already imported in sessions.py@v2.8.3; verify, add if missing.)

- [ ] **Step 4: Run → PASS (3).** FULL suite `python -m pytest -q`.

- [ ] **Step 5: Commit**
```bash
git add nwdash/sessions.py tests/test_latest_session_record.py
git commit -m "feat(email): latest_session_record — newest live or persisted connection record"
```

---

## Task 3: Self-healing adoption at arm + fire time

**Files:** Modify `nwdash/emailer.py`; Test `tests/test_selfheal.py`

- [ ] **Step 1: Write the failing test**
```python
# tests/test_selfheal.py
"""The one flaw the original engine had: a schedule without a session snapshot
went silently inert. These tests pin the self-healing behavior that fixes it."""
import importlib
from nwdash import emailer

def _rec(host="10.0.0.9"):
    return {"session_id": "s-live", "last_used": 9.0, "encrypted_networker_password": "enc",
            "config": {"rest_api_host": host, "username": "administrator"}}

def _automation(connection):
    from nwdash.models import AlertAutomation
    return AlertAutomation(
        automation_id="auto1", session_id="dead-session", connection=connection,
        smtp_host="h", smtp_port=25, smtp_username="", encrypted_smtp_password="",
        smtp_from="r@x.com", recipients=["a@x.com"], smtp_security="none",
        interval_minutes=60, trigger="critical", schedule_type="alert",
        report_time="08:00", created_at=1.0, theme="default",
    )

def test_fire_adopts_latest_record_when_snapshot_empty(monkeypatch):
    importlib.reload(emailer)
    monkeypatch.setattr(emailer, "_session_exists", lambda sid: False)
    monkeypatch.setattr(emailer, "latest_session_record", lambda: _rec())
    monkeypatch.setattr(emailer, "decrypt_process_secret", lambda s: "pw" if s else "")
    recreated = {}
    monkeypatch.setattr(emailer, "recreate_session_from_snapshot",
                        lambda sid, snap: recreated.update(host=snap["config"]["rest_api_host"]) or True)
    monkeypatch.setattr(emailer, "persist_automations", lambda: None)
    a = _automation(connection={})
    assert emailer._ensure_automation_session(a) is True
    assert recreated["host"] == "10.0.0.9"
    assert a.connection.get("config", {}).get("rest_api_host") == "10.0.0.9"  # adopted + stored

def test_fire_adopts_when_snapshot_undecryptable(monkeypatch):
    importlib.reload(emailer)
    monkeypatch.setattr(emailer, "_session_exists", lambda sid: False)
    monkeypatch.setattr(emailer, "latest_session_record", lambda: _rec("adopted-host"))
    # old snapshot decrypts to "" (dead key), adopted record decrypts fine
    monkeypatch.setattr(emailer, "decrypt_process_secret",
                        lambda s: "pw" if s == "enc" else "")
    monkeypatch.setattr(emailer, "recreate_session_from_snapshot", lambda sid, snap: True)
    monkeypatch.setattr(emailer, "persist_automations", lambda: None)
    a = _automation(connection={"encrypted_networker_password": "dead", "config": {"rest_api_host": "old"}})
    assert emailer._ensure_automation_session(a) is True
    assert a.connection["config"]["rest_api_host"] == "adopted-host"

def test_fire_still_waits_gracefully_when_nothing_anywhere(monkeypatch):
    importlib.reload(emailer)
    monkeypatch.setattr(emailer, "_session_exists", lambda sid: False)
    monkeypatch.setattr(emailer, "latest_session_record", lambda: None)
    a = _automation(connection={})
    assert emailer._ensure_automation_session(a) is False
    assert "waiting" in a.last_result.lower()

def test_arm_time_start_adopts_when_no_live_session(monkeypatch):
    importlib.reload(emailer)
    monkeypatch.setattr(emailer, "connection_snapshot_for_session", lambda sid: {})
    monkeypatch.setattr(emailer, "latest_session_record", lambda: _rec())
    assert emailer._resolve_connection({}, None)["config"]["rest_api_host"] == "10.0.0.9"

def test_arm_time_prefers_live_snapshot(monkeypatch):
    importlib.reload(emailer)
    monkeypatch.setattr(emailer, "latest_session_record", lambda: _rec("fallback"))
    live = {"session_id": "live", "config": {"rest_api_host": "live-host"}}
    assert emailer._resolve_connection(live, None)["config"]["rest_api_host"] == "live-host"
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement in `nwdash/emailer.py`:**

(a) Add `latest_session_record` to the existing `from .sessions import (...)` block.

(b) Add a small resolver helper (near `_arm_profile_automation`):
```python
def _resolve_connection(primary: dict | None, *fallbacks: dict | None) -> dict:
    """First non-empty connection snapshot, else adopt the newest live or
    persisted session record (self-healing: a schedule must never be armed
    or left connectionless while ANY proven session exists)."""
    for candidate in (primary, *fallbacks):
        if isinstance(candidate, dict) and candidate:
            return dict(candidate)
    adopted = latest_session_record()
    if adopted:
        debug_log("self-heal: adopting the newest session record as the schedule connection")
        return dict(adopted)
    return {}
```

(c) Arm time — replace the two resolutions with the helper:
In `_arm_profile_automation`:
```python
    connection = _resolve_connection(
        connection_snapshot_for_session(resolved_session),
        stored_connection,
        dict(existing.connection) if existing and existing.connection else None,
    )
```
In the action=start path:
```python
    connection = _resolve_connection(
        connection_snapshot_for_session(session_id),
        dict(existing.connection) if existing and existing.connection else None,
    )
```

(d) Fire time — in `_ensure_automation_session`, replace the two give-up branches with adoption-first logic:
```python
    snapshot = automation.connection if isinstance(automation.connection, dict) else {}
    usable = bool(snapshot) and bool(
        decrypt_process_secret(str(snapshot.get("encrypted_networker_password") or ""))
    )
    if not usable:
        adopted = latest_session_record()
        if adopted and decrypt_process_secret(str(adopted.get("encrypted_networker_password") or "")):
            automation.connection = dict(adopted)
            snapshot = automation.connection
            persist_automations()
            LOG.info(
                f"Email automation {automation.automation_id}: adopted the newest session "
                "connection (self-heal) after its own was missing or unreadable."
            )
        elif not snapshot:
            automation.last_result = (
                f"Waiting for a dashboard session at {generated_at()} — no saved connection is "
                "available yet; the schedule will heal itself as soon as anyone connects."
            )
            debug_log(f"automation {automation.automation_id}: no snapshot and nothing to adopt; run skipped")
            return False
        else:
            if automation.automation_id not in _SNAPSHOT_DECRYPT_WARNED:
                _SNAPSHOT_DECRYPT_WARNED.add(automation.automation_id)
                LOG.warning(
                    f"Email automation {automation.automation_id} ({automation.schedule_type}) cannot decrypt its "
                    "saved NetWorker credentials and no other session is available to adopt. It remains scheduled "
                    "and will heal itself as soon as anyone connects."
                )
            automation.last_result = (
                f"Saved connection credentials are unreadable ({generated_at()}); will adopt the next live connection."
            )
            return False
```
Keep the rest of the function (the `recreate_session_from_snapshot` attempt and its failure handling) unchanged after this block. IMPORTANT: preserve the exact behavior that a failed recreate keeps the schedule armed.

- [ ] **Step 4: Run → PASS (5).** FULL suite — the restored v2.8.3 email E2E suites must still pass (they exercise arm/fire paths; the helper only ADDS a fallback where the old code returned `{}`/gave up, so existing green tests must stay green — if one breaks, the integration changed behavior on a path that had a connection; fix the integration, not the test).

- [ ] **Step 5: Commit**
```bash
git add nwdash/emailer.py tests/test_selfheal.py
git commit -m "feat(email): self-healing connection — adopt the newest live/persisted session at arm + fire time"
```

---

## Task 4: Status chip in the schedule list

**Files:** Modify `nwdash/assets/app.js`, `nwdash/assets/app.css`; Test `tests/test_status_chip.py`

- [ ] **Step 1: Write the failing test**
```python
# tests/test_status_chip.py
from pathlib import Path
A = Path(__file__).resolve().parents[1] / "nwdash" / "assets"

def test_schedule_rows_render_status_chip():
    js = (A / "app.js").read_text(encoding="utf-8")
    assert "em-status" in js
    for status in ("live", "reconnectable", "waiting"):
        assert status in js

def test_status_chip_css():
    css = (A / "app.css").read_text(encoding="utf-8")
    assert ".em-status" in css
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement.** In `app.js refreshEmailScheduleList()`, replace the `linkNote` computation + its use with a status chip and last-result line. Replace:
```javascript
          const linkNote = s.sessionLive === false
            ? (s.reconnectable ? " · reconnects automatically" : " · waiting for a connection")
            : "";
```
with:
```javascript
          const status = s.sessionLive !== false ? "live" : (s.reconnectable ? "reconnectable" : "waiting");
          const statusChip = '<span class="em-status em-status-' + status + '">' + status + '</span>';
          const lastLine = s.lastResult ? '<span class="em-last">' + _emailEscape(s.lastResult) + '</span>' : "";
```
and in the row template, append the chip after the `<strong>` type label and the `lastLine` after the `em-meta` span:
```javascript
            + '<strong>' + _emailEscape(typeLabel) + '</strong>' + statusChip
            + '<span class="em-meta">' + _emailEscape(s.recipients || "") + ' &middot; '
            + _emailEscape(cadence) + ' &middot; ' + _emailEscape(s.trigger || "") + (paused ? ' &middot; (paused)' : '') + '</span>'
            + lastLine
```
(Remove the old `+ _emailEscape(linkNote)` from the em-meta span.)

- [ ] **Step 4: CSS** — append to `app.css`:
```css
.em-status { padding: 1px 8px; border-radius: 10px; font-size: 11px; font-weight: 700; margin-left: 6px; text-transform: uppercase; letter-spacing: .3px; }
.em-status-live { background: #16794322; color: #18764a; }
.em-status-reconnectable { background: #2457a622; color: #2457a6; }
.em-status-waiting { background: #a9680022; color: #a96800; }
.em-last { display: block; font-size: 12px; color: var(--muted); margin-top: 2px; }
```

- [ ] **Step 5:** `node -c nwdash/assets/app.js` (OK). Run `python -m pytest tests/test_status_chip.py -v` (2 passed), FULL suite. NOTE: a restored v2.8.3 UI test may assert the old "waiting for a connection" linkNote string — grep `tests/` for `linkNote|reconnects automatically|waiting for a connection`; if a test pins the old text, update that assertion to the chip markers and report it.

- [ ] **Step 6: Commit**
```bash
git add nwdash/assets/app.js nwdash/assets/app.css tests/test_status_chip.py
git commit -m "feat(email): live/reconnectable/waiting status chip + last result on schedule rows"
```

---

## Task 5: Version 2.13.0 + build

- [ ] **Step 1:** `nwdash/config.py`: `APP_VERSION = "2.8.3"` → `"2.13.0"` (the revert restored the old string). `pyproject.toml`: `version` → `"2.13.0"`.
- [ ] **Step 2:** FULL suite `python -m pytest -q` (green minus the known flaky E2E).
- [ ] **Step 3:** `pwsh -ExecutionPolicy Bypass -File deploy\build-bundle.ps1 -SkipRuntimeFetch` → `done -> dist\nwdash-bundle-2.13.0-win-x64.zip`. The reverted `$shipFiles` matches the reverted `nwdash/` tree, so the allow-list gate passes without edits.
- [ ] **Step 4: Commit**
```bash
git add nwdash/config.py pyproject.toml
git commit -m "chore(email): bump to 2.13.0 — restored original email engine + self-healing"
```

---

## Deployment note
Ships as 2.13.0. `Setup-NWDash.cmd -Upgrade` + hard refresh. Then: connect once in the browser — the two original schedules in `automations.json` reload and self-heal on the next tick (status chip flips waiting → reconnectable). Email is managed via the topbar **Email** button again. Point the DSO TV at plain `/tv` (the `/tv/<token>` URL is gone). SMTP config in `email_config.json` is reused unchanged.
