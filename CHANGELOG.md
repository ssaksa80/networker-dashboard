# Changelog

All notable changes to the NetWorker Backup & Recovery Dashboard.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project aims to follow [Semantic Versioning](https://semver.org/).

## [2.4.1] — 2026-06-08

### Fixed
- **Orphaned schedules from old sessions kept emailing.** Each browser
  connection gets a fresh session id, so a previous session's automations became
  orphans: invisible in the per-session modal list (which filters by the current
  `session_id`) yet still fired by the scheduler, which re-sent old reports from
  stale entries in `data/automations.json`. The scheduler now prunes any
  automation whose dashboard session no longer exists before firing
  (`prune_orphaned_automations`, run every tick), cancelling it and rewriting the
  persisted file so dead-session schedules stop sending and are removed from
  disk. `restore_automations_from_disk` likewise now rewrites the file to drop
  orphaned/invalid records instead of leaving them to accumulate. Net effect: the
  only schedules that fire are the ones currently shown and configured in the
  modal; the automations file self-heals to match the live sessions.

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

## [2.3.2] — 2026-06-08

### Fixed
- **Scheduled email reports now arm on "Save configuration."** Debug logging
  added in 2.3.1 revealed the real cause: users clicked **Save configuration**
  (`action=save`, which only persisted recipients/theme/SMTP) and never the
  separate **Schedule selected report** button (`action=start`, which actually
  armed `next_run_at`). The schedule therefore stayed inactive and no report ever
  fired. `handle_alert_automation` now also arms the schedule on `action=save`
  when a live session is connected and the config is schedulable (alert, or
  daily_report with a report time and recipients), matching the user's
  expectation that saving enables the notification. The standalone Schedule
  button still works; saving with malformed SMTP fields still completes the plain
  config save without raising.

## [2.3.1] — 2026-06-07

### Added
- `--debug` now logs the action of every `/api/alert-automation` request
  (`action=start|save|test|stop`, scheduleType, recipient count) on entry. This
  pinpoints whether a popup interaction actually scheduled a report
  (`action=start`) or merely saved the configuration (`action=save`), which was
  previously indistinguishable in the logs.

## [2.3.0] — 2026-05-31

### Changed
- **Rebuilt the email-automation scheduler for reliability.** Scheduled reports
  still weren't firing dependably. Each automation used its own long-lived
  `threading.Timer` (up to 24 h), and any failure was captured only in memory —
  invisible and fragile. Replaced this with a single background scheduler loop
  (`automation_scheduler_loop`, 30 s tick) that stores each automation's
  `next_run_at` and fires due ones, mirroring the proven shared-refresh loop.
  - Every step is now logged: when an automation is scheduled (with the exact
    next-run wall-clock time), when it fires, the dashboard build status, and
    the final result. Send failures are logged at WARNING level (visible without
    `--debug`); full step detail is at DEBUG.
  - The next run time is recomputed after every run and automation state is
    persisted, so the schedule self-heals and survives restarts.

## [2.2.9] — 2026-05-31

### Fixed
- Scheduled email notifications never fired (manual "Send test" worked). The
  schedule was created with an in-memory `threading.Timer` only and was **not
  persisted**, so any server restart wiped every scheduled alert/daily report —
  and the daily report timer is set for a future time, so it almost never
  survived to fire. Automations are now persisted to disk
  (`data/automations.json`, SMTP password encrypted at rest) on schedule/stop
  and **restored + rescheduled on startup**, right after their dashboard
  sessions are restored. A daily report scheduled for 08:00 now still fires
  after the server restarts.

## [2.2.8] — 2026-05-31

### Fixed
- The email report's brand/header card color did not match the live dashboard.
  The dashboard brand card uses a fixed navy→teal gradient over the brand color,
  but the email and PNG snapshot used a flat theme-brand color. Both now reuse
  the dashboard's exact brand-card styling (shared `BRAND_CARD_GRADIENT` /
  `BRAND_CARD_SOLID` constants): the PNG snapshot (rendered by Chrome) applies
  the gradient directly, and the email applies the gradient via
  `background-image` for modern clients plus a solid dark-teal `bgcolor`
  fallback for Outlook. The body cards continue to follow the selected theme.

## [2.2.7] — 2026-05-31

### Fixed
- Opening the email notification popup reset the dashboard theme back to default.
  The modal wrote the email config's saved per-type theme into the **shared**
  `themeSelect` control (`themeSelect.value = c.dailyReport.theme`), overriding
  the dashboard theme. Since the report theme is dynamic (it follows the current
  dashboard theme, persisted server-side), the modal no longer touches
  `themeSelect` at all. The send/test payload still captures the live theme, so
  report emails keep following the currently selected theme.

## [2.2.6] — 2026-05-31

### Fixed
- The Local Snapshot Growth panel stayed stuck on the disconnected "Waiting —
  Connect to NetWorker, then save a local snapshot" placeholder even while
  connected and even when snapshots were already saved on disk. `renderDashboard`
  enabled the Save button but never refreshed the panel. A new
  `refreshSnapshotStatus()` now loads saved snapshots and renders the growth
  comparison (or a "Ready / N snapshot(s) saved" status) on connect, on page
  load, and right after an auto-save — so the panel reflects real snapshot state
  instead of the placeholder.

## [2.2.5] — 2026-05-31

### Fixed
- Auto-save daily snapshot appeared not to work: enabling it did nothing visible
  because the background worker only ticks every 10 minutes and skips silently
  when a snapshot already exists for the day or no dashboard is loaded.
  - Enabling auto-save now captures a snapshot **immediately** (instead of
    waiting up to 10 minutes for the next worker tick).
  - `_auto_snapshot_once()` returns a status (`saved` / `exists` /
    `no-dashboard` / `disabled`) and logs each outcome; the toggle's POST
    response reports it, and the UI shows a precise toast ("snapshot saved now",
    "today already captured", or "will capture once connected") plus refreshes
    the snapshot meta line.

## [2.2.4] — 2026-05-31

### Fixed
- A failed **clone** job from the NetWorker jobs database was counted under
  **Failed Backups** instead of **Failed Clones**. When converting a jobs-DB
  record to an action (`rest_job_as_nwui_action`), the job `name` (often a
  save-set string) took priority over `policyActionName` (the NetWorker action
  type: backup/clone/...), so after projection the clone signal was lost and
  `is_clone_job` mis-classified the run as a backup. The action type now takes
  priority (matching the live monitoringactions feed), is carried through
  projection as `_action_type`, and `is_clone_job` also inspects
  `policyActionName`/`_action_type`. Clone failures now land in Failed Clones
  and are excluded from the backup failed count.

## [2.2.3] — 2026-05-31

### Changed
- The single **Failed Jobs** metric tile is now split into three separate tiles
  — **Failed Backups**, **Failed Restores**, and **Failed Clones** — so backup,
  recovery, and clone failures are each shown distinctly. The values come from
  the existing `failedJobs`, `recoveryFailed`, and `cloneFailed` summary fields
  (both REST and NWUI paths already populate them); the responsive metric grid
  flows the extra tiles automatically.

## [2.2.2] — 2026-05-31

### Changed
- Scheduled dashboard report emails now follow the **current** dashboard theme
  dynamically, instead of the theme frozen when the schedule was created. The
  selected theme is persisted server-side (`data/ui_prefs.json`) whenever it
  changes, and the background report job reads it at send time (falling back to
  the schedule's captured theme, then default). New `GET`/`POST /api/ui-theme`;
  the dashboard posts the theme on every apply.

## [2.2.1] — 2026-05-31

### Fixed
- Scheduled dashboard report emails ignored the selected theme color on the
  brand/header card. Scheduled reports forced a fixed dark-green header
  (`#003b24`) while the body cards and the PNG attachment followed the theme —
  so the header never matched. The emailed report now uses the chosen theme's
  brand color throughout (`dashboard_report_email` and `report_status_model`),
  consistent with the attached snapshot. Removed the now-unused
  `SCHEDULED_REPORT_DARK_GREEN` constant.

## [2.2.0] — 2026-05-31

### Added
- **Saveable email notification configuration.** The email automation popup now
  has a **Save configuration** button that persists the setup to disk
  (`data/email_config.json`) so it survives restarts and pre-fills the form on
  next open. The SMTP password is encrypted at rest and never returned to the
  browser (only a "saved" indicator); leaving the password blank keeps the
  previously saved one.
- **Separate Alert and Daily-report configuration.** The two notification types
  (**Alert check** and **Daily backup/SLA report**) now keep their own
  **separate recipient lists** and per-type settings (alert trigger/interval vs
  report time/theme). The shared SMTP transport is stored once; switching the
  *Email type* swaps to that type's saved recipients without disturbing the
  other. Saving or scheduling one type never overwrites the other's recipients.
- `GET /api/email-config` (UI-safe, password-masked) and a `save` action on
  `POST /api/alert-automation`. Scheduling a notification also persists its
  configuration. Saved SMTP password is used as a fallback when scheduling.

## [2.1.11] — 2026-05-31

### Performance
- Dashboard refreshes were still timing out (`/api/dashboard`,
  `/api/current-dashboard`, `/api/snapshots`). The raw `/global/jobs`
  diagnostic showed why: the jobs database holds **36,031 jobs / 11.5 MB**, and
  almost all of that volume is the per-record `message` field (multi-KB of job
  log text each). Every cache-miss downloaded, JSON-parsed and log-cleaned all
  36k records.
  - `message` is now excluded from the bulk jobs field list, cutting the
    response roughly 10x (~11.5 MB → ~1 MB) and removing the per-record log
    cleaning. Failure detail is preserved via the small, `Failed`-filtered
    `failedJobs` query, which keeps `message`.
  - The completed-job history is now trimmed to the report window **before**
    sorting and projection, so only the in-window jobs (~2.3k) are processed
    instead of the full 36k.

### Notes
- **DPA parity:** the dashboard counts ~2,248 succeeded in 24 h vs DPA's 1,979.
  Both read the same jobs DB; the difference is job granularity (NetWorker's
  jobs DB records backups at multiple levels). The dashboard count is the raw
  succeeded-job count in the window.
- **Size (GB) metric:** not available from `/global/jobs` — the jobs resource
  does not expose a byte field (NetWorker rejects `saveBytes`/`transferredBytes`
  and the size lives in the media database). Not shown for NWUI-sourced data.

## [2.1.10] — 2026-05-31

### Added
- `--debug` diagnostic that dumps a raw `/global/jobs` record (all field
  names + values for the first 3 jobs) and a `completionStatus` breakdown of
  the full set, before projection. This reveals the exact NetWorker job field
  names needed to reconcile counts against DPA and to restore the backed-up
  size metric (the original `saveBytes`/`transferredBytes` names were rejected
  by NetWorker). Logs on a jobs-history cache miss.

## [2.1.9] — 2026-05-31

Completed-job history now loads end-to-end (verified on a live server:
`historyActions=2417`, ~2,248 succeeded in 24 h). This release fixes the
fallout from pulling that large dataset on every refresh and cleans up the
status accounting.

### Added
- **Short-TTL cache for the NetWorker jobs-history fetch.** The `/global/jobs`
  response is large (~11 MB / thousands of jobs — NetWorker has no server-side
  time filter) and changes little between refreshes. It is now cached
  process-wide for 180 s, keyed by server + report range, and shared across all
  dashboard sessions and the shared refresh loop. Previously every build for
  every restored session re-downloaded and re-parsed the full set, starving the
  request workers and timing out unrelated endpoints
  (`/api/current-dashboard`, `/api/snapshots`).

### Fixed
- Status-less job records (empty `completionStatus`) are no longer merged as
  bogus "unknown" completed jobs. The history merge now keeps only terminal
  statuses (succeeded / failed / warning), so totals match what actually ran.
- `MissedTheSchedule` (and `skipped` / `interrupted` / `never started`) now
  normalize to a `warning` instead of leaking the raw status string; `aborted`
  variants normalize to `failed`.

### Changed
- `fetch_json()` accepts a `max_bytes` ceiling and logs a `REST GET too-large`
  line when a response is capped (from 2.1.8).

## [2.1.8] — 2026-05-31

### Fixed
- Job history still came back empty even after the query/field fixes. With a
  valid query, NetWorker returned the **entire** jobs database (it has no
  server-side time filter), and the response blew past the 8 MB safety guard in
  `read_limited()`, which raised `502 "REST API response exceeded dashboard
  safety limit."` *inside* the download — silently, before any success/error
  log line — so each version attempt aborted and `historyActions` stayed 0.
  The jobs-history fetch now uses a much higher response ceiling
  (`MAX_JOBS_RESPONSE_BYTES`, 64 MB) and a longer read timeout (≥120 s), and
  logs a clear `REST GET too-large` line if a response still overruns it. The
  full set is trimmed to the report window client-side as before. This applies
  to both the NWUI history merge and the REST-mode jobs/failedJobs fetches.

### Known limitation
- Because NetWorker cannot filter `/global/jobs` by time, very large windows on
  very busy servers (e.g. a year of history) can still exceed even the 64 MB
  ceiling. Day/week/month ranges are unaffected. A future enhancement may add
  incremental/streamed parsing if NetWorker exposes job pagination.

## [2.1.7] — 2026-05-31

### Fixed
- Completed jobs still did not appear; `--debug` on the live server revealed
  two concrete causes in the `/global/jobs` history query, now fixed:
  1. **Invalid NetWorker query syntax.** The 2.1.6 server-side time window sent
     `q=startTime>="..."`. NetWorker Query Language supports only
     `field:value` equality and rejected it with HTTP 400
     ("A query should be in form of <field>:<value>"). NetWorker has no range
     operator, so the report window cannot be applied server-side. The window
     is now enforced entirely client-side by `in_report_window()`; the jobs
     database is naturally bounded by NetWorker's completed-job retention.
  2. **Invalid field-list fields.** The `fl` parameter requested
     `elapsedTime`, `policyName`, `saveBytes`, and `transferredBytes`, which
     NetWorker rejects as not-valid job fields (HTTP 400), forcing several
     wasted retry round-trips. `fl` is now limited to the validated set
     (`clientHostname, startTime, completionStatus, name, message,
     policyActionName, workflowName, level`); the per-version auto-strip
     remains as a safety net.
  With a valid query and field list, `/global/jobs` on the NetWorker server
  returns the completed-job history, which is merged into the dashboard so the
  last-24h / week / month job counts populate instead of showing only the
  live running actions.

### Note
- nwrestapi is served by the NetWorker server (e.g. `198.51.100.11:9090`), not the
  NWUI front end (`192.0.2.10:9090`), which returns 404 for `/nwrestapi`. The
  REST fallback already tries the backup-server host first.

## [2.1.6] — 2026-05-31

### Fixed
- Completed jobs from the selected range still did not appear on busy
  NetWorker servers. Root cause: the `/global/jobs` query had **no
  server-side time filter**, so NetWorker returned its entire jobs database.
  On an active server (e.g. ~1,979 jobs/24h, hundreds of thousands all-time)
  the response overran the 8 MB safety guard in `read_limited()` and the whole
  query hard-failed with HTTP 502, leaving the dashboard with live activity
  only. The jobs query is now bounded server-side with a NetWorker Query
  Language `startTime>=` floor derived from the report window (with a generous
  36 h margin for timezone/clock skew); the exact range is still trimmed
  client-side by `in_report_window()`. This makes "last 24 h / week / month"
  return the full completed-job history the way NetWorker (and DPA) report it.
- If a NetWorker version rejects the time-window query syntax (HTTP 400), the
  REST fallback now drops the `q` filter once and retries unfiltered, so
  smaller deployments keep working unchanged.

## [2.1.5] — 2026-05-31

### Fixed
- Last-24h (and other ranges) showed only currently-running jobs and no
  completed backups. Root cause: `/nwui/api/monitoringactions` is the live
  Activity Monitor — it returns only the currently-active workflow actions
  (all `status=Running`, `completionTime=None`) and ignores the requested time
  window, so finished daily runs never appeared. The NWUI dashboard now also
  pulls completed job history from the NetWorker jobs database
  (`nwrestapi /global/jobs`) and merges it with the live action set:
  - completed/terminal runs come from the jobs DB (window-honored),
  - running/queued runs come from the live monitor,
  - the two are de-duplicated by workflow+action+normalized-start-time, with
    terminal records preferred over a stale "running" record on collision.
  Best-effort and fully guarded: if the jobs-DB query fails, the dashboard
  falls back to live-activity-only (recorded as an info-level diagnostic
  source, never a visible warning) exactly as before.

### Added
- `--debug` instrumentation for the NWUI activity pipeline: logs the report
  window, live vs history vs merged action counts, raw/normalized status
  breakdowns, and field samples of the first raw action records.

## [2.1.4] — 2026-05-31

### Fixed
- Account dropdown menu rendered as an empty white box. The buttons inherited
  the top bar's white-on-dark styling (`color:#fff`) but the dropdown surface
  is white, making them invisible. Dropdown buttons now use `var(--ink)` text
  on `var(--surface-2)` with themed hover/danger states, so they are readable
  in every theme. Regression from the 2.1.0 collapsible-toolbar refactor.

## [2.1.3] — 2026-05-31

### Fixed
- Backup SLA panel no longer shows "No backup jobs ran in this range" when
  backup jobs are actively running. The panel now reads
  "N job(s) currently running — SLA pending" until jobs complete.
- NWUI path: `totalJobs` in the dashboard summary now includes active/running
  jobs (previously only counted completed jobs), fixing snapshot metrics and
  report totals when jobs are mid-flight.

## [2.1.2] — 2026-05-31

### Fixed
- Cached fallback dashboards no longer render as a failed backup source named
  `last-successful-dashboard`. Live refresh failures are kept as backend
  diagnostics, while the UI shows a clean cached-dashboard notice until current
  NetWorker backup activity is available again.

## [2.1.1] — 2026-05-24

### Fixed
- Client disconnecting mid-response (e.g. browser/auto-refresh aborting the TLS
  connection) raised `ssl.SSLEOFError` during the response write; the 500
  handler then tried to write to the dead socket, raising `ssl.SSLError
  [SSL: BAD_LENGTH]`, which escaped and made `socketserver` dump a raw multi-line
  traceback to stderr. `_send_bytes` now swallows transport errors (OSError /
  SSL) and aborts the connection quietly, and the server's `handle_error` logs
  benign client disconnects at DEBUG (real faults at ERROR with traceback) as
  structured JSON instead of stderr dumps.

## [2.1.0] — 2026-05-24

### Added
- Collapsible toolbar groups for a clean command-central / TV view. The
  controls are now three caret dropdowns that slide open/closed:
  **View settings** (range, auto-refresh, interval, theme, Excel export),
  **Snapshots** (7d/30d/90d, auto-save, save/compare/manage/CSV), and
  **Account** (Connection, +Server, Share, Logout, Email). Connection status,
  range/last-refresh, and Refresh now stay always visible. Each group defaults
  collapsed and remembers its open/closed state per browser (localStorage).
  Pure CSS slide + small vanilla JS; all existing control IDs/handlers
  unchanged.

## [2.0.2] — 2026-05-24

### Fixed
- Ctrl+C / SIGINT shutdown could raise `Fatal Python error:
  _enter_buffered_busy: could not acquire lock for <_io.BufferedWriter
  name='<stderr>'> ... possibly due to daemon threads`. Shutdown now flushes
  logging and exits via `os._exit(0)` after best-effort cleanup, skipping the
  interpreter finalization that deadlocked against daemon threads still writing
  to stderr.

## [2.0.1] — 2026-05-24

### Changed
- Default `--bind` is now `0.0.0.0` (all interfaces) so the dashboard is
  reachable on the server IP for publishing without an extra flag. Use
  `--bind 127.0.0.1` to restrict to local only. Binding non-loopback without an
  auth password still prints the startup warning.

## [2.0.0] — 2026-05-22 — Security & Stability Hardening

A four-phase hardening pass turning the dashboard into an enterprise-grade,
24x7 service. Delivered as reviewed phases, each with a design spec, an
implementation plan (`docs/superpowers/`), unit tests, and a live smoke test.

> **Breaking:** the default bind changed from `0.0.0.0` to `127.0.0.1`. To
> expose on the network, pass `--bind 0.0.0.0` and set an auth password.

### Added
- **Authentication** — shared gateway password (`--auth-password` /
  `DASHBOARD_AUTH_PASSWORD`), stored as a salted PBKDF2-SHA256 hash; HMAC-signed
  `HttpOnly`/`Secure`/`SameSite=Strict` session cookie (12 h TTL); login page;
  `POST /api/login` + `/api/logout`; per-IP login rate-limit (5 / 5 min → 429).
- **SSRF allow-list** — `--allowed-hosts` / `DASHBOARD_ALLOWED_HOSTS` accepting
  hostnames, IPs, and CIDRs; resolved-IP checks with startup pinning to resist
  DNS rebinding; enforced in connect-config validation and session restore.
- **Windows DPAPI** key protection — `.session_key` and `.auth_key` wrapped with
  `CryptProtectData` (machine scope); legacy plaintext keys auto-migrated.
- **Token-scoped share data** — `GET /api/view/<token>` so the read-only share
  page no longer reads the global dashboard endpoint.
- **Operational endpoint** — cookie-gated `GET /api/status` (uptime, threads,
  session/automation/SSE counts, refresh age, auth/allow-list flags).
- **Structured logging** — JSON lines via a rotating file handler
  (`logs/networker_dashboard.log`, 10 MB × 5) plus stderr; per-request
  correlation IDs; the client 500 reference ID equals the log `request_id`.
- CLI flags `--request-timeout`, `--max-connections`, `--max-sse`,
  `--auth-password`, `--allowed-hosts`.
- Unit suites `test_phase1`–`test_phase3` and project docs (`README`, `LICENSE`,
  `requirements.txt`, this changelog).

### Changed
- Default `--bind` is now `127.0.0.1` (local-only out of the box).
- Background refresh + auto-snapshot loops run their bodies under exception
  guards so a single failure can never kill the thread.
- Snapshot writes are atomic (temp file + replace), matching the other
  persisters.
- SSE broadcasts write outside the client lock so one slow client cannot stall
  all viewers.
- Startup banner clarifies the credential-at-rest posture (the old "not written
  to disk" line was inaccurate and was corrected).

### Security
- All data endpoints are gated behind authentication when a password is set;
  `/api/health` remains open for load-balancer liveness only.
- Internal exceptions no longer leak to clients — responses carry a generic
  message + reference ID; full detail is logged server-side.
- `data/`, `.certs/`, and `logs/` are git-ignored to keep keys, certs, encrypted
  credentials, and logs out of version control.

### Fixed
- Thread-unsafe global session/automation registries (could raise
  `RuntimeError: dictionary changed size during iteration`) are now guarded by a
  reentrant lock with snapshot-based iteration.
- No per-request socket timeout / unbounded connections (slowloris &
  thread/FD-exhaustion exposure) — added a request timeout plus connection and
  SSE caps; the connection slot is released even if worker-thread creation fails.
- Only `KeyboardInterrupt` was handled on shutdown — SIGTERM/SIGINT now route
  through a clean shutdown path.

## [1.1.15] — Prior

Baseline single-file HTTPS dashboard: NetWorker REST/NWUI integration, WMI
server health, snapshots, scheduled SMTP reports, Excel export, SSE live
updates, connection profiles, and shareable read-only views.
