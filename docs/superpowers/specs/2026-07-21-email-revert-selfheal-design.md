# Revert email to the original v2.8.3 engine + self-healing connection

**Date:** 2026-07-21
**Status:** Approved (design)
**Supersedes:** the 2.9.0–2.12.0 email/reporting stack (Scheduled Reports jobs, Report Groups, /reports page, TV display token, display connection, config drawer panels).

## Problem

Four successive redesigns of the email notification system (2.9.0 ReportJob, 2.10.x settings surfaces, 2.11.x Report Groups, 2.12.0 /reports page) each introduced new failure modes; the operator reports the email notification is now not working at all and has lost confidence in the redesigned stack. The original v2.8.3 system (Email Alert Automation modal + `AlertAutomation` engine) worked in production for months and had exactly **one** structural flaw: a schedule whose dashboard session disappeared (restart/TTL) with an empty connection snapshot went silently inert — "waiting for a connection" — and never sent again.

## Decision (locked with the user)

1. **Full revert of all application code to the v2.8.3 state** (`adc177f`): the complete original email engine and modal return; everything added since — ReportJob/Report Groups engines, `/reports` page, SMTP settings panel, config side drawer, Account-menu config buttons, TV display token + display connection — is removed.
2. **Enhance the original engine with exactly two things:**
   - **Self-healing connection** — a schedule can never silently die for lack of a session.
   - **Status clarity** — each saved schedule shows a clear live/reconnectable/waiting status plus its last send result.

## What the revert restores (v2.8.3, verbatim)

- `nwdash/emailer.py` — full `AlertAutomation` engine: daily_report + alert schedule types, saved schedules, named profiles with ON/OFF toggles, quiet hours, digest, identity-replace dedup, duplicate-send guard, config reconcile, SMTP diagnostics, Send test, persistence to `data/automations.json`.
- `nwdash/sessions.py` — including `connection_snapshot_for_session` / `recreate_session_from_snapshot`.
- UI — topbar **Email** button + the Email Alert Automation modal (includes the popup resize/backdrop-dismiss fix, which predates 2.8.3's tag).
- Wiring — `main.py` (restore automations + `automation_scheduler_loop`), `server.py` (`/api/alert-automation`, GET `/api/email-config`).
- The full v2.8.3 test suite (incl. the four email E2E suites deleted in 2.9.0).
- Plain `/tv` TV mode (auth-gated; predates 2.8.3) — the DSO TV points at `/tv` instead of the removed `/tv/<token>`. With no dashboard password set, the TV renders without login exactly as it did originally.

## What is removed

All app code introduced after `adc177f`: `report_cred.py`, `report_render.py`, `report_notify.py`, `report_jobs`-era leftovers, `report_window.py`, `report_groups.py`, `report_groups_api.py`, `display.py`, `reports.html/js`, the `/reports`, `/tv/<token>`, `/api/display/*`, `/api/display-config`, `/api/report-groups` routes, the config drawer + its Account-menu buttons, the SMTP settings panel, and all their tests. `deploy/build-bundle.ps1` `$shipFiles` reverts to the v2.8.3 file list. Orphaned data files on the host (`report_groups.json`, `display_*.json`, `report_cache/`) are ignored (harmless; not part of the allow-listed app).

**Mechanism:** `git checkout v2.8.3 -- nwdash/ tests/` plus the build-bundle `$shipFiles` revert, then the enhancement commits on top. The deploy machinery itself (install.ps1/common.ps1/launcher) is unchanged since `adc177f`, so it stays as-is.

## Enhancement A — self-healing connection

New helper in `sessions.py`:

```
latest_session_record() -> dict | None
```
Returns the newest usable connection record: the most-recently-used **live** session (via the existing `_session_to_dict`), else the newest record in `sessions.json` on disk (by `last_used`), else `None`.

Two integration points inside the original engine (minimal diffs):

1. **Arm time** (`handle_alert_automation` action=start and `_arm_profile_automation`): when the connection snapshot resolves empty (no live session, no stored/profile/inherited snapshot), adopt `latest_session_record()` as the automation's connection before persisting. The schedule is born reconnectable.
2. **Fire time** (`_ensure_automation_session`): where the engine today gives up (session gone + snapshot empty, or snapshot credentials undecryptable), first try `latest_session_record()`: adopt it onto the automation, `persist_automations()`, attempt `recreate_session_from_snapshot`. Only if that also fails does the run skip — with the existing "waiting" message. Every adoption is logged.

Result: a restart can no longer strand schedules — they adopt whatever proven session exists (live or persisted) and keep sending. The operator's two production schedules (still present in `automations.json` with empty snapshots) heal automatically on the first tick after a session exists.

## Enhancement B — status clarity

In the modal's saved-schedules list (existing `refreshEmailScheduleList` rendering), each schedule row gains an explicit status chip + its last result:

- **live** — its dashboard session currently exists.
- **reconnectable** — session gone but a usable connection snapshot is stored (will self-heal at fire time).
- **waiting** — no session and no snapshot anywhere (only possible when the server has never had a session).

Data comes from fields the `list` action already returns (`sessionLive`, `reconnectable`, `lastResult`) — plus `reconnectable` now reflects post-heal reality. Styling reuses existing badge classes. No new endpoints.

## Error handling

Unchanged from the original engine (its messages were already operator-facing). Self-heal failures fall back to the original "waiting for a dashboard session" result — but now only when there is genuinely no session anywhere.

## Testing

- The restored v2.8.3 suite must pass as-is (its four email E2E suites cover schedules, profiles, dedup, reconcile, persistence).
- New: `latest_session_record` (live beats disk; newest by last_used; None when empty) · arm-with-no-session adopts a persisted record (no more `connection: {}` schedules) · fire-with-empty-snapshot adopts + recreates + proceeds · fire with nothing available still skips gracefully · undecryptable snapshot falls back to adoption · asset test for the status chip.
- Known flaky: `test_profile_toggle` E2E (restart/port race) — pre-existing at 2.8.3, not a blocker.

## Rollout

**2.13.0** via `Setup-NWDash.cmd -Upgrade` + one hard refresh. Release notes must state: TV now uses plain `/tv` (the `/tv/<token>` URL stops working; update the DSO TV bookmark), the `/reports` page is gone, and email is managed in the **Email** modal again. Existing `automations.json` schedules reload and self-heal once any dashboard session exists (connect once in the browser after upgrading). SMTP config in `email_config.json` is read by the restored engine unchanged.
