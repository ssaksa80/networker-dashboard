# NetWorker Dashboard

Self-contained HTTPS dashboard for monitoring Dell NetWorker backup and
recovery status. Single Python process, standard library only (plus an
optional `cryptography` dependency for encrypted credential storage) —
built for air-gapped and offline environments: no external CDNs, no
package installs required on the host.

## Features

- Live backup/recovery job status via the NetWorker REST API, with an
  NWUI fallback collector for older servers
- Optional Windows server health (CPU/memory/disk) via WMI
- Multi-server view, connection profiles, share-by-link read-only view
- Daily snapshots with history, comparison, and CSV export
- Scheduled Reports: named, ordered, on/off report **groups** — each group
  picks its own dashboard sections, recipients, and cadence (daily, weekly on
  Sunday, or monthly on the 1st, with matching retrospective windows) and
  pulls live data through one shared reporting connection. On-demand
  "Send now" (with a "test" checkbox) runs any group immediately. A save-time
  validation gate (connect + render + SMTP) blocks broken groups before
  they're saved, and a failed run still emails a fallback report plus a
  separate ops alert — including a rendered dashboard image when a headless
  Chrome/Edge is available
- Password login (PBKDF2), HMAC-signed sessions, CSRF protection,
  HSTS/CSP security headers, login rate limiting

## TV / DSO wall

A DSO TV can open `https://<host>:<port>/tv/<token>` (token generated in
admin -> TV / Display) to show the live wall with **no login**. Set a
"display connection" in that panel so the wall keeps refreshing 24/7 with
nobody signed in. Protect everything else with a dashboard password
(`Setup-NWDash.cmd -Upgrade -AuthPassword '...'`). Rotate the token from
the same panel to invalidate an old TV URL.

## Quick start

```powershell
# optional but recommended
$env:DASHBOARD_AUTH_PASSWORD = "choose-a-password"

python networker_dashboard.py
```

First start writes a development TLS certificate to `.certs/` and opens
the dashboard at `https://<host>:8443/`. Supply a real certificate with
`--cert`/`--key` for production use.

### Common flags

| Flag | Default | Purpose |
|---|---|---|
| `--bind` | `0.0.0.0` | Interface to bind (`127.0.0.1` = local only) |
| `--port` | `8443` | HTTPS port |
| `--cert` / `--key` | `.certs/…` | TLS certificate/key (PEM) |
| `--no-auto-cert` | off | Require an existing certificate |
| `--auth-password` | — | Dashboard password (or `DASHBOARD_AUTH_PASSWORD`) |
| `--allowed-hosts` | any | Allow-list of NetWorker hosts/CIDRs (or `DASHBOARD_ALLOWED_HOSTS`) |
| `--no-launch` | off | Don't open a browser after startup |
| `--debug` | off | Verbose REST diagnostics (never logs credentials) |

## Layout

```
networker_dashboard.py   thin launcher
nwdash/                  implementation package
  config.py              constants, paths, logging, runtime flags
  auth.py                password, session cookie, CSRF
  secrets.py             DPAPI/Fernet key handling
  restapi.py / nwui.py   NetWorker collectors
  server.py / main.py    HTTPS server and entrypoint
  ui.py + assets/        dashboard/login/view pages (real HTML/CSS/JS)
  …
tests/                   stdlib unittest smoke suite
data/                    runtime state (never commit)
```

## Tests

```powershell
python -m unittest discover -s tests -v
```

27 tests: module imports, auth/CSRF primitives (incl. tamper cases),
TLS fail-closed defaults, served-page wiring, and a subprocess
end-to-end HTTPS test that drives the real entry script through the
login/CSRF flow.

## Notes

- `data/`, `logs/`, `.certs/` hold runtime state and secrets — they are
  gitignored and must never be committed or copied between machines
  blindly: the key files under `data/` are DPAPI-protected per Windows
  account, so copies are unreadable elsewhere (the app then regenerates
  keys and previously saved sessions must be re-entered once).
- Outbound TLS verification to NetWorker servers is on by default and
  controlled per connection profile ("Verify REST API TLS certificate").
