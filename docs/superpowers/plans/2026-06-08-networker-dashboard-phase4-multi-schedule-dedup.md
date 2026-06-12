# NetWorker Dashboard — Phase 4: Multi-Schedule Email + Daily-Report Dedup Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On top of v2.3.2 (scheduler-loop + persisted `data/automations.json`), (a) allow multiple independent email schedules per session — each created via the same modal with its own recipients / interval / time / trigger — and (b) skip duplicate daily reports when the dashboard content has not changed.

**Architecture:** All edits in `networker_dashboard.py`, confined to email-automation paths. Multi-schedule = new `automation_id = f"{session_id}:{uuid4().hex[:8]}"`; `handle_alert_automation` gains `action=list`, accepts `automationId` on `start`/`save`/`stop`. UI gains a "Saved schedules" list above the form with Edit/Delete. Dedup = a new `_dashboard_content_signature(dashboard)` (sha1 of stable summary + sorted job/alert identifiers, no timestamps) replaces the timestamp signature in `run_alert_automation`'s daily branch. Scheduler loop + persistence file format unchanged.

**Tech Stack:** Python 3 stdlib only (`hashlib` already imported). No new third-party deps.

---

### Task 1: Daily-report content signature + dedup

**Files:**
- Modify: `networker_dashboard.py` (new helper above `run_alert_automation`; replace one line in the daily branch)
- Test: `test_phase4.py` (create)

- [ ] **Step 1: Write the failing test**

Create `test_phase4.py`:
```python
import unittest

import networker_dashboard as nd


class DashboardSignatureTests(unittest.TestCase):
    def _make(self, *, total=10, fail=1, jobs=None, alerts=None, generated="2026-06-08T10:00:00Z"):
        return {
            "generatedAt": generated,
            "summary": {
                "totalJobs": total, "successfulJobs": total - fail, "failedJobs": fail,
                "activeJobs": 0, "recoveryJobs": 0, "cloneJobs": 0,
                "totalAlerts": len(alerts or []), "slaPercent": 95,
                "health": "ok", "range": "24h",
            },
            "tables": {
                "jobs": jobs or [{"name": "j1", "client": "c1", "status": "Succeeded"}],
                "failedJobs": [{"name": "fj", "client": "c1", "status": "Failed"}] if fail else [],
                "alerts": alerts or [],
            },
        }

    def test_same_content_same_signature(self):
        a = self._make()
        b = self._make()
        self.assertEqual(nd._dashboard_content_signature(a), nd._dashboard_content_signature(b))

    def test_timestamp_change_does_not_change_signature(self):
        a = self._make(generated="2026-06-08T10:00:00Z")
        b = self._make(generated="2026-06-08T11:00:00Z")
        self.assertEqual(nd._dashboard_content_signature(a), nd._dashboard_content_signature(b))

    def test_job_order_does_not_change_signature(self):
        a = self._make(jobs=[
            {"name": "j1", "client": "c1", "status": "Succeeded"},
            {"name": "j2", "client": "c2", "status": "Succeeded"},
        ])
        b = self._make(jobs=[
            {"name": "j2", "client": "c2", "status": "Succeeded"},
            {"name": "j1", "client": "c1", "status": "Succeeded"},
        ])
        self.assertEqual(nd._dashboard_content_signature(a), nd._dashboard_content_signature(b))

    def test_new_failed_job_changes_signature(self):
        a = self._make()
        b = self._make(fail=2)
        b["tables"]["failedJobs"] = [
            {"name": "fj", "client": "c1", "status": "Failed"},
            {"name": "fj2", "client": "c2", "status": "Failed"},
        ]
        self.assertNotEqual(nd._dashboard_content_signature(a), nd._dashboard_content_signature(b))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest test_phase4 -v`
Expected: FAIL with `AttributeError: module 'networker_dashboard' has no attribute '_dashboard_content_signature'`.

- [ ] **Step 3: Add the signature helper**

Find:
```python
def run_alert_automation(automation_id: str) -> None:
```
Insert IMMEDIATELY ABOVE that line:
```python
def _dashboard_content_signature(dashboard: dict[str, Any]) -> str:
    """Stable hash of meaningful dashboard fields (no timestamps), used for
    daily-report dedup so an unchanged dashboard is not re-emailed."""
    try:
        summary = dashboard.get("summary") if isinstance(dashboard.get("summary"), dict) else {}
        tables = dashboard.get("tables") if isinstance(dashboard.get("tables"), dict) else {}
        keys = ("totalJobs", "successfulJobs", "failedJobs", "activeJobs",
                "recoveryJobs", "cloneJobs", "totalAlerts", "slaPercent", "health", "range")
        payload: dict[str, Any] = {"summary": {k: summary.get(k) for k in keys}}

        def _job_key(j: Any) -> tuple[str, str, str]:
            if not isinstance(j, dict):
                return ("", "", "")
            return (str(j.get("name") or ""), str(j.get("client") or ""), str(j.get("status") or ""))

        def _alert_key(a: Any) -> tuple[str, str]:
            if not isinstance(a, dict):
                return ("", "")
            return (str(a.get("message") or ""), str(a.get("severity") or ""))

        payload["jobs"] = sorted(_job_key(j) for j in (tables.get("jobs") or []))
        payload["failedJobs"] = sorted(_job_key(j) for j in (tables.get("failedJobs") or []))
        payload["alerts"] = sorted(_alert_key(a) for a in (tables.get("alerts") or []))
        blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha1(blob).hexdigest()
    except Exception:
        return ""


```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest test_phase4 -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Wire dedup into the daily branch**

In `run_alert_automation`, find EXACTLY:
```python
            # Use the current dashboard theme (dynamic) so the report matches
            # whatever theme is set now, falling back to the theme captured when
            # the schedule was created.
            dashboard["theme"] = load_ui_theme() or automation.theme
            dashboard["scheduledReport"] = True
            plain, html_body, attachments = scheduled_dashboard_email_payload(dashboard)
            report_password = decrypt_process_secret(automation.encrypted_smtp_password)
            smtp_debug = send_smtp_email(
                automation,
                "NetWorker daily backup status and SLA report",
                plain,
                report_password,
                html_body,
                attachments=attachments,
            ) or smtp_debug_snapshot(automation, report_password, "sent")
            automation.last_signature = dashboard.get("generatedAt") or generated_at()
```
Replace with:
```python
            # Use the current dashboard theme (dynamic) so the report matches
            # whatever theme is set now, falling back to the theme captured when
            # the schedule was created.
            dashboard["theme"] = load_ui_theme() or automation.theme
            dashboard["scheduledReport"] = True
            new_signature = _dashboard_content_signature(dashboard)
            if new_signature and new_signature == automation.last_signature:
                automation.last_result = (
                    f"Skipped at {generated_at()}: no change since last successful report"
                )
                automation.last_run = time.time()
                return
            plain, html_body, attachments = scheduled_dashboard_email_payload(dashboard)
            report_password = decrypt_process_secret(automation.encrypted_smtp_password)
            smtp_debug = send_smtp_email(
                automation,
                "NetWorker daily backup status and SLA report",
                plain,
                report_password,
                html_body,
                attachments=attachments,
            ) or smtp_debug_snapshot(automation, report_password, "sent")
            automation.last_signature = new_signature or (dashboard.get("generatedAt") or generated_at())
```

- [ ] **Step 6: Parse + tests**

Run: `python -c "import ast; ast.parse(open('networker_dashboard.py',encoding='utf-8').read()); print('parse ok')"`
Run: `python -m unittest test_phase1 test_phase2 test_phase2b test_phase3 test_phase4 -v` → all PASS.

- [ ] **Step 7: Commit**
```bash
git add networker_dashboard.py test_phase4.py
git commit -m "fix(email): content-hash signature for daily reports; skip re-send on unchanged dashboard (H? / phase4)"
```
(Use `git -c user.name="dev" -c user.email="dev@local" commit ...` if needed.)

---

### Task 2: Multi-schedule backend — list / id-based stop / uuid-create-or-update

**Files:**
- Modify: `networker_dashboard.py` (`handle_alert_automation`)
- Test: `test_phase4.py`

- [ ] **Step 1: Write the failing test**

Append to `test_phase4.py`:
```python
class MultiScheduleTests(unittest.TestCase):
    def setUp(self):
        # Inject a fake live session and reset automations + scheduler timer noop.
        self._orig_session_exists = nd._session_exists
        self._orig_persist = nd.persist_automations
        self._orig_schedule = nd.schedule_alert_automation
        nd._session_exists = lambda sid: sid == "S1"
        nd.persist_automations = lambda: None
        nd.schedule_alert_automation = lambda automation: None
        with nd.REGISTRY_LOCK:
            nd.ALERT_AUTOMATIONS.clear()

    def tearDown(self):
        nd._session_exists = self._orig_session_exists
        nd.persist_automations = self._orig_persist
        nd.schedule_alert_automation = self._orig_schedule
        with nd.REGISTRY_LOCK:
            nd.ALERT_AUTOMATIONS.clear()

    def _start(self, recipients, automation_id=""):
        return nd.handle_alert_automation({
            "action": "start",
            "sessionId": "S1",
            "automationId": automation_id,
            "smtpHost": "smtp.example.com",
            "smtpPort": 25,
            "smtpSecurity": "none",
            "smtpFrom": "from@example.com",
            "smtpTo": recipients,
            "intervalMinutes": 30,
            "trigger": "all",
            "scheduleType": "daily_report",
            "reportTime": "08:00",
            "theme": "default",
        })

    def test_list_empty(self):
        status, body = nd.handle_alert_automation({"action": "list", "sessionId": "S1"})
        self.assertEqual(status, 200)
        self.assertEqual(body["schedules"], [])

    def test_start_twice_makes_two_rows(self):
        s1, b1 = self._start("a@x.com")
        s2, b2 = self._start("b@x.com")
        self.assertEqual(s1, 200)
        self.assertEqual(s2, 200)
        self.assertNotEqual(b1["automationId"], b2["automationId"])
        _, listed = nd.handle_alert_automation({"action": "list", "sessionId": "S1"})
        self.assertEqual(len(listed["schedules"]), 2)

    def test_start_with_existing_id_updates_in_place(self):
        _, b1 = self._start("a@x.com")
        first_id = b1["automationId"]
        _, b2 = self._start("a-updated@x.com", automation_id=first_id)
        self.assertEqual(b2["automationId"], first_id)
        _, listed = nd.handle_alert_automation({"action": "list", "sessionId": "S1"})
        self.assertEqual(len(listed["schedules"]), 1)
        self.assertEqual(listed["schedules"][0]["recipients"], "a-updated@x.com")

    def test_stop_by_id_removes_only_that_row(self):
        _, b1 = self._start("a@x.com")
        _, b2 = self._start("b@x.com")
        nd.handle_alert_automation({"action": "stop", "sessionId": "S1", "automationId": b1["automationId"]})
        _, listed = nd.handle_alert_automation({"action": "list", "sessionId": "S1"})
        ids = [s["automationId"] for s in listed["schedules"]]
        self.assertEqual(ids, [b2["automationId"]])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest test_phase4.MultiScheduleTests -v`
Expected: FAIL (the second `start` collides with the first because `automation_key(session_id, "daily_report")` returns the same key; `list` action does not exist yet).

- [ ] **Step 3: Add the `list` action**

In `handle_alert_automation`, find EXACTLY:
```python
    if action == "stop":
        raw_schedule_type = str(payload.get("scheduleType") or "").strip().lower()
        if raw_schedule_type in ("alert", "daily_report"):
```
Replace with:
```python
    if action == "list":
        rows = []
        for _key, automation in _automation_items_snapshot():
            if automation.session_id != session_id:
                continue
            rows.append({
                "automationId": automation.automation_id,
                "scheduleType": automation.schedule_type,
                "recipients": ", ".join(automation.recipients or []),
                "intervalMinutes": automation.interval_minutes,
                "reportTime": automation.report_time,
                "trigger": automation.trigger,
                "theme": automation.theme,
                "smtpHost": automation.smtp_host,
                "smtpPort": automation.smtp_port,
                "smtpSecurity": automation.smtp_security,
                "smtpUsername": automation.smtp_username,
                "smtpFrom": automation.smtp_from,
                "lastResult": automation.last_result,
                "lastRun": automation.last_run,
                "nextRunAt": getattr(automation, "next_run_at", 0.0),
            })
        return HTTPStatus.OK, {"ok": True, "schedules": rows}
    if action == "stop":
        requested_id = str(payload.get("automationId") or "").strip()
        if requested_id:
            target = _get_automation(requested_id)
            if target and target.session_id == session_id:
                cancel_alert_automation(requested_id)
                persist_automations()
                return HTTPStatus.OK, {
                    "ok": True,
                    "message": "Schedule stopped.",
                    "activeAutomations": active_automation_summary(session_id),
                }
            return HTTPStatus.OK, {
                "ok": True,
                "message": "No such schedule.",
                "activeAutomations": active_automation_summary(session_id),
            }
        raw_schedule_type = str(payload.get("scheduleType") or "").strip().lower()
        if raw_schedule_type in ("alert", "daily_report"):
```

- [ ] **Step 4: uuid-create-or-update on `start`**

In `handle_alert_automation`, find EXACTLY:
```python
    settings = parse_smtp_settings(payload)
    automation_id = automation_key(session_id, settings["schedule_type"])
    existing = existing_smtp_automation(session_id, settings["schedule_type"])
    smtp_password = settings["smtp_password"] or (
        decrypt_process_secret(existing.encrypted_smtp_password) if existing else ""
    ) or saved_email_smtp_password()
```
Replace with:
```python
    settings = parse_smtp_settings(payload)
    requested_id = str(payload.get("automationId") or "").strip()
    existing_by_id = _get_automation(requested_id) if requested_id else None
    if existing_by_id and existing_by_id.session_id == session_id:
        automation_id = existing_by_id.automation_id
    else:
        automation_id = f"{session_id}:{uuid.uuid4().hex[:8]}"
    existing = existing_by_id or existing_smtp_automation(session_id, settings["schedule_type"])
    smtp_password = settings["smtp_password"] or (
        decrypt_process_secret(existing.encrypted_smtp_password) if existing else ""
    ) or saved_email_smtp_password()
```

- [ ] **Step 5: uuid-create-or-update on `save` (when arming a schedule)**

In `handle_alert_automation`, find EXACTLY:
```python
                if can_schedule:
                    automation_id = automation_key(session_id, settings["schedule_type"])
                    existing = existing_smtp_automation(session_id, settings["schedule_type"])
                    smtp_password = settings["smtp_password"] or (
                        decrypt_process_secret(existing.encrypted_smtp_password) if existing else ""
                    ) or saved_email_smtp_password()
```
Replace with:
```python
                if can_schedule:
                    requested_id = str(payload.get("automationId") or "").strip()
                    existing_by_id = _get_automation(requested_id) if requested_id else None
                    if existing_by_id and existing_by_id.session_id == session_id:
                        automation_id = existing_by_id.automation_id
                    else:
                        automation_id = f"{session_id}:{uuid.uuid4().hex[:8]}"
                    existing = existing_by_id or existing_smtp_automation(session_id, settings["schedule_type"])
                    smtp_password = settings["smtp_password"] or (
                        decrypt_process_secret(existing.encrypted_smtp_password) if existing else ""
                    ) or saved_email_smtp_password()
```

- [ ] **Step 6: Return the resolved id in the start response**

In `handle_alert_automation`, find EXACTLY:
```python
    return HTTPStatus.OK, {
        "ok": True,
        "message": message,
        "activeAutomations": active_summary,
    }
```
Replace with:
```python
    return HTTPStatus.OK, {
        "ok": True,
        "automationId": automation_id,
        "message": message,
        "activeAutomations": active_summary,
    }
```

- [ ] **Step 7: Parse + tests**

Run: `python -c "import ast; ast.parse(open('networker_dashboard.py',encoding='utf-8').read()); print('parse ok')"`
Run: `python -m unittest test_phase1 test_phase2 test_phase2b test_phase3 test_phase4 -v` → all PASS.

- [ ] **Step 8: Commit**
```bash
git add networker_dashboard.py test_phase4.py
git commit -m "feat(email): multi-schedule per session — list action + uuid create-or-update + id-based stop (phase4)"
```

---

### Task 3: Modal UI — Saved schedules list + Edit / Delete

**Files:**
- Modify: `networker_dashboard.py` (modal HTML + CSS + JS, all inside `dashboard_html`)

- [ ] **Step 1: Insert the list container into the modal**

In the modal `<div id="alertAutomationModal" ...>` block, find EXACTLY:
```python
            <button id="alertModalCloseBtn" class="ghost modal-close" type="button" aria-label="Close email automation popup">x</button>
          </div>
          <div class="automation-grid">
```
Replace with:
```python
            <button id="alertModalCloseBtn" class="ghost modal-close" type="button" aria-label="Close email automation popup">x</button>
          </div>
          <div id="emailScheduleList" class="email-schedule-list" aria-label="Saved schedules"></div>
          <div class="automation-grid">
```

- [ ] **Step 2: Add CSS for the list + rows**

Find EXACTLY:
```python
    .automation-status {
      font-size: 12px;
      color: var(--muted);
      font-weight: 720;
      white-space: pre-wrap;
    }
```
Insert IMMEDIATELY BELOW it:
```python

    .email-schedule-list {
      display: flex; flex-direction: column; gap: 6px;
      padding: 10px 14px 0;
    }
    .email-schedule-list:empty { display: none; }
    .email-row {
      display: flex; align-items: center; gap: 10px;
      padding: 8px 10px; border: 1px solid var(--line);
      border-radius: 8px; background: var(--surface2);
      font-size: 12px;
    }
    .email-row strong { color: var(--ink); font-weight: 700; min-width: 110px; }
    .email-row .em-meta {
      color: var(--muted); flex: 1; min-width: 0;
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }
    .email-row .em-actions { display: flex; gap: 6px; }
    .email-row button { font-size: 12px; padding: 4px 10px; }
```

- [ ] **Step 3: Add JS state + helpers + wiring**

Find EXACTLY:
```python
    function openAlertAutomationModal() {
      alertAutomationModal.classList.add("open");
      alertAutomationModal.setAttribute("aria-hidden", "false");
      syncSmtpSecurityFields();
      loadEmailConfigIntoForm();
      setTimeout(() => document.getElementById("smtpHost").focus(), 0);
    }
```
Replace with:
```python
    let currentEmailAutomationId = "";

    function _emailEscape(s) {
      return String(s == null ? "" : s)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;")
        .replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
    }

    function editEmailRow(id, rows) {
      const s = (rows || []).find(r => r.automationId === id);
      if (!s) return;
      currentEmailAutomationId = id;
      document.getElementById("emailScheduleType").value = s.scheduleType || "alert";
      document.getElementById("alertIntervalMinutes").value = s.intervalMinutes || 60;
      document.getElementById("dailyReportTime").value = s.reportTime || "08:00";
      document.getElementById("alertTrigger").value = s.trigger || "critical";
      document.getElementById("smtpTo").value = s.recipients || "";
      if (s.smtpHost) document.getElementById("smtpHost").value = s.smtpHost;
      if (s.smtpPort) smtpPort.value = s.smtpPort;
      if (s.smtpSecurity) smtpSecurity.value = s.smtpSecurity;
      if (s.smtpUsername) smtpUsername.value = s.smtpUsername;
      if (s.smtpFrom) document.getElementById("smtpFrom").value = s.smtpFrom;
      syncSmtpSecurityFields();
      applyEmailTypeBlock();
      alertAutomationStatus.textContent = "Editing schedule " + id + " - update fields and click Schedule or Save.";
    }

    async function deleteEmailRow(id) {
      if (!sessionId) return;
      try {
        await fetch("/api/alert-automation", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({action: "stop", sessionId, automationId: id}),
          cache: "no-store",
        });
      } catch (_e) {}
      if (currentEmailAutomationId === id) currentEmailAutomationId = "";
      refreshEmailScheduleList();
    }

    async function refreshEmailScheduleList() {
      const list = document.getElementById("emailScheduleList");
      if (!list) return;
      if (!sessionId) { list.innerHTML = ""; return; }
      try {
        const r = await fetch("/api/alert-automation", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({action: "list", sessionId}),
          cache: "no-store",
        });
        const data = await r.json();
        if (!r.ok || !data.ok) { list.innerHTML = ""; return; }
        const rows = data.schedules || [];
        if (!rows.length) { list.innerHTML = ""; return; }
        list.innerHTML = rows.map(s => {
          const typeLabel = s.scheduleType === "daily_report" ? "Daily report" : "Alert check";
          const cadence = s.scheduleType === "daily_report"
            ? ("at " + (s.reportTime || "08:00"))
            : ("every " + (s.intervalMinutes || 0) + " min");
          return '<div class="email-row" data-id="' + _emailEscape(s.automationId) + '">'
            + '<strong>' + _emailEscape(typeLabel) + '</strong>'
            + '<span class="em-meta">' + _emailEscape(s.recipients || "") + ' &middot; '
            + _emailEscape(cadence) + ' &middot; ' + _emailEscape(s.trigger || "") + '</span>'
            + '<div class="em-actions">'
            + '<button type="button" class="ghost em-edit" data-id="' + _emailEscape(s.automationId) + '">Edit</button>'
            + '<button type="button" class="ghost em-del" data-id="' + _emailEscape(s.automationId) + '">Delete</button>'
            + '</div></div>';
        }).join("");
        list.querySelectorAll(".em-edit").forEach(b => b.addEventListener("click", () => editEmailRow(b.getAttribute("data-id"), rows)));
        list.querySelectorAll(".em-del").forEach(b => b.addEventListener("click", () => deleteEmailRow(b.getAttribute("data-id"))));
      } catch (_e) { list.innerHTML = ""; }
    }

    function openAlertAutomationModal() {
      alertAutomationModal.classList.add("open");
      alertAutomationModal.setAttribute("aria-hidden", "false");
      syncSmtpSecurityFields();
      loadEmailConfigIntoForm();
      applyEmailTypeBlock();
      refreshEmailScheduleList();
      setTimeout(() => document.getElementById("smtpHost").focus(), 0);
    }
```

- [ ] **Step 4: Clear id on close**

Find EXACTLY:
```python
    function closeAlertAutomationModal() {
      alertAutomationModal.classList.remove("open");
      alertAutomationModal.setAttribute("aria-hidden", "true");
      smtpPassword.value = "";
      alertConfigBtn.focus();
    }
```
Replace with:
```python
    function closeAlertAutomationModal() {
      alertAutomationModal.classList.remove("open");
      alertAutomationModal.setAttribute("aria-hidden", "true");
      smtpPassword.value = "";
      currentEmailAutomationId = "";
      alertConfigBtn.focus();
    }
```

- [ ] **Step 5: Include `automationId` in the request payload**

Find EXACTLY:
```python
    function alertAutomationPayload(action) {
      const payload = {
        action,
        sessionId,
        smtpHost: document.getElementById("smtpHost").value.trim(),
```
Replace with:
```python
    function alertAutomationPayload(action) {
      const payload = {
        action,
        sessionId,
        automationId: currentEmailAutomationId || "",
        smtpHost: document.getElementById("smtpHost").value.trim(),
```

- [ ] **Step 6: Clear id + refresh list after a successful start / save / stop**

Find EXACTLY:
```python
        if (action === "test") setStatus("Test email sent", "ok");
        if (action === "start") setStatus(payload.scheduleType === "daily_report" ? "Report scheduled" : "Alerts scheduled", "ok");
        if (action === "stop") setStatus("Schedule stopped", "neutral");
```
Replace with:
```python
        if (action === "test") setStatus("Test email sent", "ok");
        if (action === "start") setStatus(payload.scheduleType === "daily_report" ? "Report scheduled" : "Alerts scheduled", "ok");
        if (action === "stop") setStatus("Schedule stopped", "neutral");
        if (action === "save") setStatus("Configuration saved", "ok");
        if (action === "start" || action === "save" || action === "stop") {
          currentEmailAutomationId = "";
          refreshEmailScheduleList();
        }
```

- [ ] **Step 7: Parse + tests**

Run: `python -c "import ast; ast.parse(open('networker_dashboard.py',encoding='utf-8').read()); print('parse ok')"`
Run: `python -m unittest test_phase1 test_phase2 test_phase2b test_phase3 test_phase4 -v` → all PASS.
Quick marker check:
```bash
python -c "import networker_dashboard as nd; h=nd.dashboard_html(); [print(('FOUND ' if m in h else 'MISS  ')+m) for m in ['id=\"emailScheduleList\"','refreshEmailScheduleList','currentEmailAutomationId','email-row','editEmailRow']]"
```
Expected: all FOUND.

- [ ] **Step 8: Commit**
```bash
git add networker_dashboard.py
git commit -m "feat(email): saved-schedules list + Edit/Delete UI in the email modal (phase4)"
```

---

### Task 4: Version bump + CHANGELOG

**Files:**
- Modify: `networker_dashboard.py` (APP_VERSION)
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Bump version**

In `networker_dashboard.py`, find EXACTLY:
```python
APP_VERSION = "2.3.2"
```
Replace with:
```python
APP_VERSION = "2.4.0"
```

- [ ] **Step 2: CHANGELOG entry**

In `CHANGELOG.md`, find EXACTLY:
```markdown
## [2.3.2] — 2026-06-08
```
Insert IMMEDIATELY ABOVE it:
```markdown
## [2.4.0] — 2026-06-08

### Added
- Email Automation now supports **multiple scheduled reports per session**. The
  modal lists every saved schedule with **Edit** / **Delete** controls; the
  Schedule / Save buttons either create a new row or update the row being
  edited. SMTP server fields stay shared per session; each schedule keeps its
  own email type, interval **or** daily time, trigger, recipients, and theme.
- New `POST /api/alert-automation` `action=list` returns all schedules for the
  current session (no SMTP password). `action=stop` accepts an `automationId`
  to stop a specific row; the legacy `scheduleType` stop is kept.

### Fixed
- Daily reports no longer re-send when the dashboard has not changed. The
  signature is now a content hash (`sha1` of stable summary counts + sorted
  job / failed-job / alert identifiers — no timestamps). When the new signature
  matches the last one, the send is skipped with `last_result = "Skipped at
  <ts>: no change since last successful report"`. Previously the signature was
  `generatedAt`, which always changed, so an unchanged report would still be
  emailed on every cycle. The signature is persisted in `data/automations.json`,
  so dedup survives a restart.

### Changed
- Each schedule now uses a per-row id (`<session_id>:<uuid>`). A session is no
  longer capped at one alert + one daily-report entry.

```

- [ ] **Step 3: Parse + tests**

Run: `python -c "import ast; ast.parse(open('networker_dashboard.py',encoding='utf-8').read()); print('parse ok')"`
Run: `python -m unittest test_phase1 test_phase2 test_phase2b test_phase3 test_phase4 -v` → all PASS.
Sanity: `python -c "import networker_dashboard as nd; print('version', nd.APP_VERSION)"` → `version 2.4.0`.

- [ ] **Step 4: Commit**
```bash
git add networker_dashboard.py CHANGELOG.md
git commit -m "chore: bump APP_VERSION to 2.4.0 with phase 4 CHANGELOG entry"
```

---

### Task 5: Full regression + live smoke

**Files:** none (verification only)

- [ ] **Step 1: Unit suite**

Run: `python -m unittest test_phase1 test_phase2 test_phase2b test_phase3 test_phase4 -v` → all PASS.

- [ ] **Step 2: Server boot smoke**

Boot (background): `python networker_dashboard.py --no-launch --port 18448 --bind 127.0.0.1 --auth-password testpass`
Then (substitute port if needed):
```python
import ssl, json, urllib.request, urllib.error, http.cookiejar, time
ctx = ssl._create_unverified_context()
base = "https://localhost:18448"
def call(path, data=None, cookies=None):
    cj = cookies if cookies is not None else http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx), urllib.request.HTTPCookieProcessor(cj))
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(base+path, data=body, method="POST" if data is not None else "GET")
    if data is not None: req.add_header("Content-Type","application/json")
    try:
        r = op.open(req, timeout=5); return r.status, r.read(4000), cj
    except urllib.error.HTTPError as e:
        return e.code, e.read(4000), cj
for _ in range(40):
    try:
        if call("/api/health")[0] == 200: break
    except Exception: time.sleep(0.5)
s, _, cj = call("/api/login", {"password":"testpass"}); assert s == 200, s
# list before any schedule should be {ok:True, schedules:[]} (session id "" -> empty)
s, b, _ = call("/api/alert-automation", {"action":"list","sessionId":""}, cookies=cj)
print("list_empty_status", s, b[:120])
assert s == 200 and b'"schedules"' in b
print("LIST OK")
```
Expected: `LIST OK`. Stop the server (PowerShell: `Get-NetTCPConnection -LocalPort 18448 -State Listen | %{ Stop-Process -Id $_.OwningProcess -Force }`). Remove the test password: delete `data/auth.json`.

- [ ] **Step 3: Manual smoke (eyes on)**

Boot the server, open the dashboard, connect to a NetWorker session, open the Email modal:
- Schedule two daily reports for different recipients at different `report_time`s. Confirm both rows appear in the list, both fire from the scheduler loop, and `data/automations.json` contains two rows for that session.
- Edit one row, change the recipients, click Schedule (or Save) — confirm the same row updates (id unchanged).
- Delete the other row — confirm only the surviving row remains in the list and on disk.
- For dedup, with auto-refresh paused and the dashboard unchanged, force the daily run twice (e.g. set `report_time` to a near-future minute, restart). The second run should set `last_result = "Skipped ... no change since last successful report"` and not send. Changing any backup-job status, then forcing the next run, should send again.

- [ ] **Step 4: Final commit (optional, empty)**
```bash
git commit --allow-empty -m "test: phase 4 regression green"
```

- [ ] **Step 5: Push**
```bash
git push origin main
```

---

## Self-Review

**Spec coverage:**
- Content signature helper (`_dashboard_content_signature`) → Task 1 Step 3. ✔
- Daily branch wired to skip on unchanged signature → Task 1 Step 5. ✔
- `action=list` returning per-session rows → Task 2 Step 3. ✔
- `action=stop` with `automationId` → Task 2 Step 3. ✔
- `start`/`save` uuid-create-or-update → Task 2 Steps 4–5. ✔
- Response carries `automationId` → Task 2 Step 6. ✔
- Modal HTML list container → Task 3 Step 1. ✔
- CSS for list/rows → Task 3 Step 2. ✔
- JS state + helpers + Edit / Delete + post-action refresh → Task 3 Steps 3–6. ✔
- Version + CHANGELOG → Task 4. ✔
- Regression + smoke (incl. multi-row + dedup) → Task 5. ✔

**Placeholder scan:** none — all code blocks shown in full; all commands have an expected outcome.

**Type / name consistency:** `_dashboard_content_signature`, `currentEmailAutomationId`, `refreshEmailScheduleList`, `editEmailRow`, `deleteEmailRow`, `_emailEscape`, `emailScheduleList`, `email-row`, `email-schedule-list`, `automation_id` format `<session_id>:<uuid8>` are used consistently across tasks.

**Known limitations (documented):**
- The legacy `automation_key(session_id, schedule_type)` and the
  `existing_smtp_automation` fallback are retained for SMTP-password inheritance
  from any prior session entry. They are NOT used as the new entry's storage
  key.
- The alert-check signature (line ~11292) is already content-based and is not
  touched here.
- Scheduler-loop body, persistence file format, and the
  `applyEmailTypeBlock` gray-out behavior are unchanged.
