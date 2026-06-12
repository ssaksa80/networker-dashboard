# NetWorker Dashboard — Phase 4: Multi-Schedule Email + Daily-Report Dedup

Date: 2026-06-08
Status: Approved
Target file: `networker_dashboard.py` (single-file by design)
Tests: `test_phase4.py` (stdlib `unittest`)
Base: v2.3.2 (scheduler-loop + persisted `data/automations.json`)

## Goal

Close the two remaining email-automation gaps on top of the v2.3.2
scheduler-loop / persistence architecture:

- **#2 — Multiple scheduled email reports per session.** Today each
  `(session_id, schedule_type)` pair is capped at a single entry, so an operator
  cannot have, say, two daily reports going to different recipient groups at
  different times. Allow N independent schedules per session, each editable
  and removable from the modal.
- **#3 — No duplicate daily reports.** Today the daily-report signature is
  `dashboard.get("generatedAt")` (a timestamp), so two consecutive daily reports
  with identical content still both fire. Replace it with a content hash so an
  unchanged dashboard is never re-emailed.

Out of scope: the alert-check path (its signature is already content-based), the
scheduler loop, the persistence file format (new entries fit the existing
schema), and the gray-out / `applyEmailTypeBlock` logic (already shipped in
2.3.x).

## Background

Current v2.3.2 state (mapped):

- `AlertAutomation` has `automation_id`, `session_id`, `last_signature`,
  `next_run_at`. Persisted in `data/automations.json` keyed by `automation_id`.
- `automation_key(session_id, schedule_type) -> "<session>:<type>"` — used by
  `existing_smtp_automation` and as the storage key in `handle_alert_automation`
  for both `save` and `start`. Capped at two entries per session.
- `session_automation_keys(session_id)` already accepts BOTH a `session_id:`
  prefix AND a per-row `automation.session_id == session_id` match, so any new
  unique key that starts with `"<session_id>:"` participates in
  `active_automation_summary`, `cancel_session_automations`, etc.
- `automation_scheduler_loop` (30 s tick) iterates `ALERT_AUTOMATIONS` and fires
  due rows by `next_run_at` — no change needed to support more rows.
- `run_alert_automation` daily branch ends with
  `automation.last_signature = dashboard.get("generatedAt") or generated_at()` —
  the dedup defect for #3.

## Component 1 — Multi-schedule

### Key model
- New entries get `automation_id = f"{session_id}:{uuid.uuid4().hex[:8]}"`. The
  `session_id:` prefix preserves existing prefix-match helpers.
- `automation_key(session_id, schedule_type)` and `existing_smtp_automation` are
  kept unchanged for back-compat / SMTP password inheritance from any existing
  entry in the same session.

### Handler — `handle_alert_automation`
- **New `action=list`** — returns `{"ok": True, "schedules": [...]}` with rows
  for `automation.session_id == session_id`. Per row:
  `automationId`, `scheduleType`, `recipients` (joined string),
  `intervalMinutes`, `reportTime`, `trigger`, `theme`, `smtpHost`, `smtpPort`,
  `smtpSecurity`, `smtpUsername`, `smtpFrom`, `lastResult`, `lastRun`,
  `nextRunAt`. **No SMTP password.**
- **`action=stop`** — if `payload["automationId"]` is non-empty and resolves to
  an automation whose `session_id == session_id`, cancel that row only and
  `persist_automations()`. Legacy `scheduleType`-based stop kept as a fallback.
- **`action=start` and `action=save`** — read `payload.get("automationId")`. If
  it resolves to an existing entry whose `session_id == session_id`, REUSE that
  id (in-place update). Else mint a fresh
  `f"{session_id}:{uuid.uuid4().hex[:8]}"` and create a new row. Response now
  includes `"automationId"`.
- SMTP password inheritance from any prior session entry is unchanged
  (`existing_smtp_automation` fallback + `saved_email_smtp_password()`).

### Persistence
- Already keyed by `automation_id` in `automations.json` — no schema change. The
  file just gets N rows per session instead of up to two.

### Modal UI
- New container `<div id="emailScheduleList" class="email-schedule-list"></div>`
  inserted between the modal head and `.automation-grid` body.
- CSS: `.email-schedule-list` (flex column, hidden when empty), `.email-row`
  (border, two-column meta + actions).
- Module-level `let currentEmailAutomationId = ""`.
- `refreshEmailScheduleList()` posts `{action:"list", sessionId}` on modal open
  and after every save/start/stop. Renders rows: `<type> · <recipients> ·
  <cadence> · <trigger>` + Edit / Delete buttons.
- Edit click → `editEmailRow(id, rows)` populates every form field from the
  matching row (does NOT write `themeSelect` — keeps the 2.2.7 fix), calls
  `applyEmailTypeBlock()` + the SMTP-security sync, sets
  `currentEmailAutomationId = id`, updates the status text.
- Delete click → posts `{action:"stop", sessionId, automationId:id}`, clears
  `currentEmailAutomationId` if it matched, refreshes list.
- `alertAutomationPayload(action)` includes
  `automationId: currentEmailAutomationId || ""`.
- After a successful `start` / `save` / `stop`: clear
  `currentEmailAutomationId` (so the next click is treated as a new entry) and
  refresh the list.
- `closeAlertAutomationModal` clears `currentEmailAutomationId`.

## Component 2 — Daily-report dedup

### Signature
- New helper `_dashboard_content_signature(dashboard: dict) -> str`:
  - `payload["summary"]` = `summary` values for the keys
    `("totalJobs","successfulJobs","failedJobs","activeJobs","recoveryJobs",
       "cloneJobs","totalAlerts","slaPercent","health","range")`.
  - `payload["jobs"]` = sorted tuples `(name, client, status)` from
    `tables["jobs"]`.
  - `payload["failedJobs"]` = sorted tuples `(name, client, status)` from
    `tables["failedJobs"]`.
  - `payload["alerts"]` = sorted tuples `(message, severity)` from
    `tables["alerts"]`.
  - Hash = `hashlib.sha1(json.dumps(payload, sort_keys=True,
    default=str).encode("utf-8")).hexdigest()`.
  - Returns `""` on any exception (safe fallback — caller treats empty as "no
    signature available", i.e. send).

### Wire-in (`run_alert_automation` daily branch)
- Compute `new_signature = _dashboard_content_signature(dashboard)` after the
  `dashboard["theme"]` / `dashboard["scheduledReport"]` annotations and BEFORE
  building the email body.
- If `new_signature and new_signature == automation.last_signature`:
  - `automation.last_result = f"Skipped at {generated_at()}: no change since last successful report"`
  - `automation.last_run = time.time()`
  - return (do not send). The scheduler loop reschedules `next_run_at` as usual.
- Else: send as today. On success store
  `automation.last_signature = new_signature or (dashboard.get("generatedAt") or generated_at())`
  so the field is populated for the first send AND stays a stable content hash
  thereafter.
- `persist_automations()` already writes `last_signature` to disk → dedup
  survives a restart.

## Tests (`test_phase4.py`)

Use a temp `DATA_DIR` (monkeypatch + restore) so persistence and the
automations file don't touch real state.

- `_dashboard_content_signature`:
  - Same content → same hash.
  - Adding a `generatedAt` field that changes does NOT change the hash.
  - Re-ordering `jobs`/`alerts` does NOT change the hash (sorted).
  - Adding a new failed job changes the hash.
- `handle_alert_automation`:
  - `action=list` returns `[]` when nothing scheduled for the session; returns
    each row created via `start`.
  - `start` with no `automationId` mints a unique `f"{session_id}:<hex>"` id; a
    second `start` with no id creates a SECOND row with a different id (same
    session, same type) — proves the cap is lifted.
  - `start` with `automationId` of an existing row updates in place (id stays).
  - `stop` with `automationId` removes that row only and leaves siblings intact.

Manual smoke: open the modal twice, schedule two daily reports for different
recipients at different `report_time`s, confirm both rows appear in the list,
Edit one and re-save, Delete the other, confirm `data/automations.json` reflects
the two then one row. For dedup: run the scheduler against an unchanged
dashboard twice; second run logs `Skipped ... no change since last successful
report` and does not send.

## Version

Bump `APP_VERSION` `"2.3.2"` → `"2.4.0"`. `CHANGELOG.md` gets an `[2.4.0]` entry
under **Added** (list action + multi-schedule UI) and **Fixed** (daily-report
content dedup).

## Notes

- Scope strictly limited to email-automation code. No changes to do_GET/do_POST
  routing, validate_payload, scheduler loop body, persistence file format,
  modal JS unrelated to email, or other handlers.
- OneDrive working tree carries a stray local commit
  `fc9d51e (feat(email)... 2.2.0)` on top of an unrelated `0b58e2b` RUCKUS
  baseline. It was never pushed. Optional cleanup:
  `git -C "<onedrive-path>" reset --hard 0b58e2b`. Not part of this change.
