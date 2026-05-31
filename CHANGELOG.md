# Changelog

All notable changes to the NetWorker Backup & Recovery Dashboard.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project aims to follow [Semantic Versioning](https://semver.org/).

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
