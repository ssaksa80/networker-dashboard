# Email UI Fixes + Notification Enhancements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the invisible Account button on the default theme, collapse the modal's saved-schedule list behind a dropdown with per-schedule pause checkboxes, and add quiet hours, digest, and per-schedule severity to email automations.

**Architecture:** Single-file Python 3.9+ stdlib app (`networker_dashboard.py`). Email automations are in-memory `AlertAutomation` dataclasses that self-reschedule with a daemon `threading.Timer` (no central loop, no disk persistence). The HTML/CSS/JS dashboard is embedded in the `HTML_PAGE` string. New fields get safe defaults; the alert send path (`run_alert_automation`) reads them.

**Tech Stack:** Python stdlib (`http.server`, `threading`, `smtplib`, `unittest`), embedded HTML/CSS/JS.

---

### Task 1: Bug A — Account button contrast (CSS)

**Files:**
- Modify: `networker_dashboard.py` (CSS in `HTML_PAGE`, `.collapse-toggle` rule ~line 1133)
- Test: `test_phase5.py` (new)

- [ ] **Step 1: Write the failing test**

Create `test_phase5.py`:

```python
import unittest
import networker_dashboard as nd


class AccountButtonContrastTests(unittest.TestCase):
    def test_topbar_collapse_toggle_has_explicit_light_style(self):
        html = nd.HTML_PAGE
        self.assertIn(".topbar .collapse-toggle", html)
        # White-on-dark, theme-independent, so the default theme's dark --ink
        # never makes the button vanish on the dark topbar.
        idx = html.index(".topbar .collapse-toggle")
        block = html[idx:idx + 220]
        self.assertIn("color: #ffffff", block)

    def test_collapse_toggle_uses_correct_surface_variable(self):
        html = nd.HTML_PAGE
        # The base rule must not reference the non-existent --surface2 typo.
        self.assertNotIn("var(--surface2)", html)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest test_phase5.AccountButtonContrastTests -v`
Expected: FAIL — `.topbar .collapse-toggle` not present and/or `var(--surface2)` still present.

- [ ] **Step 3: Fix the CSS**

In the base `.collapse-toggle` rule (~line 1133), change `background: var(--surface2);` to `background: var(--surface-2);`.

Immediately after the `.collapse-toggle:hover { ... }` line (~line 1139), add:

```css
    .topbar .collapse-toggle {
      background: rgba(255, 255, 255, 0.10);
      color: #ffffff;
      border-color: rgba(255, 255, 255, 0.22);
    }
    .topbar .collapse-toggle:hover { border-color: rgba(255, 255, 255, 0.5); }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest test_phase5.AccountButtonContrastTests -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add networker_dashboard.py test_phase5.py
git commit -m "fix(ui): make Account dropdown button visible on the default theme"
```

---

### Task 2: Backend — `enabled` flag + `set_enabled` action + pause skip

**Files:**
- Modify: `networker_dashboard.py`
  - `AlertAutomation` dataclass (~line 5805, after `theme`)
  - `run_alert_automation` (~line 10685, top of `try`)
  - `handle_alert_automation` — `list` row dict (~line 10768) and a new `set_enabled` branch (after the `list` branch, ~line 10784)
- Test: `test_phase5.py`

- [ ] **Step 1: Write the failing test**

Append to `test_phase5.py`:

```python
import time
import networker_dashboard as nd
from networker_dashboard import AlertAutomation


def _make_automation(session_id="sess1", schedule_type="alert", enabled=True):
    return AlertAutomation(
        automation_id="auto1",
        session_id=session_id,
        smtp_host="10.0.0.1",
        smtp_port=25,
        smtp_username="",
        encrypted_smtp_password=nd.encrypt_process_secret(""),
        smtp_from="dash@example.com",
        recipients=["ops@example.com"],
        smtp_security="none",
        interval_minutes=60,
        trigger="critical",
        schedule_type=schedule_type,
        report_time="08:00",
        created_at=time.time(),
        theme="default",
        enabled=enabled,
    )


class EnableDisableTests(unittest.TestCase):
    def setUp(self):
        nd.ALERT_AUTOMATIONS.clear()

    def tearDown(self):
        nd.ALERT_AUTOMATIONS.clear()

    def test_set_enabled_flips_flag(self):
        auto = _make_automation()
        nd._put_automation(auto.automation_id, auto)
        status, body = nd.handle_alert_automation(
            {"action": "set_enabled", "sessionId": "sess1",
             "automationId": "auto1", "enabled": False})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertFalse(body["enabled"])
        self.assertFalse(nd._get_automation("auto1").enabled)

    def test_set_enabled_rejects_other_session(self):
        auto = _make_automation(session_id="other")
        nd._put_automation(auto.automation_id, auto)
        status, body = nd.handle_alert_automation(
            {"action": "set_enabled", "sessionId": "sess1",
             "automationId": "auto1", "enabled": False})
        self.assertEqual(status, 200)
        self.assertTrue(nd._get_automation("auto1").enabled)  # unchanged

    def test_list_returns_enabled(self):
        auto = _make_automation()
        nd._put_automation(auto.automation_id, auto)
        status, body = nd.handle_alert_automation(
            {"action": "list", "sessionId": "sess1"})
        self.assertEqual(body["schedules"][0]["enabled"], True)

    def test_disabled_automation_does_not_send(self, ):
        sent = []
        orig = nd.send_smtp_email
        nd.send_smtp_email = lambda *a, **k: sent.append(a) or {}
        orig_sched = nd.schedule_alert_automation
        nd.schedule_alert_automation = lambda a: None  # no real timer
        try:
            auto = _make_automation(enabled=False)
            nd._put_automation(auto.automation_id, auto)
            nd.run_alert_automation("auto1")
            self.assertEqual(sent, [])
            self.assertEqual(nd._get_automation("auto1").last_result, "Paused")
        finally:
            nd.send_smtp_email = orig
            nd.schedule_alert_automation = orig_sched
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest test_phase5.EnableDisableTests -v`
Expected: FAIL — `AlertAutomation` has no `enabled` kwarg / no `set_enabled` action.

- [ ] **Step 3: Add the `enabled` field**

In `AlertAutomation` (after `theme: str = "default"`, ~line 5805) add:

```python
    enabled: bool = True
```

- [ ] **Step 4: Skip sending when paused**

In `run_alert_automation`, immediately after the `try:` line (~line 10689) and before `status, dashboard = build_dashboard_from_session(...)`, add:

```python
        if not automation.enabled:
            automation.last_result = "Paused"
            return
```

(The existing `finally:` still calls `schedule_alert_automation`, so the timer keeps ticking and resume is immediate.)

- [ ] **Step 5: Add `enabled` to the `list` row dict**

In `handle_alert_automation`'s `list` branch, inside the `rows.append({...})` dict (~line 10782, after `"lastRun": automation.last_run,`), add:

```python
                "enabled": automation.enabled,
```

- [ ] **Step 6: Add the `set_enabled` action**

In `handle_alert_automation`, immediately after the `list` branch returns (after the `return HTTPStatus.OK, {"ok": True, "schedules": rows}` line, ~line 10784), add:

```python
    if action == "set_enabled":
        requested_id = str(payload.get("automationId") or "").strip()
        target = _get_automation(requested_id) if requested_id else None
        if not target or target.session_id != session_id:
            return HTTPStatus.OK, {"ok": True, "message": "No such schedule."}
        target.enabled = bool(payload.get("enabled"))
        return HTTPStatus.OK, {
            "ok": True,
            "automationId": requested_id,
            "enabled": target.enabled,
            "message": "Schedule resumed." if target.enabled else "Schedule paused.",
        }
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `python -m unittest test_phase5.EnableDisableTests -v`
Expected: PASS (4 tests)

- [ ] **Step 8: Commit**

```bash
git add networker_dashboard.py test_phase5.py
git commit -m "feat(email): per-schedule enable/disable (pause) without losing config"
```

---

### Task 3: Backend — quiet hours (alert schedules)

**Files:**
- Modify: `networker_dashboard.py`
  - `AlertAutomation` dataclass (~line 5805, after `enabled`)
  - new helper `within_quiet_hours` (near `should_send_alert`, ~line 10474)
  - `parse_smtp_settings` (~line 9605 return dict)
  - `handle_alert_automation` `AlertAutomation(...)` constructor (~line 10841) and `list` row dict
  - `run_alert_automation` alert branch (~line 10732)
- Test: `test_phase5.py`

- [ ] **Step 1: Write the failing test**

Append to `test_phase5.py`:

```python
class QuietHoursTests(unittest.TestCase):
    def test_within_window_same_day(self):
        self.assertTrue(nd.within_quiet_hours("22:00", "23:00", hhmm="22:30"))
        self.assertFalse(nd.within_quiet_hours("22:00", "23:00", hhmm="21:30"))

    def test_within_window_wraps_midnight(self):
        self.assertTrue(nd.within_quiet_hours("22:00", "06:00", hhmm="02:00"))
        self.assertTrue(nd.within_quiet_hours("22:00", "06:00", hhmm="23:30"))
        self.assertFalse(nd.within_quiet_hours("22:00", "06:00", hhmm="12:00"))

    def test_empty_window_never_quiet(self):
        self.assertFalse(nd.within_quiet_hours("", "", hhmm="03:00"))

    def test_parse_smtp_settings_reads_quiet(self):
        s = nd.parse_smtp_settings({
            "smtpHost": "10.0.0.1", "smtpFrom": "a@b.com", "smtpTo": "c@d.com",
            "quietStart": "22:00", "quietEnd": "06:00"})
        self.assertEqual(s["quiet_start"], "22:00")
        self.assertEqual(s["quiet_end"], "06:00")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest test_phase5.QuietHoursTests -v`
Expected: FAIL — `within_quiet_hours` not defined.

- [ ] **Step 3: Add the `within_quiet_hours` helper**

Immediately before `def should_send_alert(` (~line 10474) add:

```python
def within_quiet_hours(start: str, end: str, hhmm: str | None = None) -> bool:
    """True when the current time (HH:MM) falls inside [start, end).
    Empty start/end disables quiet hours. Windows may wrap past midnight
    (start > end), e.g. 22:00->06:00."""
    start = (start or "").strip()
    end = (end or "").strip()
    if not start or not end:
        return False
    try:
        if hhmm is None:
            hhmm = datetime.now().astimezone().strftime("%H:%M")
        def _mins(v: str) -> int:
            h, m = (int(p) for p in v.split(":", 1))
            return h * 60 + m
        now_m, s_m, e_m = _mins(hhmm), _mins(start), _mins(end)
    except Exception:
        return False
    if s_m == e_m:
        return False
    if s_m < e_m:
        return s_m <= now_m < e_m
    return now_m >= s_m or now_m < e_m  # wraps midnight
```

- [ ] **Step 4: Add quiet fields to dataclass**

In `AlertAutomation` (after `enabled: bool = True`) add:

```python
    quiet_start: str = ""
    quiet_end: str = ""
```

- [ ] **Step 5: Parse quiet fields**

In `parse_smtp_settings`, add before the `return` dict (~line 9605):

```python
    quiet_start = str(payload.get("quietStart") or "").strip()
    quiet_end = str(payload.get("quietEnd") or "").strip()
    for label, value in (("Quiet start", quiet_start), ("Quiet end", quiet_end)):
        if value and not TIME_HHMM_PATTERN.match(value):
            raise BadRequest(f"{label} must be HH:MM.")
```

And add to the returned dict:

```python
        "quiet_start": quiet_start,
        "quiet_end": quiet_end,
```

NOTE: if `TIME_HHMM_PATTERN` does not already exist, define it near the top
constants (search for `HOST_PATTERN`): `TIME_HHMM_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")`. Confirm before adding to avoid a duplicate.

- [ ] **Step 6: Pass quiet fields into the constructor + list**

In `handle_alert_automation`'s `AlertAutomation(...)` call (~line 10841) add after `theme=settings["theme"],`:

```python
        quiet_start=settings["quiet_start"],
        quiet_end=settings["quiet_end"],
```

In the `list` row dict add:

```python
                "quietStart": automation.quiet_start,
                "quietEnd": automation.quiet_end,
```

- [ ] **Step 7: Suppress alert sends during quiet hours**

In `run_alert_automation`, in the alert branch immediately after `severity, lines = dashboard_alert_lines(dashboard)` (~line 10732) add:

```python
        if within_quiet_hours(automation.quiet_start, automation.quiet_end):
            automation.last_result = f"Quiet hours: suppressed at {generated_at()}"
            automation.last_run = time.time()
            return
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `python -m unittest test_phase5.QuietHoursTests -v`
Expected: PASS (4 tests)

- [ ] **Step 9: Commit**

```bash
git add networker_dashboard.py test_phase5.py
git commit -m "feat(email): quiet hours suppress alert emails in a configurable window"
```

---

### Task 4: Backend — digest subject + per-schedule severity confirmation

**Files:**
- Modify: `networker_dashboard.py`
  - `AlertAutomation` dataclass (after `quiet_end`)
  - `parse_smtp_settings` return dict
  - `handle_alert_automation` constructor + `list` row dict
  - `run_alert_automation` alert subject line (~line 10736)
- Test: `test_phase5.py`

CONTEXT: `dashboard_alert_lines` already returns ALL current alert lines, and the
alert email sends them joined — i.e. the email is already a batched digest. So
`digest=True` (default) keeps the existing subject `"NetWorker dashboard alert:
<Severity>"`; `digest=False` prepends the highest-severity context for faster
single-line triage. `should_send_alert` already gates by `trigger`.

- [ ] **Step 1: Write the failing test**

Append to `test_phase5.py`:

```python
class DigestAndSeverityTests(unittest.TestCase):
    def test_parse_reads_digest(self):
        s = nd.parse_smtp_settings({
            "smtpHost": "10.0.0.1", "smtpFrom": "a@b.com", "smtpTo": "c@d.com",
            "digest": True})
        self.assertTrue(s["digest"])

    def test_parse_digest_defaults_false(self):
        s = nd.parse_smtp_settings({
            "smtpHost": "10.0.0.1", "smtpFrom": "a@b.com", "smtpTo": "c@d.com"})
        self.assertFalse(s["digest"])

    def test_alert_subject_for_digest(self):
        self.assertEqual(
            nd.alert_email_subject("critical", digest=True),
            "NetWorker dashboard alert: Critical")

    def test_alert_subject_for_single(self):
        self.assertEqual(
            nd.alert_email_subject("critical", digest=False),
            "NetWorker dashboard alert (single): Critical")

    def test_should_send_alert_respects_trigger(self):
        self.assertTrue(nd.should_send_alert("all", "warning"))
        self.assertTrue(nd.should_send_alert("critical", "critical"))
        self.assertFalse(nd.should_send_alert("critical", "warning"))

    def test_list_returns_trigger_and_digest(self):
        nd.ALERT_AUTOMATIONS.clear()
        auto = _make_automation()
        auto.digest = True
        nd._put_automation(auto.automation_id, auto)
        row = nd.handle_alert_automation(
            {"action": "list", "sessionId": "sess1"})[1]["schedules"][0]
        self.assertEqual(row["trigger"], "critical")
        self.assertTrue(row["digest"])
        nd.ALERT_AUTOMATIONS.clear()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest test_phase5.DigestAndSeverityTests -v`
Expected: FAIL — `alert_email_subject` not defined / no `digest` field.

- [ ] **Step 3: Add the `digest` field**

In `AlertAutomation` (after `quiet_end: str = ""`) add:

```python
    digest: bool = True
```

- [ ] **Step 4: Add the subject helper**

Immediately before `def run_alert_automation(` (~line 10685) add:

```python
def alert_email_subject(severity: str, digest: bool = True) -> str:
    label = (severity or "alert").title()
    if digest:
        return f"NetWorker dashboard alert: {label}"
    return f"NetWorker dashboard alert (single): {label}"
```

- [ ] **Step 5: Use the helper in the alert branch**

In `run_alert_automation`, replace the line (~line 10736):

```python
            subject = f"NetWorker dashboard alert: {severity.title()}"
```

with:

```python
            subject = alert_email_subject(severity, automation.digest)
```

- [ ] **Step 6: Parse + plumb `digest`**

In `parse_smtp_settings` return dict add:

```python
        "digest": bool(payload.get("digest", True)),
```

In `handle_alert_automation`'s `AlertAutomation(...)` call add:

```python
        digest=settings["digest"],
```

In the `list` row dict add:

```python
                "digest": automation.digest,
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `python -m unittest test_phase5.DigestAndSeverityTests -v`
Expected: PASS (6 tests)

- [ ] **Step 8: Commit**

```bash
git add networker_dashboard.py test_phase5.py
git commit -m "feat(email): digest subject style + per-schedule severity round-trip"
```

---

### Task 5: Frontend — schedule dropdown, pause checkbox, quiet/digest inputs

**Files:**
- Modify: `networker_dashboard.py` (HTML/CSS/JS in `HTML_PAGE`)
  - modal form: add quiet-hours inputs + digest checkbox (near `dailyReportTime`, ~line 3075)
  - schedule list: wrap in collapse toggle (~line 3013), add CSS
  - JS: `syncEmailTypeFields` (gray quiet/digest for daily), `refreshEmailScheduleList` render (~line 4287), `editEmailRow` (~line 4239), `alertAutomationPayload` (~line 4323), new `toggleEmailRow`
- Test: manual smoke (CSS/DOM behavior) + Task 6 string-presence assertions

- [ ] **Step 1: Add quiet-hours + digest form fields**

After the `Daily report time` `<label>` block (~line 3076, the one containing `id="dailyReportTime"`), add:

```html
            <label>
              Quiet hours start
              <input id="quietStart" placeholder="HH:MM" autocomplete="off" inputmode="numeric">
            </label>
            <label>
              Quiet hours end
              <input id="quietEnd" placeholder="HH:MM" autocomplete="off" inputmode="numeric">
            </label>
            <label class="email-checkbox">
              <input id="emailDigest" type="checkbox" checked>
              Digest (one email per interval)
            </label>
```

- [ ] **Step 2: Wrap the schedule list in a collapse toggle**

Replace the `<div id="emailScheduleList" ...></div>` line (~line 3013) with:

```html
          <button class="collapse-toggle" type="button" data-toggle-target="emailScheduleListWrap" aria-expanded="false" style="margin:8px 14px 0">
            <span class="caret">&#9656;</span> Saved schedules (<span id="emailScheduleCount">0</span>)
          </button>
          <div id="emailScheduleListWrap" class="collapsible">
            <div id="emailScheduleList" class="email-schedule-list" aria-label="Saved schedules"></div>
          </div>
```

- [ ] **Step 3: Add CSS for the email checkbox + paused row**

Find the `.email-schedule-list` / `.email-row` CSS rules (search `.email-row`) and add nearby:

```css
    .email-checkbox { flex-direction: row; align-items: center; gap: 8px; }
    .email-checkbox input { width: auto; }
    .email-row.is-disabled { opacity: 0.5; }
    .email-row .em-toggle { margin-right: 8px; }
```

- [ ] **Step 4: Gray quiet/digest fields for daily report**

In `syncEmailTypeFields` (~line 4224), before its closing brace add:

```javascript
      const quietStart = document.getElementById("quietStart");
      const quietEnd = document.getElementById("quietEnd");
      const digestEl = document.getElementById("emailDigest");
      [quietStart, quietEnd, digestEl].forEach(el => {
        if (!el) return;
        el.disabled = isDaily;
        const lbl = el.closest("label");
        if (lbl) lbl.classList.toggle("is-disabled", isDaily);
      });
```

- [ ] **Step 5: Add quiet/digest to the payload**

In `alertAutomationPayload` (~line 4323), add to the `payload` object literal (after `reportTime: ...`):

```javascript
        quietStart: document.getElementById("quietStart").value.trim(),
        quietEnd: document.getElementById("quietEnd").value.trim(),
        digest: document.getElementById("emailDigest").checked,
```

- [ ] **Step 6: Restore quiet/digest in editEmailRow**

In `editEmailRow` (~line 4239), before `syncEmailTypeFields();` add:

```javascript
      document.getElementById("quietStart").value = s.quietStart || "";
      document.getElementById("quietEnd").value = s.quietEnd || "";
      document.getElementById("emailDigest").checked = s.digest !== false;
```

- [ ] **Step 7: Render the pause checkbox + count in the list**

Replace the body of `refreshEmailScheduleList`'s `list.innerHTML = rows.map(...)` block and the count/handlers (~line 4287-4302) with:

```javascript
        document.getElementById("emailScheduleCount").textContent = rows.length;
        list.innerHTML = rows.map(s => {
          const typeLabel = s.scheduleType === "daily_report" ? "Daily report" : "Alert check";
          const cadence = s.scheduleType === "daily_report"
            ? ("at " + (s.reportTime || "08:00"))
            : ("every " + (s.intervalMinutes || 0) + " min");
          const paused = s.enabled === false;
          return '<div class="email-row' + (paused ? " is-disabled" : "") + '" data-id="' + escapeHtmlAttr(s.automationId) + '">'
            + '<label class="em-toggle"><input type="checkbox" class="em-active" data-id="' + escapeHtmlAttr(s.automationId) + '"' + (paused ? "" : " checked") + '> Active</label>'
            + '<strong>' + escapeHtmlAttr(typeLabel) + '</strong>'
            + '<span class="em-meta">' + escapeHtmlAttr(s.recipients || "") + ' &middot; '
            + escapeHtmlAttr(cadence) + ' &middot; ' + escapeHtmlAttr(s.trigger || "") + (paused ? ' &middot; (paused)' : '') + '</span>'
            + '<div class="em-actions">'
            + '<button type="button" class="ghost em-edit" data-id="' + escapeHtmlAttr(s.automationId) + '">Edit</button>'
            + '<button type="button" class="ghost em-del" data-id="' + escapeHtmlAttr(s.automationId) + '">Delete</button>'
            + '</div></div>';
        }).join("");
        list.querySelectorAll(".em-edit").forEach(b => b.addEventListener("click", () => editEmailRow(b.getAttribute("data-id"), rows)));
        list.querySelectorAll(".em-del").forEach(b => b.addEventListener("click", () => deleteEmailRow(b.getAttribute("data-id"))));
        list.querySelectorAll(".em-active").forEach(b => b.addEventListener("change", () => toggleEmailRow(b.getAttribute("data-id"), b.checked)));
```

(Also reset the count to 0 in the empty/early-return branches: in `refreshEmailScheduleList`, the `if (!rows.length) { list.innerHTML = ""; return; }` line becomes `if (!rows.length) { list.innerHTML = ""; document.getElementById("emailScheduleCount").textContent = "0"; return; }`.)

- [ ] **Step 8: Add the toggleEmailRow function**

Immediately after `deleteEmailRow` (~line 4270) add:

```javascript
    async function toggleEmailRow(id, active) {
      if (!sessionId) return;
      try {
        await fetch("/api/alert-automation", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({action: "set_enabled", sessionId, automationId: id, enabled: active}),
          cache: "no-store",
        });
      } catch (_e) {}
      refreshEmailScheduleList();
    }
```

- [ ] **Step 9: Syntax check + manual smoke**

Run: `python -c "import ast; ast.parse(open('networker_dashboard.py',encoding='utf-8').read()); print('OK')"`
Expected: OK

Manual: boot the app, open the Email modal. Confirm: Account button visible on default theme; "Saved schedules (N)" collapses/expands; selecting Daily report grays quiet/interval/digest; create two alert schedules, uncheck one's Active box, confirm it shows "(paused)" and dims; Edit restores quiet/digest values.

- [ ] **Step 10: Commit**

```bash
git add networker_dashboard.py
git commit -m "feat(email): schedule dropdown with pause checkboxes + quiet/digest controls"
```

---

### Task 6: Version bump + CHANGELOG + full regression

**Files:**
- Modify: `networker_dashboard.py` (`APP_VERSION`), `CHANGELOG.md`
- Test: all `test_phase*.py`

- [ ] **Step 1: Bump version**

In `networker_dashboard.py` change `APP_VERSION = "2.4.2"` to `APP_VERSION = "2.5.0"`.

- [ ] **Step 2: Add CHANGELOG entry**

At the top of `CHANGELOG.md` (above the `## [2.4.2]` entry) add:

```markdown
## [2.5.0] — 2026-06-19

### Added
- **Per-schedule pause.** Each saved email schedule has an **Active** checkbox;
  unchecking pauses sending without deleting the configuration (`set_enabled`
  action, in-memory `enabled` flag honoured by `run_alert_automation`).
- **Quiet hours** for alert schedules — alerts are suppressed inside a
  configurable `HH:MM`–`HH:MM` window (wraps midnight). Daily reports ignore it.
- **Digest toggle** — alert subject reflects digest (batched) vs. single style.
- Saved schedules now live behind a **"Saved schedules (N)" dropdown** instead of
  stacking inline in the modal.

### Fixed
- **Account dropdown button was invisible on the default theme** — the topbar
  toggle used an undefined CSS variable and the theme's dark `--ink`, rendering
  dark-on-dark. Now styled with explicit light-on-dark, theme-independent.
```

- [ ] **Step 3: Run the full test suite**

Run: `python -m unittest discover -p "test_phase*.py" -v`
Expected: all tests pass (Phase 1–4 regression + new Phase 5).

- [ ] **Step 4: Commit**

```bash
git add networker_dashboard.py CHANGELOG.md
git commit -m "chore: release v2.5.0 — email pause/quiet-hours/digest + Account button fix"
```

---

## Self-Review

- **Spec coverage:** Bug A → Task 1. Bug B (dropdown + pause) → Task 2 (backend) + Task 5 (UI). Quiet hours → Task 3. Digest → Task 4. Per-schedule severity → Task 4 (confirmation test + meta surface). Version/CHANGELOG → Task 6. All spec sections mapped.
- **Type consistency:** field names `enabled` / `quiet_start` / `quiet_end` / `digest` (Python) ↔ `enabled` / `quietStart` / `quietEnd` / `digest` (JSON) are consistent across dataclass, `parse_smtp_settings`, constructor, `list` dict, and JS. `within_quiet_hours`, `alert_email_subject`, `toggleEmailRow`, `set_enabled` referenced consistently.
- **Placeholders:** none — every code step shows full code.
- **Caveat:** Task 3 Step 5 conditionally defines `TIME_HHMM_PATTERN` — the implementer must grep first to avoid a duplicate definition.
