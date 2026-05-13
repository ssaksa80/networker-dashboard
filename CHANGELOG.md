# Changelog

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
