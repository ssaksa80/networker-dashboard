# Project State

Snapshot of the hardening effort completed on 2026-07-18. Read this
before making changes — it records why things are shaped the way they
are and what was deliberately deferred.

## History (one session, 7 commits)

| Commit | Change |
|---|---|
| `9cd6f40` | Initial import; `.gitignore` fences `data/`, `logs/`, `.certs/`, `debug_test/` (live keys and session state — never commit) |
| `87dab91` | CSRF protection + fail-closed TLS verification |
| `7d0b028` | Monolith split into the `nwdash/` package; 27-test smoke suite |
| `73630ed` | UI extracted from Python string literals to `nwdash/assets/` |
| `55003e9` | README, pyproject, GitHub Actions CI |
| `29ba8a5` | Swallowed exceptions logged; DPAPI key-regeneration warning |

Result: overall report-card grade moved from B- to A-. All rendered
pages were verified byte-identical across the split and the asset
extraction (SHA-256 against the prior revision).

## Architecture rules

- `networker_dashboard.py` is a thin launcher; all code lives in
  `nwdash/`. Keep it that way.
- **Mutable runtime flags** (`AUTH_ENABLED`, `APP_DEBUG`,
  `MAX_CONNECTIONS`, `ALLOWLIST_ENABLED`, …) are owned by
  `nwdash/config.py` and are mutated by `run()` after argument parsing.
  Always access them as module attributes (`from . import config as
  _cfg; _cfg.AUTH_ENABLED`), never `from .config import AUTH_ENABLED` —
  a from-import freezes the value at import time and silently ignores
  runtime changes.
- **UI pages are real files** in `nwdash/assets/` (`dashboard.html` +
  `app.css` + `app.js`, `login.html`, `view.html`), reassembled at
  import via marker replacement (`__APP_CSS__`, `__APP_JS__`,
  `__SHARE_TOKEN__`, `__NETWORKER_LOGO_SRC__`). Edit the assets, not
  Python strings. `.gitattributes` pins them to LF; the loader uses
  universal-newline decoding so served bytes are checkout-invariant.
- Five deliberate function-level imports break circular module edges;
  each is commented at the import site. Don't "clean them up" into
  top-level imports. (The sessions→emailer edge was removed when session
  cleanup stopped cancelling automations.)

## Security model

- Login: PBKDF2-SHA256 password hash, rate-limited, HMAC-signed
  HttpOnly session cookie (`SameSite=Strict; Secure`).
- CSRF: stateless synchronizer token = HMAC(auth key, session-cookie
  payload). Issued in the login response and via `GET /api/csrf`; the
  fetch wrapper in `app.js` injects `X-CSRF-Token` on every non-GET
  `/api` call and re-bootstraps once on 403. `do_POST` enforces it for
  all state-changing routes; `/api/login` and `/api/logout` are exempt.
- Outbound TLS to NetWorker servers verifies certificates by default,
  controlled per connection profile; restored sessions missing the
  flag default to verify (fail closed). The startup self-test pins the
  server's own certificate instead of disabling verification.

## Designs, sidebar and admin console

- **Four designs** (`body[data-design]`): Daylight (the 15 themes, as
  before), Aurora Flow, Command Deck and DEDB Navy. `assets/design.css`
  holds their palettes (they override the theme variables app.css uses) and
  the sidebar, hero and motion styles; `assets/design.js` switches them. A
  viewer's choice lives in their browser; the admin console sets the default.
  No library and no CDN: the "glow flow" is CSS (`@property --g` keyframes,
  background-position beams, SVG `animateMotion`) plus a small tween.
  Reduced-motion systems always get motion off.
- **Sidebar**: docked, collapsed to icons, or floating (drag by the header,
  double-click to park, minimise to a button); on narrow screens it slides
  over the page. The connection form and the old account-menu buttons moved
  into it with their ids unchanged, so app.js still binds them.
- **Protection-flow hero**: drawn from the dashboard payload only
  (`summary` counts and `tables.jobs`), via `NWDesign.onDashboard(data)`
  called at the end of app.js `renderDashboard`.
- **Admin console** at `/admin` (`assets/admin.*`, `nwdash/admin.py`),
  modelled on DEDB's: title bar, tab strip, 12 sections, audit log. DEDB gates
  on an admin role; here the gate is signed in **and** the password typed
  again within 15 minutes (the "admin window", kept in memory per login and
  extended by admin activity: every change, and GETs the console marks
  `X-NWDash-Activity: 1`; timed refreshes leave it off so an unattended
  console still locks). With no password set, `/admin` opens only to a
  browser on the server itself: a loopback peer with no proxy-forwarding
  header (a local reverse proxy also connects from loopback), plus the
  one-time setup code `run()` writes to `data/setup-code.txt` at every start
  (nginx's default `proxy_pass` adds no forwarding header, so the code is
  what proves file access). It then offers to set the first password.
  Every `/api/admin/*` route re-checks this; POSTs also keep the global JSON,
  same-origin and CSRF rules. Sections that edit existing data
  (connections, email, schedules, snapshots) call the same routes the
  dashboard uses, which stay as open to a signed-in user as before; those
  routes now write audit entries too.

## Verification

```
python -m unittest discover -s tests -v   # 27 tests, must pass
ruff check .                              # must be clean
```

CI (`.github/workflows/tests.yml`) runs both on windows-latest /
Python 3.14 for every push and PR. Run the suite locally before any
release claim — `py_compile` or a clean boot is not evidence.

## Persistence guarantees

Every config/state store lives under `data/`, which the installer
(`scripts/Setup-NWDash.cmd`) preserves on upgrade (only non-`data\`,
non-`logs\`, non-`.certs\` files are replaced). What each file holds and
how it comes back after a restart/update/crash:

| File | Contents | Restore path |
|---|---|---|
| `data/automations.json` | Scheduled email alert/report automations, incl. each schedule's encrypted connection snapshot | `restore_automations_from_disk()` at boot — **unconditional** (no session required); the scheduler loop re-arms them; sessions are recreated lazily at fire time from the snapshot |
| `data/email_profiles.json` | Named email notification profiles (SMTP + schedule presets; password encrypted, API always masks it) | Read on demand by the `/api/alert-automation` profile actions |
| `data/email_config.json` | Shared SMTP transport + per-type recipients | Read on demand (`load_email_config`) |
| `data/sessions.json` | Encrypted NetWorker dashboard sessions | `restore_sessions_from_disk()` at boot (re-login; best effort) |
| `data/profiles.json` | Connection profiles (encrypted passwords) | Read on demand (`load_profiles`) |
| `data/ui_prefs.json` | Dashboard theme, plus the default design, motion level and sidebar mode set in the admin console | Read on demand (`load_ui_theme`, `load_ui_prefs`); saves merge keys |
| `data/auth.json` | Gateway password hash | Read at boot by `auth.py` |
| `data/auth_sessions.json` | Live gateway login session ids, their expiry, and when/where each signed in; logout removes one | Loaded by `auth.py` on first cookie check; expired ids are pruned. Files from before the metadata (bare expiry numbers) still load |
| `data/audit.jsonl` (+ `.1`) | Admin console audit log: sign-ins, failed sign-ins and every change, with old/new values (never passwords) | Appended by `admin.audit()`; rotated once at 2 MB |
| `data/allowed_hosts.json` | NetWorker host allow-list saved from the admin console | Read at boot only when neither `--allowed-hosts` nor `DASHBOARD_ALLOWED_HOSTS` is set (those win, and make the list read-only in the console) |
| `data/networker_snapshots.json`, `data/auto_snapshot_config.json` | Local snapshots + auto-save flag | Read on demand |
| `data/last_good_dashboard.json` | Last successful dashboard (stale fallback) | Read on demand |
| `data/.session_key`, `data/.auth_key` | DPAPI-wrapped encryption/HMAC keys | Loaded at import in `secrets.py` |

Email automations are deliberately **decoupled from dashboard sessions**
(v2.5.x fix for "schedules vanish after restart"): a missing session
never cancels a schedule. At fire time the automation reuses the live
session or recreates one from its stored connection snapshot; on failure
the run is skipped and retried — never auto-cancelled. Legacy
`automations.json` records without a snapshot stay scheduled and wait
for a matching session. If the encryption key was regenerated (DPAPI),
the schedule stays armed but inert, with one loud log warning, until
re-saved.

## Operational gotchas

- `data/.session_key` and `data/.auth_key` are DPAPI-protected per
  Windows account. Copies to another machine/account are unreadable;
  the app warns at startup and regenerates, which invalidates saved
  sessions and profile passwords (re-enter once).
- The dev TLS certificate in `.certs/` is self-signed; browsers warn
  once. Provide `--cert`/`--key` for production.
- Headless-browser screenshot rendering (email reports) needs a local
  Chrome or Edge; on some managed hosts Edge silently refuses
  `--screenshot` while Chrome works.

## Deferred work (in priority order)

1. **Split the large collector functions** — `build_dashboard_nwui`
   (~349 lines), `handle_alert_automation`, `dashboard_report_email`.
   Deliberately not attempted: they are only exercisable against a
   real NetWorker server, and refactoring them blind risks silent
   breakage behind fail-soft guards. Do this when a test server is
   reachable, with live-data validation before and after.
2. Live-data validation of REST/NWUI/WMI collectors (same dependency).
3. Nice-to-haves: release tagging + offline bundle, CSP tightening
   pass, ubuntu CI leg, narrowing the ~23 broad `except Exception`
   sites.
