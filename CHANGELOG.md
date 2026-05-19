# Changelog

## v1.1.12 - 2026-05-19

- Added NetWorker-style log cleanup for multi-record `jobOutput` payloads from both NWUI and direct REST job sources.
- Recent Jobs now suppresses raw `savegrp`, `nsrim`, and `nsrexecd` telemetry IDs and shows concise NMC-style messages such as final waiting, processing, or crosscheck events.
- Added a Logs tab with Priority, Time, Source, Category, and Message columns for easier comparison with the NetWorker console log view.
- Changed job duration display from raw seconds to readable values such as `5m 31s` and `8h 36m`.
- Added regression coverage for combined NetWorker output parsing, direct REST message cleanup, NWUI message cleanup, log tab rendering, and duration formatting.

## v1.1.11 - 2026-05-19

- Fixed NWUI split-host direct REST fallback: when `/nwui/api/monitoringactions` returns HTTP 500, the dashboard now tries `/nwrestapi/.../global/jobs` on the configured NetWorker backup server before falling back to the NWUI host.
- Added detailed direct REST fallback diagnostics that show the host/version attempts, making HTTP 404 routing issues easier to identify.
- Protected scheduled daily backup/SLA emails from sending misleading empty reports when the backup job source is unavailable.
- Scheduled reports now reuse the last successful dashboard snapshot with a visible report notice, or skip sending if no reliable data is available.
- Prevented failed zero-count refreshes from overwriting the shared last-good dashboard cache.
- Improved the dashboard card layout so Activity Mix, Backup SLA, Management Overview, and the branded status card remain readable at default size and browser zoom levels.
- Added regression coverage for split-host fallback routing, scheduled-report source validation, last-good report reuse, and responsive dashboard layout rules.

## v1.1.10 - 2026-05-18

- Added a direct NetWorker REST fallback for NWUI `/monitoringactions` HTTP 500 failures so backup job data can still load when the NWUI monitoring POST endpoint is unstable.
- Treated NWUI `/monitoringpolicies` failures as optional when policy fallback is unavailable, preventing noisy source warnings while preserving backup job visibility.
- Added encrypted in-process NetWorker credential retention and automatic NWUI re-login so dashboard refreshes can recover from expired upstream sessions without asking for the password again.

## v1.1.9 - 2026-05-17

- Updated NWUI backup summary totals to count save-session inputs from `jobData` so Completed, Succeeded, Failed, and Active backup counts align with Data Protection Advisor style reports.
- Preserved decimal SLA percentages such as `99.86%` instead of rounding large reports up to `100%`.
- Updated startup binding and console output so the HTTPS service runs on both localhost and the local server IP by default.

## v1.1.8 - 2026-05-17

- Updated the scheduled backup/SLA email report to mirror the dashboard output order.
- Added the same six top cards, six metric cards, and NetWorker Server Health section to the embedded report.
- Matched Server Protection Job, CPU, and memory display with the live dashboard snapshot.
- Added NWUI monitoring retries for intermittent HTTP 500 failures, including smaller page sizes and a locally-filtered no-date fallback.
- Scheduled backup/SLA email reports now use the dashboard theme selected when the email automation is scheduled or tested.
- Scheduled backup/SLA HTML report emails now use a dark green background only on the branded NetWorker status card.
- Alert emails and daily dashboard report emails can now be scheduled at the same time for the same dashboard session.
- Scheduled backup/SLA emails now keep the dashboard-style HTML body and attach the generated dashboard PNG snapshot as `networker-dashboard.png`.
- Hardened the scheduled report brand card so email clients render the full NetWorker status block with a solid dark green background and fixed logo size.

## v1.1.7 - 2026-05-13

- Added SMTP delivery diagnostics for test email failures, including connect, EHLO, STARTTLS, login, and send stages.
- Surfaced SMTP host, port, security mode, auth state, recipient count, and sanitized provider error details in the dashboard.
- Preserved scheduled automation failure details without exposing SMTP passwords.

## v1.1.6 - 2026-05-13

- Fixed WMI health collection for local backup server targets by omitting explicit credentials on local WMI calls.
- Remote WMI targets still use the provided WMI username/password.

## v1.1.5 - 2026-05-13

- Updated SMTP security UI behavior so selecting `None` forces port `25`.
- SMTP username and password fields are disabled and cleared when SMTP security is set to `None`.

## v1.1.4 - 2026-05-13

- Fixed daily scheduled report test emails after the SMTP password field is cleared by reusing the encrypted in-memory scheduled SMTP password.
- Daily report test emails now use the current dashboard snapshot when available, avoiding unnecessary live NetWorker refresh failures during SMTP testing.

## v1.1.3 - 2026-05-13

- Removed transient refresh timeout text from the visible Server Protection Job card when showing a last-known successful job.
- Kept refresh failure details internally without cluttering the dashboard status text.

## v1.1.2 - 2026-05-13

- Enhanced scheduled daily email reports with an embedded dashboard-style HTML summary.
- Added branded status, Activity Mix, Backup SLA, Management Overview, Recovery Health, Clone Jobs, and metric cards to the report email body.

## v1.1.1 - 2026-05-13

- Fixed repeated Server Protection Job fallback text when intermittent REST/NWUI refresh timeouts occur.
- Added regression coverage to keep last-known Server Protection details stable across repeated refresh failures.

## v1.1.0 - 2026-05-13

- Added live Server Protection Job refresh with the server health refresh cycle.
- Added SMTP email alert automation with STARTTLS, SSL/TLS, and plain SMTP options.
- Added scheduled daily backup/SLA email reports embedded in the email body.
- Added additional UI themes.
- Kept HTTPS-only local serving, encrypted in-memory WMI/SMTP password handling, and automated test coverage.

## v1.0.0 - 2026-05-13

- Initial single-file Dell NetWorker Backup & Recovery Dashboard release.
- Added NWUI/REST dashboard views, SLA charting, WMI server health, clone job separation, Excel export, and local HTTPS startup.
