# NetWorker Backup & Recovery Dashboard

A single-file, HTTPS-only web dashboard for monitoring **Dell EMC NetWorker**
backup, clone, and recovery activity — backup job status, SLA summaries, server
health (CPU/RAM via WMI), alerts, daily snapshots, scheduled email reports, and
Excel export.

Built to run **24x7x365** as an enterprise service: no web framework, no
database — the Python 3.9+ standard library plus one optional dependency.

> Connects to your NetWorker REST API / NWUI. Credentials are accepted per
> request, encrypted at rest, and never placed in URLs.

---

## Highlights

- **HTTPS only** with HSTS, CSP, `X-Frame-Options: DENY`, and no-store caching.
- **Gateway authentication** — shared password → HMAC-signed `HttpOnly` /
  `Secure` / `SameSite=Strict` session cookie gating every data endpoint.
- **SSRF allow-list** — restrict which NetWorker hosts/IPs/CIDRs the server may
  connect to, with DNS-rebinding-resistant resolved-IP checks.
- **Encrypted secrets at rest** — Fernet master key + AES-256-GCM profile
  secrets; on Windows the key files are wrapped with **DPAPI** (machine scope).
- **Hardened for long uptime** — thread-safe session/automation registries,
  exception-guarded background loops, per-request socket timeout, connection &
  SSE caps, atomic state writes, graceful SIGTERM/SIGINT shutdown.
- **Observability** — JSON structured logging with rotation, per-request
  correlation IDs, and a `/api/status` operational endpoint.
- **Live updates** via Server-Sent Events; shareable read-only views via
  capability-URL tokens.
- **Reports** — daily snapshots, scheduled SMTP email reports, and `.xlsx`
  export, all generated from the stdlib.

See [`CHANGELOG.md`](CHANGELOG.md) for the full hardening history.

---

## Requirements

- **Python 3.9+**
- Optional: [`cryptography`](https://pypi.org/project/cryptography/) — enables
  credential-at-rest encryption (master key, WMI passwords, saved profile
  passwords). Everything else works without it.
- Optional, for emailed snapshot images: Microsoft Edge or Google Chrome
  (headless screenshot).
- Windows is required only for **WMI** server-health collection and **DPAPI**
  key wrapping; the dashboard itself is cross-platform.

```bash
pip install -r requirements.txt   # installs the optional 'cryptography' package
```

---

## Quick start

```bash
# Local only (loopback), no auth — for evaluation on your own machine
python networker_dashboard.py

# Network-exposed, production-style: set a password and restrict targets
python networker_dashboard.py \
  --bind 0.0.0.0 \
  --auth-password "change-me" \
  --allowed-hosts "nw1.corp.local,10.0.0.0/24"
```

On first run a self-signed development certificate is written to `.certs/`
(replace it with a CA-signed cert via `--cert`/`--key` + `--no-auto-cert` for
production). The dashboard opens at `https://localhost:8443/`.

The password may also be supplied via the `DASHBOARD_AUTH_PASSWORD` environment
variable, and the allow-list via `DASHBOARD_ALLOWED_HOSTS`.

---

## Configuration (CLI flags)

| Flag | Default | Purpose |
|------|---------|---------|
| `--bind` | `127.0.0.1` | Interface to bind. Use `0.0.0.0` to expose on the network (set a password first). |
| `--port` | `8443` | HTTPS port. Falls back to a random free port if busy. |
| `--cert` / `--key` | `.certs/...` | TLS certificate / private key (PEM). |
| `--no-auto-cert` | off | Require an existing cert instead of writing the dev cert. |
| `--auth-password` | _(none)_ | Gateway password. Also `DASHBOARD_AUTH_PASSWORD`. Stored only as a salted PBKDF2 hash. |
| `--allowed-hosts` | _(none)_ | Comma-separated NetWorker host/IP/CIDR allow-list. Also `DASHBOARD_ALLOWED_HOSTS`. Unset = any host. |
| `--request-timeout` | `30` | Per-request socket timeout (seconds) — slowloris guard. |
| `--max-connections` | `200` | Max concurrent connections before new ones are refused. |
| `--max-sse` | `50` | Max concurrent live-stream (SSE) viewers. |
| `--no-launch` | off | Do not auto-open a browser after the startup self-test. |
| `--debug` | off | Verbose diagnostics (no passwords / Authorization headers logged). |

---

## Security posture

- **Authentication is required to expose data.** When a password is configured,
  all data endpoints require a valid cookie; `/api/health` stays open for load
  balancers. With no password and a loopback bind, the dashboard runs open for
  local use. Binding non-loopback **without** a password boots with a loud
  warning.
- **Login throttling** — 5 failed attempts per IP per 5 minutes → HTTP 429.
- **SSRF** — when `--allowed-hosts` is set, connections to any other target are
  rejected (HTTP 400). Hostname entries are pinned to their resolved IPs at
  startup to resist DNS rebinding.
- **Secrets at rest** — `data/` holds the master key, auth key, encrypted
  credentials, sessions, and snapshots. On Windows the key files are DPAPI-
  wrapped (machine scope: defends against offline disk/file theft, not a local
  code-exec attacker). `data/`, `.certs/`, and `logs/` are git-ignored.
- **Error handling** — internal exceptions return a generic message plus a
  reference ID; full detail is logged server-side under the same request ID.

### Deploy checklist
1. Set `--auth-password` (or `DASHBOARD_AUTH_PASSWORD`) before binding non-loopback.
2. Set `--allowed-hosts` to your NetWorker servers/subnets.
3. Replace the dev cert with a CA-signed cert (`--cert`/`--key --no-auto-cert`).
4. `pip install cryptography` to enable credential-at-rest encryption.
5. Run behind a reverse proxy (TLS termination / SSO) if exposing remotely.

---

## Endpoints

**Open:** `GET /api/health`, `GET /favicon.ico`, `GET /networker-logo.png`,
`POST /api/login`, `POST /api/logout`.

**Capability-token (no login):** `GET /view/<token>` (read-only page),
`GET /api/view/<token>` (token-scoped dashboard data).

**Authenticated (cookie required when auth is on):** `GET /` (dashboard SPA),
`GET /api/status`, `GET /api/current-dashboard`, `GET /api/profiles`,
`GET /api/stream` (SSE), `GET /api/snapshots`, and `POST` `/api/dashboard`,
`/api/export`, `/api/server-health`, `/api/alert-automation`, `/api/snapshots`,
`/api/share`, `/api/multi-server`, `/api/profiles`.

`/api/status` returns uptime, thread count, session/automation/SSE counts,
shared-dashboard refresh age, and the auth/allow-list flags.

---

## Logging

JSON lines (one object per line) to a rotating file at
`logs/networker_dashboard.log` (10 MB × 5) and to stderr. Each line carries
`ts`, `level`, `logger`, `msg`, and where relevant `request_id`, `client`,
`status`, and `exc`. The client-facing 500 reference ID equals the log
`request_id` for direct correlation.

---

## Testing

Standard-library `unittest` — no test framework needed:

```bash
python -m unittest test_phase1 test_phase2 test_phase2b test_phase3 -v
```

Coverage spans concurrency safety, auth (cookies, password hashing, rate limit),
SSRF allow-list matching, DPAPI wrap/migration (Windows), and logging.

---

## Project layout

```
networker_dashboard.py   # the application (single file)
smb_terminal_v4_Enc2.py  # companion utility
test_phase1.py           # concurrency / lifecycle tests
test_phase2.py           # auth tests
test_phase2b.py          # SSRF + DPAPI tests
test_phase3.py           # logging tests
requirements.txt
docs/superpowers/        # design specs + implementation plans per phase
data/   .certs/   logs/  # runtime state (git-ignored)
```

---

## License

[MIT](LICENSE) © 2026 EXAMPLE-CORP
