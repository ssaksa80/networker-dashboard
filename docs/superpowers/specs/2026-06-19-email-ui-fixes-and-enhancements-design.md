# NetWorker Dashboard — Email UI Fixes + Notification Enhancements

Date: 2026-06-19
Status: Draft
Target file: `networker_dashboard.py` (single-file by design)
Tests: `test_phase5.py` (stdlib `unittest`)
Base: v2.4.2 (interval/time gray-out CSS fix already applied)

## Goal

Three things, in one coherent pass over the email-automation code only:

1. **Bug A — Account dropdown button invisible under the default theme.**
2. **Bug B — Saved schedules stack inside the modal.** Collapse them behind a
   dropdown and add a per-schedule **pause/resume checkbox** (config kept).
3. **Enhancements** — quiet hours, digest mode, per-schedule severity.

Out of scope: routing, auth, SSRF, logging, snapshot code, scheduler tick
cadence, persistence file *location*. The interval/report-time gray-out (Bug C)
is already fixed in v2.4.2 (`input:disabled` / `label.is-disabled` CSS +
existing `syncEmailTypeFields`).

## Bug A — Account button contrast

### Cause
`.collapse-toggle` (CSS ~line 1133) sets `background: var(--surface2)` (a typo —
the real variable is `--surface-2`, so it resolves to transparent) and
`color: var(--ink)`. The `.topbar` is always dark (`#102832`), but the default
theme's `--ink` is near-black `#172026` → dark text on a dark bar = invisible.
Other themes happen to define a light `--ink`, so they render.

### Fix
Scope an explicit, theme-independent style for the toggle when it sits in the
topbar, matching the existing `.status-pill` treatment:

```css
.topbar .collapse-toggle {
  background: rgba(255, 255, 255, 0.10);
  color: #ffffff;
  border-color: rgba(255, 255, 255, 0.22);
}
.topbar .collapse-toggle:hover { border-color: rgba(255, 255, 255, 0.5); }
```

Also correct the `var(--surface2)` → `var(--surface-2)` typo on the base
`.collapse-toggle` rule so the non-topbar toggles (view controls, snapshot
controls) render with their intended surface background.

## Bug B — Schedule list → dropdown + pause checkbox

### UI
- Wrap `#emailScheduleList` in a collapse toggle: **"Saved schedules (N)"** using
  the existing `.collapse-toggle` / `.collapsible` pattern. Collapsed by default;
  N updates on every refresh. Eliminates stacking in the modal view.
- Each rendered row gains a **checkbox** at the left = enabled/paused state.
  Toggling it posts `action=set_enabled`. Edit / Delete buttons stay.
- A paused row renders with `.is-disabled` styling and its meta shows `(paused)`.

### Backend
- `AlertAutomation` (dataclass ~line 5790): add `enabled: bool = True`.
- New handler action **`set_enabled`**: payload `{automationId, enabled}` for a
  row whose `session_id == session_id`; sets the flag, `persist_automations()`,
  returns `{ok, automationId, enabled}`.
- `automation_scheduler_loop`: skip rows where `enabled is False` (do not fire,
  do not advance `last_run`; still reschedule `next_run_at` so resume is prompt).
- `action=list` row dict gains `"enabled"`.
- Persistence: `enabled` written/read in the existing `automations.json` schema
  (default `True` for legacy rows that lack the key).

## Enhancements

### 1. Quiet hours (alert schedules only)
- Per-schedule optional `quiet_start` / `quiet_end` (`"HH:MM"`, empty = off).
- Daily reports ignore quiet hours (they fire at a fixed time by design).
- In the alert branch of `run_alert_automation`: if `now` falls inside the quiet
  window, skip the send, set `last_result = "Quiet hours: suppressed at <ts>"`,
  advance `next_run_at` as normal. Window may wrap midnight (start > end).
- UI: two `HH:MM` inputs in the modal, grayed out when type = daily report.

### 2. Digest mode (alert schedules only)
- Per-schedule boolean `digest` (default off).
- The implementation plan first confirms how the alert branch currently composes
  its email (one message listing all matching alerts vs. one per alert).
  - If it already sends one batched email per interval: `digest=on` keeps that;
    `digest=off` changes the subject to name the single highest-severity alert
    (`"CRITICAL: <message>"`) for faster triage, body unchanged.
  - If it currently sends per-alert: `digest=on` collapses them into one
    combined email per interval (subject `"N alerts (<range>)"`), `digest=off`
    is the existing behaviour.
- No new timer; digest only affects how the per-interval send is composed.
- UI: a checkbox; grayed out for daily report.

### 3. Per-schedule severity threshold
- `trigger` already exists (`critical` / `warning` / …). Confirm the alert-match
  code honours it per schedule; if it currently hard-codes a level, wire it to
  `automation.trigger`. Expose existing `alertTrigger` select per schedule (it is
  already in the form and persisted) — no new field, just ensure `list`/edit
  round-trips it and the match path reads it.

## Persistence & migration
All new fields (`enabled`, `quiet_start`, `quiet_end`, `digest`) get safe
defaults when absent, so existing `automations.json` rows load unchanged.

## Tests (`test_phase5.py`)
- `set_enabled` flips a row's flag; scheduler skips a disabled row; re-enable
  fires again.
- Quiet-hours window (including midnight-wrap) suppresses an alert send and sets
  the quiet `last_result`; outside window sends.
- Legacy automation dict without the new keys loads with defaults.
- `list` returns `enabled`, `quietStart`, `quietEnd`, `digest`, `trigger`.

## Version
Bump `APP_VERSION` `"2.4.2"` → `"2.5.0"` (new features). CHANGELOG `[2.5.0]`:
Fixed (Account button contrast), Added (schedule dropdown + pause, quiet hours,
digest, per-schedule severity round-trip).

## Notes
- Scope strictly the email-automation code + the one topbar CSS rule. No changes
  to unrelated handlers, the scheduler cadence, or the persistence file format
  beyond additive keys.
