# DSO TV wall — no-login display token + persistent display feed

**Date:** 2026-07-20
**Status:** Approved (design)
**Depends on:** the 2.9.0 credential model (`nwdash/report_cred.py`, machine-DPAPI) and the existing share-token capability-URL mechanism.

## Problem

The dashboard is shown on a TV in the DSO room, opened by a URL with no keyboard — it must render **without a login prompt**. But the same app holds NetWorker credentials and lets an admin create report jobs, so the admin surface and all mutations **must** be behind a password. Today `AUTH_ENABLED` is all-or-nothing: turning on a password would lock the TV too, and the wall's live feed depends on someone being logged in (the shared-refresh loop pulls from a session — `main.py:240`). With no session it goes stale.

Additionally, the production host currently runs with **no dashboard password at all** (`data/auth.json` absent), so admin + credentials are exposed.

## Goals

1. Enable a dashboard password that protects `/`, admin, and every mutation (including report-job APIs).
2. The TV wall renders live with **no login**, via a revocable capability URL.
3. The wall stays live 24/7 with **nobody logged in**.
4. A leaked TV URL exposes only the read-only wall — never credentials, admin, or mutations.

## Non-goals

- Per-user accounts. The dashboard auth stays a single shared password.
- Changing the wall's visual design (`tv.html`/`tv.js` layout is reused as-is).

## Decisions (locked)

- **TV access:** display-token capability URL `/tv/<token>` (no IP lock for now).
- **Live feed:** a persistent stored **display connection** (NetWorker credential, machine-DPAPI) drives the background shared-refresh via session-free `build_dashboard`.

## Architecture

Three parts, reusing existing infrastructure.

### Part A — Display token + `/tv/<token>` capability route

The app already serves `/view/<token>` (read-only dashboard, pre-auth, `server.py:355`) and `/api/view/<token>` (`_handle_token_dashboard`), both validated by `validate_share_token` and returning the sanitized `shared_dashboard_payload()`. We add a **durable display token** and a TV capability route that reuse this pattern.

- **Display token store** (`nwdash/display.py`): a single long-lived token persisted to `data/display_token.json` (32-hex). Admin-managed: `get_or_create`, `rotate`, `revoke`, `validate`. Independent of any session (unlike share tokens, which expire with a session), because the wall reads the global shared payload, not a per-session one.
- **Route `GET /tv/<token>`** (pre-auth, added next to the `/view/` branch): if `validate_display_token(token)` OR `validate_share_token(token)` passes → serve `tv_page_html(token)`; else a friendly "link expired" page. The bare `/tv` route stays auth-gated exactly as today (admin preview).
- **Route `GET /api/display/<token>`** (pre-auth): returns `shared_dashboard_payload()` when the token is valid; `410 GONE` otherwise. Read-only, sanitized — the same payload shape `/api/view/<token>` returns (no credentials, no session secrets).
- **`tv.js`:** when the page URL carries a token (`/tv/<token>`), fetch from `/api/display/<token>` (+ the existing `/api/ui-theme` which is safe to keep public or mirror token-scoped) instead of the cookie-gated `/api/current-dashboard` / `/api/stream`. Poll on the existing cadence. No token → current behavior (admin preview under auth).

### Part B — Persistent display connection (24/7 live feed)

- **Display connection store** (`nwdash/display.py`): one NetWorker credential sealed with `report_cred.encrypt_credential_password` (machine-DPAPI), persisted to `data/display_connection.json`. `save`, `load`, `clear`.
- **Shared-refresh integration** (`nwdash/models.py` refresh loop, `:473`): when no live interactive session is available to drive the shared refresh, and a display connection is configured, refresh `SHARED_DASHBOARD_STATE` by rendering session-free via `report_render.render(display_cred)` (which calls `build_dashboard`). This keeps the wall live with zero logins and reuses the exact session-free path proven in 2.9.0.
- Never returned by any API; validated (live connect) on save.

### Part C — Auth enablement (operator + admin UI)

- Enabling the password is an operator action (`Setup-NWDash.cmd -Upgrade -AuthPassword '...'`, documented). Once `data/auth.json` exists, `AUTH_ENABLED` is true and the boundary is live.
- **Admin UI — "TV / Display" section** (in the authed dashboard): show/rotate/revoke the display token (renders the full `https://<host>:<port>/tv/<token>` URL to paste into the TV), and set/validate the display connection credential (with health). Both behind auth like all admin.

## Data flow

```
TV browser → GET /tv/<token>            (pre-auth) → validate → tv_page_html
tv.js      → GET /api/display/<token>   (pre-auth) → validate → shared_dashboard_payload()  [read-only]

background shared-refresh loop (no login):
   display_conn = display.load_connection()
   if display_conn and no live session:
       status, dash = report_render.render(display_conn)   # session-free build_dashboard
       set_shared_dashboard("display", dash)               # feeds /api/display + /api/view
```

## Security

- Display token = read-only capability to the sanitized shared payload only. Revocable + rotatable from admin; rotation invalidates the old TV URL immediately.
- Display connection credential encrypted at rest (machine-DPAPI); never serialized to any response.
- `/` + all mutation/admin APIs require the auth cookie once a password is set; only `/tv/<token>`, `/api/display/<token>`, `/view/<token>`, `/api/view/<token>`, and the login endpoint are reachable pre-auth.
- No new plaintext-credential surface; the display feed reuses the 2.9.0 session-free render path.

## Testing

- `/tv/<token>` serves the wall pre-auth for a valid token; expired/invalid → friendly page, not the wall.
- `/api/display/<token>` returns the sanitized payload for a valid token; `410` otherwise; assert the payload never contains credential fields.
- Auth boundary: with `AUTH_ENABLED`, `/` and a sample mutation API return `401` without a cookie, while `/tv/<token>` + `/api/display/<token>` return `200`.
- Display-token store: create/rotate/revoke; rotate invalidates the prior token.
- Display connection: save seals the password (machine-DPAPI), `load` round-trips, save validates via a live connect (mocked), API never echoes the secret.
- Live feed: with no session and a configured display connection, one refresh tick populates `SHARED_DASHBOARD_STATE` from `render(display_cred)` (mocked `build_dashboard`) — the wall stays live.

## File layout

- Create `nwdash/display.py` — display-token store + display-connection store.
- Modify `nwdash/server.py` — `/tv/<token>` + `/api/display/<token>` pre-auth routes; admin token/connection endpoints.
- Modify `nwdash/models.py` — shared-refresh falls back to the display connection when no session drives it.
- Modify `nwdash/assets/tv.js` — token-aware data source.
- Modify `nwdash/assets/dashboard.html` / `app.js` — admin "TV / Display" section.
- `deploy/build-bundle.ps1` — add `nwdash\display.py` to the allow-list.

## Rollout

Ships as a minor version (additive). Post-deploy operator steps: (1) set the dashboard password; (2) in admin, set the display connection credential (validated) and generate the display token; (3) point the DSO TV at `https://<host>:<port>/tv/<token>`. The wall then renders live with no login while admin + credentials sit behind the password.
