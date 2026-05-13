# Changelog

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
