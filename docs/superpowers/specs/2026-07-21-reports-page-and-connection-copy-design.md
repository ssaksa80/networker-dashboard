# Scheduled Reports page + reporting connection by copy

**Date:** 2026-07-21
**Status:** Approved (design)
**Fixes:** empty scheduled reports (all zeros) on 2.11.x; scattered/hard-to-find reporting settings.

## Problem

**1. Reports arrive empty.** A delivered report showed the correct server but every metric `0`, health `Unavailable`, "Connected – action required" — while the live dashboard showed thousands of jobs for the same window.

Root cause: the reporting connection is hand-entered with only host / port / username / password. `report_cred.credential_to_apiconfig` then **guesses** the rest of the `ApiConfig`: `use_authc_header=False`, `verify_tls=False`, `api_version="auto"`, `backup_server_host/port` copied from the REST host/port, `use_wmi_health=False`. A real working connection may differ on any of these. The result authenticates but queries return nothing — a silent, empty report.

**2. Settings are hard to find and split up.** Reporting settings live across three drawer panels (Scheduled Reports, Email (SMTP), TV / Display), and the connection the reports depend on is labelled "Display connection" in a different panel from the reports that use it.

## Goals

1. A scheduled report renders exactly what the dashboard renders — no guessed connection fields.
2. Setting up reporting takes one click, not a form full of fields to get wrong.
3. All reporting settings in one place.
4. A connection that authenticates but returns no data is caught **at setup**, not by an empty email.

## Decisions (locked)

- The reporting connection is established by **copying the current dashboard connection** (not hand-entered).
- A dedicated **`/reports` page** owns reporting connection + SMTP + report groups. Drawer entries become launchers.
- `/reports` is **auth-gated exactly like `/`** (dashboard password). It is NOT token-public like `/tv/<token>`.
- Setup validation reports **what the connection actually returned** (row counts), flagging a zero-data connection.

## Architecture

### 1. Connection as a full session snapshot

The app already produces a proven, complete connection record: `sessions._session_to_dict(session_id, session)` — every `ApiConfig` field plus `encrypted_networker_password` / `encrypted_wmi_password`, nested as:

```
{ session_id, created_at, last_used, encrypted_networker_password, encrypted_wmi_password,
  config: { rest_api_host, rest_api_port, backup_server_host, backup_server_port, username,
            api_mode, api_version, report_range, custom_start_date, custom_end_date,
            use_wmi_health, wmi_username, timeout_seconds, verify_tls, use_authc_header } }
```

New helper `sessions.snapshot_latest_live_session() -> dict | None`: returns `_session_to_dict(...)` for the **most recently used live session** (max `last_used`), or `None` when there is no live session. Server-side selection means the `/reports` page works in a new tab without the originating tab's in-memory session id.

The reporting connection store (`display.save_connection`/`load_connection`, machine-DPAPI) persists this snapshot as-is.

### 2. Reading a stored connection (accepts both shapes)

Because the stored shape is now the nested snapshot, `report_cred` gains:

```
apiconfig_from_stored(stored: dict) -> ApiConfig
```

- If `stored` has a `config` dict → build from `stored["config"]` + `decrypt_credential_password`/`decrypt_process_secret` of `stored["encrypted_networker_password"]`, carrying **every** field verbatim (no guessing).
- Else → fall back to the legacy flat shape via the existing `credential_to_apiconfig` (keeps older stored connections working until re-copied).

`report_render.render_window` uses `apiconfig_from_stored` instead of `credential_to_apiconfig`, then applies the cadence window with `dataclasses.replace`.

Note on password sealing: session snapshots carry `encrypted_networker_password` sealed by the app's process/session cipher (`secrets.encrypt_process_secret`), not `report_cred`'s DPAPI token. `apiconfig_from_stored` must decrypt with the same scheme the snapshot used — use `secrets.decrypt_process_secret` for snapshot-shaped records and `report_cred.decrypt_credential_password` for legacy flat records.

### 3. Setup + validation

New actions on `/api/report-groups` (one endpoint for the page):

- `connection-status` → `{hasConnection, host, username, apiMode, lastValidated, lastRowCount}` (never the password).
- `use-current-connection` → `snapshot_latest_live_session()`; if `None`, return a clear message: *"No live dashboard connection. Connect on the dashboard first, then click this."* Otherwise save the snapshot, then **validate**.
- `validate-connection` → render the current daily window and report **row counts** back: `{ok, jobs, alerts, message}` where `message` is e.g. *"Validated — 2,245 jobs in the last 24h."* If the render succeeds but `totalJobs == 0`, return `ok:true, zeroData:true` with *"Connected, but returned 0 jobs — reports would be empty. Check the account's permissions or the backup server."* This is the guard against a silently empty report.

Validation results (`lastValidated`, `lastRowCount`) are stored alongside the connection for display.

### 4. `/reports` page

- `nwdash/assets/reports.html` + `reports.js` (+ reuse `app.css`), rendered by `ui.reports_page_html()` following the existing `tv_page_html()`/`dashboard_html()` pattern.
- Route `GET /reports` in `server.py`, placed **after** the auth gate (same treatment as `/`): unauthenticated → login page.
- Page sections:
  1. **Reporting connection** — status line (host / user / api mode / last validated / last row count), **"Use my current dashboard connection"** button, **"Re-validate"** button, inline result banner.
  2. **Email (SMTP)** — host, port, security, username, password, from, ops-alert address (moved from the drawer; same `GET`/`POST /api/email-config`).
  3. **Report groups** — the full manager (list with health, section checkboxes, recipients, cadence + send time, enabled, Send now + test, Edit, Delete, reorder).

### 5. Drawer becomes launchers

- Account → **Scheduled Reports** and **Email (SMTP)** call `window.open("/reports", "_blank")`; their drawer panels are removed from `dashboard.html`/`app.js`.
- Account → **TV / Display** keeps the TV token UI only. Its connection form is removed; it shows the shared reporting connection read-only with a link to `/reports`.

## Data flow

```
Operator (already connected on the dashboard)
  → /reports → "Use my current dashboard connection"
  → server: snapshot_latest_live_session() → display.save_connection(snapshot)
  → validate: render_window(stored, daily window) → row counts
  → banner: "Validated — N jobs in the last 24h"  (or zero-data warning)

Scheduled fire / Send now
  → apiconfig_from_stored(stored)  [full fidelity, nothing guessed]
  → build_dashboard(cfg with cadence window) → section-filtered email
```

## Error handling

- No live session → explicit instruction, no save.
- Render fails → surface the actual error; connection not marked validated.
- Renders but zero rows → saved, flagged `zeroData` with guidance (permissions / backup server), so it is visible before the first scheduled send.
- Group fire keeps the 2.11.1 guard: a missing/unusable connection produces the operator-facing message + ops alert, never a raw socket error.

## Testing

- `snapshot_latest_live_session`: picks max `last_used`; returns `None` with no sessions.
- `apiconfig_from_stored`: snapshot shape carries **every** field verbatim (assert `use_authc_header`, `verify_tls`, `api_version`, `backup_server_host/port` survive — the exact fields that were being guessed); legacy flat shape still works; password decrypts by the right scheme per shape.
- `use-current-connection`: no session → clear message, nothing saved; with session → saved + validated.
- `validate-connection`: ok with counts; `zeroData` flagged when `totalJobs == 0`; render failure surfaces the error.
- `/reports` route: authed (401/login when not authenticated), 200 + page when authenticated.
- Asset tests: `reports.html` contains the three sections; `reports.js` wires `/api/report-groups` + `/api/email-config`; drawer buttons open `/reports`; old drawer panels gone.
- Regression: a group fire uses `apiconfig_from_stored` (not the guessing path).

## File layout

- Modify `nwdash/sessions.py` — `snapshot_latest_live_session()`.
- Modify `nwdash/report_cred.py` — `apiconfig_from_stored()` (both shapes).
- Modify `nwdash/report_render.py` — use `apiconfig_from_stored`.
- Modify `nwdash/report_groups_api.py` — `connection-status`, `use-current-connection`, `validate-connection`.
- Create `nwdash/assets/reports.html`, `nwdash/assets/reports.js`; modify `nwdash/ui.py` (`reports_page_html`), `nwdash/server.py` (`GET /reports`).
- Modify `nwdash/assets/dashboard.html` / `app.js` — drawer buttons become launchers; remove the Scheduled Reports + SMTP panels; trim the TV/Display connection form.
- `deploy/build-bundle.ps1` — allow-list `reports.html`, `reports.js`.

## Rollout

Ships as **2.12.0**. Post-deploy: connect on the dashboard as usual → Account → Scheduled Reports (opens `/reports`) → **Use my current dashboard connection** → confirm the "Validated — N jobs" banner → set SMTP → enable groups → **Send now + test** to confirm delivery.
