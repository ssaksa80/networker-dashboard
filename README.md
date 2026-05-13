# NetWorker Backup Dashboard

Version: `1.1.4`

Local HTTPS dashboard for Dell NetWorker backup monitoring. This recreates the May 6 prototype as a single-file project with the same login flow and the `/nwui/api/monitoringactions` polling behavior.

## Run Locally

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python networker_dashboard.py
```

Open the `https://...` URL printed in the console. The dashboard tries port `8443` by default; if that port is busy, it automatically selects a free random HTTPS port. The first run creates a self-signed certificate under `.certs/`, so the browser will ask you to accept the local certificate.

## Email Alerts

After connecting to NetWorker, use **Email Alert Automation** to configure SMTP delivery. The dashboard can send a test email, schedule recurring alert checks, or schedule a daily backup/SLA status report from the current logged-in session. SMTP passwords are accepted over the local HTTPS page, encrypted in process memory, and are not written to disk.

The alert scheduler can trigger on critical issues only, warnings plus critical issues, or every scheduled check. Daily reports are embedded directly in the email body with dashboard-style summary cards, activity/SLA donuts, management bars, backup job totals, success/failure counts, active jobs, alerts, server health, clone/recovery health, and the latest Server Protection Job state.

## Configuration

The dashboard listens on `127.0.0.1` by default so it is not exposed to the network during local testing.

Useful settings:

- `--bind` controls the local listener address.
- `--port` controls the preferred HTTPS port. If unavailable, the app selects a free random port.
- `--cert` and `--key` can point to your own TLS certificate and private key.
- `--no-auto-cert` requires the provided certificate files instead of writing the embedded development certificate.
- `--debug` enables verbose REST API diagnostics without logging passwords or Authorization headers.

## Security Notes

- The dashboard only serves over HTTPS. Plain HTTP requests are rejected and the listening socket is wrapped with TLS before serving.
- NetWorker passwords are not saved to disk. WMI credentials are encrypted while held in process memory.
- SMTP passwords for alert automation are encrypted while held in process memory and are cleared from the browser field after use.
- Browser responses include no-store cache headers, HSTS, frame denial, content type protection, and a restrictive content security policy.
- Generated certificates and `.env` are ignored by git.

## Test

```powershell
python -m pip install -r requirements-dev.txt
pytest
```
