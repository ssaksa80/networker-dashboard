# Scheduled Report Groups

**Date:** 2026-07-21
**Status:** Approved (design)
**Supersedes:** the `ReportJob` (digest/alert) model from 2.9.0 and `/api/report-jobs`.

## Problem / goal

Operators need to send different scheduled report emails to different audiences: each audience wants a specific subset of dashboard sections, its own recipients, and its own cadence (daily / weekly / monthly). Today's Scheduled Reports has one flat job type (daily digest OR threshold alert), one credential per job, and no grouping, ordering, section selection, weekly/monthly cadence, or on-demand send.

This feature introduces **report groups**: named, ordered, individually toggleable bundles of *(selected sections + recipients + cadence)*, all pulling live data through one shared reporting connection, with on-demand and test send.

## Decisions (locked)

- A "report" you select = a **dashboard section/card** (Backup SLA, Management Overview, Recovery Health, Clone Jobs, Alerts, Server Protection, Health).
- Groups use **one shared reporting connection** — the existing display connection (`nwdash/display.py`, machine-DPAPI). No per-group credential.
- **Groups replace** the old digest+alert `ReportJob` model entirely. Threshold "alert" emails are removed.
- Cadence is **retrospective, fixed send**: daily (prev 24h), weekly (Sunday, prev Sun–Sat), monthly (1st, prev calendar month); each group picks its send time.

## Sections (selectable)

Stable keys → email block:

| key | section |
|---|---|
| `backup_sla` | Backup SLA |
| `management` | Management Overview |
| `recovery` | Recovery Health |
| `clone` | Clone Jobs |
| `alerts` | Alerts |
| `server_protection` | Server Protection Job |
| `health` | Storage / Health |

A group stores a subset of these keys; the email renders only those blocks (+ a PNG snapshot of the same).

## Data model

New record `ReportGroup`, persisted ordered in `data/report_groups.json`:

```
ReportGroup:
  id: str
  name: str
  position: int              # display + fire order (0-based, contiguous)
  enabled: bool
  sections: [str]            # subset of the section keys above (>=1)
  recipients: [str]          # >=1
  cadence: "daily"|"weekly"|"monthly"
  send_time: "HH:MM"         # local; send DAY fixed by cadence
  health:
    last_run, last_success, next_run: float
    last_result: str
    state: "healthy"|"unhealthy"|"never_run"
```

- **Reporting connection**: reuse `nwdash/display.py` `save_connection`/`load_connection` (rename UI label to "Reporting connection"; it already backs the TV feed). One credential, machine-DPAPI at rest, validated on save.
- **SMTP + ops address**: existing `email_config.json` (2.10.1 SMTP settings UI), read via `_smtp_config`.

## Cadence + window (computed at fire time)

`next_run` and the report window are computed from cadence + `send_time`, in local time:

- **daily**: next occurrence of `send_time` today/tomorrow. Window = `now-24h → now` (report_range `24h`).
- **weekly**: next **Sunday** at `send_time`. Window = the **previous** Sunday 00:00 → Saturday 23:59:59 (custom range).
- **monthly**: next **1st** of a month at `send_time`. Window = the **previous** calendar month, 1st 00:00 → last day 23:59:59 (custom range).

Windows drive `build_dashboard` via `report_range` (daily) or `custom_start_date`/`custom_end_date` (weekly/monthly) on the reporting connection's `ApiConfig`. After a fire, `next_run` advances to the next occurrence.

A dedicated module `nwdash/report_window.py` computes `(report_range, custom_start, custom_end)` and `next_run` for a cadence — pure functions, fully unit-tested (Sunday anchoring, month-length/year boundaries, DST-agnostic via local `datetime`).

## Rendering

`nwdash/report_render.py` gains section-aware rendering:
- `render_window(cred, window) -> RenderResult` — connects fresh via the reporting connection with the window applied (real-time).
- The email builder (`nwdash/reports.py`) gets a `sections` parameter: `dashboard_report_email(dashboard, sections=None)` renders only the requested section blocks (None = all, preserving current behavior); the PNG snapshot likewise reflects the selected sections (or is omitted if none of the visual sections are selected).

## Scheduler

Reuse the single background tick loop pattern (proven in 2.9.0):
- Per enabled group, ensure `next_run` is set (compute if 0); when `now >= next_run`, fire in a daemon thread.
- **Fire** = load reporting connection → compute window → `render_window` → email selected sections to recipients → record health (healthy/last_success). On failure: send the last-good cached render with a STALE banner (if present) + an ops alert, mark unhealthy — reuse the isolated-send logic from 2.9.0 so an SMTP failure never suppresses the ops alert.
- Reschedule `next_run` for the next cadence occurrence; persist.
- Session-free (uses the shared reporting connection), so it runs 24/7 with nobody logged in — same guarantee as 2.9.0/2.10.0.

## On-demand + test send

Per group: **Send now** with a **"Send as test"** checkbox.
- `POST /api/report-groups {action:"send", id, test:bool}` renders the group's current window immediately and emails it now, independent of the schedule.
- `test:true` → subject prefixed `[TEST]`, body notes it's a test; `test:false` → a real on-demand send to the group's recipients.
- Same render + section pipeline as a scheduled fire. Returns per-send result (sent / SMTP error) for inline UI feedback.

## API — `/api/report-groups` (authed)

- `list` → groups in `position` order, each with health (never leaks the connection/SMTP secret).
- `create` / `update` → validate (name non-empty, ≥1 section, ≥1 recipient, valid cadence, `HH:MM`), assign/keep `position`, persist. A group only becomes `enabled` if the reporting connection is configured (else saved disabled with a note).
- `delete` → remove + re-pack positions.
- `toggle` → enable/disable.
- `reorder` → set positions from an ordered id list.
- `send` → on-demand (see above).

`/api/report-jobs` is removed (410 shim, mirroring the earlier alert-automation retirement).

## Reporting-connection endpoint

Reuse/extend the existing display-config path: the "Reporting connection" is the display connection. The admin UI sets it once (validated via a live `render_window` probe). `hasConnection` surfaced to the groups UI so it can warn when unset.

## UI (in the config drawer's Scheduled Reports panel)

- **Reporting connection** status + set/validate (shared with TV display).
- **Group list**: ordered cards; each shows name, cadence + send time, recipients count, section chips, health badge, an **on/off toggle**, **Edit**, **Send now** + **Send as test** checkbox, **Delete**, and up/down (reorder).
- **Create/Edit form**: name, section checkboxes, recipients, cadence (daily/weekly/monthly) + send time, enabled. Saving returns to the ordered list with the new/edited group in place.

## Migration

On boot, if legacy `report_jobs.json` exists it is **not** imported (different model) — the groups UI shows a one-time notice to recreate as groups. The `ReportJob` model, `report_api.py` job endpoints, scheduler, and job UI are removed; the SMTP core, `report_cred`, `report_render`, `report_notify`, and `display` connection are kept and reused. The two production jobs are recreated once as groups post-deploy.

## Error handling

- Save validation returns field-level errors (missing name/sections/recipients, bad time, unknown cadence).
- Fire failures: STALE fallback email (if cache) + ops alert, isolated sends, health = unhealthy, structured log — never silent.
- On-demand send returns the actual SMTP result to the UI.

## File layout

- Create `nwdash/report_window.py` — cadence → (window, next_run) pure functions.
- Create `nwdash/report_groups.py` — `ReportGroup` model, ordered store, validator, scheduler, fire logic.
- Create `nwdash/report_groups_api.py` — `/api/report-groups` router (list/create/update/delete/toggle/reorder/send).
- Modify `nwdash/report_render.py` — `render_window`.
- Modify `nwdash/reports.py` — `dashboard_report_email(dashboard, sections=None)` + section-filtered PNG.
- Modify `nwdash/report_notify.py` — send a group's selected-section email + `[TEST]` prefix.
- Modify `nwdash/main.py` — boot restore + group scheduler start; drop the old job scheduler.
- Modify `nwdash/server.py` — route `/api/report-groups`; 410 `/api/report-jobs`.
- Remove `nwdash/report_jobs.py` + its tests.
- Modify assets — Scheduled Reports panel becomes the group manager.
- `deploy/build-bundle.ps1` — allow-list the new modules.

## Testing

- **report_window** (unit, the crux): daily/weekly/monthly `next_run` from a fixed "now" passed in (no wall-clock); weekly anchored to Sunday; monthly window = previous month across month-length and year boundaries; window strings match `build_dashboard` expectations.
- **Group store**: create/persist/restore, ordering (positions contiguous after delete/reorder).
- **Validator**: rejects empty name/sections/recipients, bad time, unknown cadence; enabled requires a configured connection.
- **Section filtering**: `dashboard_report_email(d, sections=[...])` includes only those blocks; `None` = all.
- **Fire**: renders the computed window + emails only selected sections; failure sends STALE fallback + ops alert (isolated sends); **no-session** fire works via the shared connection (regression, mirrors 2.9.0).
- **On-demand**: `send test:true` prefixes `[TEST]`; `test:false` sends real; both independent of schedule.
- **API**: create→list order, update, delete re-packs positions, toggle, reorder, send.
- **E2E (asset/SPA)**: group manager markup + wiring markers.

## Rollout

Additive-schema minor (new `report_groups.json`, removed endpoint) → **2.11.0**. Post-deploy: set the reporting connection (if not already the display connection), recreate the two reports as groups. Ships via `Setup-NWDash.cmd -Upgrade`.
