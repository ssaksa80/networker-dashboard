# NetWorker Backup Dashboard

Local HTTPS dashboard for Dell NetWorker backup monitoring. This recreates the May 6 prototype as a single-file project with the same login flow and the `/nwui/api/monitoringactions` polling behavior.

## Run Locally

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python networker_dashboard.py
```

Open the `https://...` URL printed in the console. The dashboard tries port `8443` by default; if that port is busy, it automatically selects a free random HTTPS port. The first run creates a self-signed certificate under `.certs/`, so the browser will ask you to accept the local certificate.

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
- Browser responses include no-store cache headers, HSTS, frame denial, content type protection, and a restrictive content security policy.
- Generated certificates and `.env` are ignored by git.

## Test

```powershell
python -m pip install -r requirements-dev.txt
pytest
```
