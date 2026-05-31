# Changelog

All notable changes to the NetWorker Backup & Recovery Dashboard.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project aims to follow [Semantic Versioning](https://semver.org/).

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
