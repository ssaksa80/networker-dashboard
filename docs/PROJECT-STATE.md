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
- A projected job's `started` is a `dd-mm-YYYY` display string: order jobs
  by `_started_ts` (`nwui.newest_first`), never by `started`, or "latest"
  becomes the highest day of the month (review C2). The NWUI history merge
  tells jobs-database records apart by their NetWorker job id (`restJobId`,
  the `id` field `restapi.job_history_endpoint` asks for), not by client,
  action and start second, which collapsed save sets started together
  (review C1).
- **The dashboard, admin console and TV pages link their CSS, JS and logo**
  (review F10; the dashboard was 1.4 MB with the logo inlined five times, and
  is now 41 KB). The CSS/JS markers become `/assets/<name>.<hash>.<ext>` URLs,
  the hash being the first 16 hex characters of the SHA-256 of the bytes served,
  so an upgrade or any edited asset is a new URL and the year-long
  `immutable` cache never serves a stale file. `ui.asset_response` answers
  only the exact URLs in the table `ui.py` fills at import; nothing maps a path
  onto a file. The routes need no sign-in: the admin console links them before
  it signs in, and they are the shipped code, the same for everyone (the login
  and share pages link nothing). The admin console's theme CSS stays inline:
  it must come before `design.css`. Email reports keep the logo as a data: URI
  (`networker_logo_src()`), since a mail has no server to fetch it from. The
  logo is read once at import: `assets\networker-logo.png` beside the app if
  present, else the PNG embedded in `ui.py`. Tests read a page through
  `tests/served.py`'s `with_assets()`, which puts the linked files back.
- Five deliberate function-level imports break circular module edges;
  each is commented at the import site. Don't "clean them up" into
  top-level imports. (The sessions→emailer edge was removed when session
  cleanup stopped cancelling automations.)
- **Live push (`/api/stream`)**: every `/api/dashboard` answer goes to every
  viewer, each push carrying its `sessionId`. A page applies only its own
  session's (`isOwnPush` in app.js, review F1); the TV follows whichever is
  shared. A broadcast only queues (`models.SseClient`, at most
  `SSE_QUEUE_FRAMES` behind): the viewer's own handler thread is the only
  writer to its socket, and a viewer that falls that far behind is dropped and
  its connection closed, so its page reconnects (review F3). Never write a
  viewer's socket from anywhere else: a slow viewer would hold up the request
  that broadcast, and two writers tear each other's frames.

## Security model

- Login: PBKDF2-SHA256 password hash, rate-limited, HMAC-signed
  HttpOnly session cookie (`SameSite=Strict; Secure`).
- CSRF: stateless synchronizer token = HMAC(auth key, session-cookie
  payload). Issued in the login response and via `GET /api/csrf`; the
  fetch wrapper in `app.js` injects `X-CSRF-Token` on every non-GET
  `/api` call and re-bootstraps once on 403. `do_POST` enforces it for
  all state-changing routes; `/api/login` and `/api/logout` are exempt.
- Only the gateway's "not signed in" answer is a 401 with
  `X-NWDash-Login: required`, and the fetch wrapper reloads to the sign-in
  page on that header alone; the admin console (`admin.js`) goes back to its
  sign-in card on it too. A wrong password typed into the console (Security's
  current password, the unlock card) is an unmarked 401 whose error is shown.
  An expired dashboard session answers 409
  `session_expired`; a NetWorker server refusing the credentials (its own
  401/403) answers 502 `networker_auth_failed`. Keep it that way, or the
  page hides those errors behind a reload (review F2).
- Outbound TLS to NetWorker servers verifies certificates by default,
  controlled per connection profile; restored sessions missing the
  flag default to verify (fail closed). The startup self-test pins the
  server's own certificate instead of disabling verification.
- **Keys and the folder lock (review S5).** `data/.session_key` and
  `data/.auth_key` are DPAPI **machine-scope** (`secrets.py`): any account on
  the host can unwrap a key file it can read, and no other machine can. The
  protection is therefore the folder, not the wrapping. `Set-NwdashInstallAcl`
  (`deploy/lib/common.ps1`) runs on every install, and on `-Upgrade` and
  `-Rollback` before the service is stopped, so a failure leaves it running.
  `data\`, `.certs\`, `logs\` and anything unknown go to SYSTEM, Administrators
  and the service account (Modify); `backups\` to SYSTEM and Administrators. The
  app itself is read-only for everyone else, the service account included.
  `Restore-NwdashApp` resets what it moves back, because `Move-Item` keeps a
  backup's old open permissions.
  **Machine scope is a decision, not an oversight.** User-scope keys would also
  protect a copy of `data\` left elsewhere on the host, but only the account that
  wrapped a key can read it, so a change of service account needs the old account
  to re-wrap first, and an admin password reset on a local service account loses
  the keys for good. Under "fail closed, never regenerate" either one is a service
  that will not start. The remaining gap is that copy of `data\`, which
  `NWDash-Deployment-SOP.md` §6 covers. A virtual service account is the review's
  other suggestion and is not done: it changes what the service can reach (WMI,
  the headless browser) and needs a real host to prove.
- The lock also tightens the first-password path: on an installed host only an
  administrator can read `data/setup-code.txt`.
- The self-test's **Folder permissions** step (`admin.folder_permissions`, reading
  access lists through `nwdash/fsacl.py`) shows the lock going away. Only the
  installer sets it, so a restored `data\` or a hand-started copy would otherwise
  be open with no sign. It also reads the TLS key and its `*.previous` copy, which
  an upload writes beside the live key: they take that folder's permissions, so
  the lock covers them only while the key is in `.certs\`. A key the account is
  denied raises `KeyMaterialError`
  without the "delete it" advice a dead key gets: access denied says nothing about
  the key, and that advice would cost every secret.

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
- **Certificate upload** (TLS tab, `POST /api/admin/tls`): PEM certificate
  (chain allowed, server certificate first, or found by trying each) and an
  unencrypted key. Before anything is replaced the pair is loaded into a new
  `SSLContext` (the key-matches-certificate check), the dates are checked,
  and a certificate whose subjectAltNames don't cover the Host the console
  was opened with needs an explicit confirm (HSTS is on, so a browser that
  trusted the old certificate gets no click-through). The files the service
  started with (`--cert`/`--key`) are overwritten, the replaced pair kept as
  `*.previous` (`POST /api/admin/tls/restore` swaps them back), and the new
  context goes on `server.ssl_context`, which `get_request` reads per
  connection: new connections get it at once, no restart. The file swap is
  all or nothing (`_swap_in`): a new certificate beside its old key stops the
  service at its next start, so a failure puts the old files back. PFX is not
  accepted (the standard library cannot read PKCS#12); the form shows the
  `openssl` conversion.

## Verification

```
python -m unittest discover -s tests -v   # the whole suite must pass
ruff check .                              # must be clean
```

CI (`.github/workflows/tests.yml`) runs for every push to `main` and
every PR:

- ruff, pinned in the workflow (bump it on purpose, with its fixes);
- the unit suite on windows-latest under Python 3.12.10, the embedded
  runtime the bundle ships, and 3.14 (`test_deploy.py` fails if the
  matrix stops including the build's runtime);
- the installer tests in `deploy/test/` under Windows PowerShell 5.1.

The bundle build also imports every `nwdash` module under its staged
runtime before packing, in a throwaway copy laid out like an install.

Tests are `unittest.TestCase` classes: `discover` does not collect bare
pytest-style functions, so a test written that way never runs. Run the
suite locally before any release claim — `py_compile` or a clean boot is
not evidence.

## Persistence guarantees

Every config/state store lives under `data/`, which the installer
(`scripts/Setup-NWDash.cmd`) preserves on upgrade (only non-`data\`,
non-`logs\`, non-`.certs\` files are replaced; of those three, only the
folder permissions change). What each file holds and how it comes back after
a restart/update/crash:

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
| `data/.session_key`, `data/.auth_key` | DPAPI-wrapped (machine scope) encryption/HMAC keys | Loaded at import in `secrets.py`. An unusable key stops the start; it is never replaced |

Every file above except the audit log, which is appended to, is saved through
`nwdash/fileio.py` (review O5). Each save writes a temporary file of its own,
then `os.replace`s the live file. The replace is retried for about 1.5 s
while another process holds the target, because on Windows antivirus or an open
reader refuses it. A final failure is logged as `could not save <file>` and
raised. Sessions, schedules, email settings, the last good dashboard and gateway
logins carry on from memory when their save fails, so that log line is the only
sign a restart would lose the change. The audit log's rotation and the
certificate swap retry their replaces the same way. Save new files in `data/`
through it too.

`automations.json` is written from the live registry, so `persist_automations`
holds `_AUTOMATIONS_FILE_LOCK` from reading the registry to the replace (review
C19). Without it, two saves could land out of order, and the older one would
bring back a schedule just stopped. Every change to a schedule is saved,
pausing and resuming included. An unreadable file at boot is logged, and no
schedule is restored.

Email automations are deliberately **decoupled from dashboard sessions**
(v2.5.x fix for "schedules vanish after restart"): a missing session
never cancels a schedule. At fire time the automation reuses the live
session or recreates one from its stored connection snapshot; on failure
the run is skipped and retried — never auto-cancelled. Legacy
`automations.json` records without a snapshot stay scheduled and wait
for a matching session. If the encryption key was deleted and replaced
(the app itself never replaces one), the schedule stays armed but inert,
with one loud log warning, until re-saved.

A daily report covers the rolling range stored on its schedule
(`report_range`, `24h` unless set, chosen in the dashboard's Email window
or on the admin Email tab) and is sent once per local date and range. It
never uses, or writes back, the range the dashboard was last viewed with:
`build_dashboard_from_session(..., remember_range=False)`. Interactive
range changes are still remembered on the session, because the shared
view refreshes on the session's range.

A daily report whose run fails (no session, no backup job data, an SMTP
error) is tried again after 5, 15 and 30 minutes, then hourly, until it is
sent or its day ends (review C6). A retry never runs past midnight, where it
would send the next day's report early and under that day's date. Each
schedule's `next_run_at`, `last_run` and `last_result` are saved in
`automations.json`. At boot, `resume_alert_automation` sends a daily report
still owed today at the first scheduler tick: its send time has passed, today's
has not gone out, and the schedule was armed before that time. An alert check
keeps its saved time, and runs at once when that passed while the service was
down.

A send time is found on the naive local wall clock (`datetime.now()`), and
turned into an instant with `.timestamp()`, which uses that day's UTC offset
(review C23, the next-run half). Never schedule from
`datetime.now().astimezone()`: its offset is fixed at the moment it is read,
so the morning after a DST change the report went out an hour early or late.

The scheduler tick starts a schedule's run only when none of that schedule's
is still going (`_claim_run`, released when the run's thread ends; review
C20). A run slower than its interval, such as a large jobs database or a
NetWorker timeout, is never started a second time beside itself.

When live backup job data is missing, a report only accepts a cached
dashboard of its own range (`summary.range`, checked by
`models.dashboard_covers_range` in `stale_dashboard_from_cache` and in the
emailer's own `shared_reliable_dashboard_for_session` fallback). With none,
no email goes out and the schedule's last result says why. The shared
view, interactive builds and alert schedules pass no range, so they still
take any last good dashboard. A test email ignores the dashboard the page
sends if that is another range and builds its own.

When the completed-job history does not load (NWUI mode's merge from the jobs
database, including a saved password that cannot be decrypted, or REST mode's
`/global/jobs`), the collector sets `summary.historyAvailable` to false. That
source is a warning, never `diagnosticOnly`, so health is at best `warning`;
the dashboard, the TV view and the email say the counts are incomplete, and
`summary.slaAvailable` is false, so the SLA shows as N/A rather than 0% (review
C10). The live dashboard still loads. `slaAvailable` is also false when there
was no job to measure.

`/global/jobs` has no server-side time filter, so both modes download the whole
retained jobs database and trim it to the window. Its oldest job, taken before
that trim (and cached with the NWUI history), is how far back a report can see:
`summary.coverage` (`restapi.report_coverage`) holds the window actually
covered, and `short` is true when the oldest job is more than an hour after the
requested start. The email always lists "Data covers"; a short range gets a
"Shorter than requested" banner there, in the snapshot and on the dashboard
(review C13). The periodic-reports design is the real fix for long ranges.

Saving email settings (the admin Email tab, the dialog's Save
configuration) writes `email_config.json`, then runs
`_reconcile_config_driven_automations`, the same pass as a restart, so
armed config-driven schedules follow it at once. That covers both types
because the SMTP server is shared; scheduling from the form runs it too,
for the other type's schedule. A save never starts sending: it arms
nothing, a paused schedule stays paused, the last-sent signature is kept,
and a schedule armed from a profile follows its profile. The next run only
moves if the time or interval changed.

A schedule re-aimed at another server drops its own password
(`snapshots.smtp_target`) and takes the config's, which also replaces its
password after a rotation on the same server. With none saved for the
server, the save's response warns that schedules cannot log in. Sends work
from a copy taken with the password (`_smtp_send_view`), so a save during a
send cannot pair the old password with the new server.

The automatic snapshot (`snapshots._auto_snapshot_once`, every 10 minutes and
when it is switched on) stores yesterday, the last whole calendar day, under
that day's date (review C18). It builds the day itself as a custom range,
midnight to midnight, in the shared session with `remember_range=False`, and
keeps the build only when it is fresh: HTTP 200, `ok`, not a `stale` fallback,
and with backup job data. It never copies the shared dashboard, whose range is
whatever its viewer picked and which can be hours old. The build runs outside
`SNAPSHOTS_LOCK`; the check that decides and the save take the lock once, so a
snapshot saved meanwhile, by hand or by another run, is kept. A snapshot saved
by hand is still the dashboard on screen, under the date of the save.

## Operational gotchas

- `data/.session_key` and `data/.auth_key` are DPAPI-protected per
  machine, not per account: a change of service account needs nothing,
  and a copy on another machine is unreadable. The app then refuses to
  start (`KeyMaterialError`, pinned by `TestKeyMaterialFailsLoud`) rather
  than regenerate, because a new key orphans every saved secret. An
  operator can restore `data/` or delete the key files deliberately.
- On an installed host `data\`, `.certs\` and `logs\` need an elevated
  prompt. `icacls /C` exits 0 even when every file failed, `icacls /T` on a
  file searches subfolders for that name, and `Set-Acl` on an already
  protected item also writes the audit list: `Set-NwdashItemAcl` and
  `Invoke-NwdashIcacls` exist because of those three. `Test-Path` on a locked
  file is fatal under `$ErrorActionPreference = 'Stop'`, so anything that
  runs before elevation uses `Get-NwdashPathState`.
- The dev TLS certificate in `.certs/` is self-signed; browsers warn
  once, and Chrome and Edge do not cache the linked CSS, JS and logo from a
  site whose certificate they don't trust, so every load fetches them. Upload your CA's certificate on the admin console's TLS tab, or
  provide `--cert`/`--key`, for production.
- Headless-browser screenshot rendering (email reports) needs a local
  Chrome or Edge; on some managed hosts Edge silently refuses
  `--screenshot` while Chrome works.
- The installer moves app files with `Move-NwdashItem`, never `Move-Item`.
  Under Windows PowerShell 5.1 `Move-Item` handles a folder with any open
  handle inside by moving file by file, so one locked file splits the folder
  and the next retry nests it; nothing can restore that. Keep the `catch` in
  `-Upgrade`/`-Rollback` around the swap alone: `Stop-Nwdash` throws under
  `$ErrorActionPreference='Stop'`, so a wider one also catches the refusals
  and the final failure, and undoes a deploy twice (review O1,
  `deploy/test/swap-recovery.test.ps1`).
- A `-Rollback` uses up the backup it restores: its files move into the
  install and `Remove-NwdashSpentBackup` removes the emptied folder, so the
  next `-Rollback` goes one further back (review O3). It moves rather than
  copies: a rename per item is quick and either happens or not, a copy of the
  runtime is thousands of files for EDR to hold, and a copied backup would stay
  newest, so a second `-Rollback` would restore the same build again.
  `Get-NwdashBackups` skips any backup with no file left (an earlier release's
  rollbacks left those), and a backup missing the launcher, `nwdash\config.py`
  or `runtime\python\python.exe` is refused before the service is stopped
  (`Get-NwdashBackupProblem`).

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
