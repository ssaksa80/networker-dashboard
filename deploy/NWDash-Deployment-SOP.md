# NetWorker Dashboard — Production Deployment SOP

**Audience:** Windows server / IT operations.
**Scope:** Deploy and operate the NetWorker Dashboard as a Windows service on a
production host, monitoring one or more Dell NetWorker servers.
**Outcome:** A hardened, HTTPS-only, auto-restarting service
(`NetWorkerDashboard`); password-gated dashboard; TV wall display; scheduled
email notifications; all state under `data\` surviving every upgrade.

> Companion documents: [`INSTALL.md`](INSTALL.md) is the full offline
> install/upgrade reference; [`../RELEASING.md`](../RELEASING.md) is how a
> release is cut; [`NWDash-User-Guide.md`](NWDash-User-Guide.md) is for end
> users. This SOP is the operations view.

---

## 0. Architecture (what you are operating)

```
Browser / TV ──HTTPS:8443──► NetWorkerDashboard service (nssm)
                              └─ embedded Python 3.12 (bundled, zero host prereqs)
                                  ├─ /            dashboard SPA (login required)
                                  ├─ /tv          TV wall-display mode
                                  ├─ /view/<tok>  read-only share links
                                  ├─ /api/*       session + CSRF protected
                                  └─ outbound ──► Dell NetWorker REST API
                                                  (or NWUI :9090 fallback)
                                                  + optional WMI host health
                                                  + optional SMTP relay (email)

C:\apps\networker-dashboard\
  networker_dashboard.py, nwdash\   app         (replaced on upgrade)
  runtime\python\                   runtime     (replaced on upgrade)
  nssm.exe                          service mgr (pinned)
  data\  .certs\  logs\  backups\   PERSISTENT  (never touched by upgrades)
```

One Python process, standard library only (plus the bundled optional
`cryptography` for encrypted credential storage). Single instance per install
— the login rate limiter and the schedulers are in-process.

---

## 1. Prerequisites (gather before starting)

| Item | Notes |
|---|---|
| Windows Server (2019/2022) | Target host. Nothing needs preinstalling — Python and nssm ship in the bundle |
| The two release assets | `Setup-NWDash.cmd` + `nwdash-bundle-<ver>-win-x64.zip`, side by side; SHA-256 in the release notes |
| NetWorker reachability | The host must reach the NetWorker server's REST API (and/or NWUI on 9090 for older servers) |
| Dashboard password | Chosen up front; passed once as `-AuthPassword` (stored as a salted PBKDF2 hash, plaintext then removed from the service env) |
| Service account | Optional (`-ServiceAccount CORP\svc-nwdash`); default LocalSystem. Needed only if WMI health or SMTP require a domain identity |
| TLS certificate | Optional PEM cert+key for the dashboard's own HTTPS; default is a self-signed cert generated on first boot |
| Free HTTPS port | Default **8443**; co-tenant hosts additionally need a dedicated IP for `-BindHost` (see §3) |
| SMTP relay | Optional, for email notifications; configured later in the app |

---

## 2. Install SOP

1. Copy both assets into one folder on the host.
2. **Dry-run first** — prints the full plan (mode, dirs, service command line,
   probe URL, firewall), changes nothing, needs no admin:

   ```
   Setup-NWDash.cmd -Check
   ```

3. Install (elevated; UAC is requested automatically):

   ```
   Setup-NWDash.cmd -AuthPassword <pw> [-Port 8443] [-BindHost <ip>] [-AllowedHosts <cidrs>]
   ```

   On an air-gapped host add `-NoUpdate` (skips the GitHub latest-release
   check; the check is fail-soft either way).

4. The installer migrates any **legacy scheduled-task install** automatically
   (task removed, service registered, `data\` untouched), opens the firewall
   (rule group `NetWorkerDashboard`), starts the service, and **health-gates**
   it — it only reports OK once `https://<host>:<port>/api/health` answers.

5. Verify: browse `https://<host>:8443/`, log in with the dashboard password,
   connect to a NetWorker server.

---

## 3. Co-tenant hosts (a second HTTPS app on the same server)

If another app already serves HTTPS on this host, pin the dashboard to its own
IP: `-BindHost 192.0.2.10`. A wildcard bind is exclusive on Windows and will
collide. The installer's health probe and every later upgrade follow the
pinned IP automatically (upgrades read port/bind from the live service's own
command line, so they can never silently un-pin it). Never run unscoped
port checks/kills on such a host — scope to the dashboard's IP or to
processes under the install dir.

---

## 4. Service management

| Task | Command |
|---|---|
| Status | `Get-Service NetWorkerDashboard` (or services.msc) |
| Restart | `C:\apps\networker-dashboard\nssm.exe restart NetWorkerDashboard` |
| Stop / start | `nssm.exe stop NetWorkerDashboard` / `nssm.exe start NetWorkerDashboard` |
| Logs | `logs\service.out.log` / `logs\service.err.log` in the install dir — nssm-rotated at **10 MB** |
| Change port/cert/bind | `nssm.exe set NetWorkerDashboard AppParameters "networker_dashboard.py --port <p> [--bind <ip>] [--cert <pem> --key <pem>] --no-launch"` then restart. The service command line is the source of truth — upgrades preserve it |
| Service env | `nssm.exe get NetWorkerDashboard AppEnvironmentExtra` — holds only the non-secret `DASHBOARD_ALLOWED_HOSTS`; the auth password is removed after first healthy boot |

The service auto-starts at boot and auto-restarts on crash (nssm
`AppExit Restart`). During deploys the installer temporarily suppresses
auto-restart so a mid-swap kill cannot resurrect the old process, and
guarantees it is re-enabled on every exit path.

---

## 5. Upgrade / rollback SOP

**Upgrade** (config + data preserved; see `INSTALL.md` §7 for full detail):

1. Place the new bundle next to `Setup-NWDash.cmd` (connected hosts:
   the bootstrap self-fetches the newest release when `gh`/`GITHUB_TOKEN` is
   available).
2. Verify the zip's SHA-256 against the release notes.
3. Run `Setup-NWDash.cmd -Upgrade` from an elevated prompt.
4. It backs up app+runtime to `backups\<ver>-<timestamp>\`, swaps, restarts,
   and health-gates (`/api/health`, consecutive-probe rule, ~150 s budget).
   **Healthy** → backups pruned to newest 3. **Unhealthy** → prints the log
   lines written by this deploy and **auto-rolls-back**; the service is never
   left down.
5. Tell users to hard-refresh once (Ctrl+F5).

**Rollback** (manual, e.g. a healthy-but-wrong release):

```
Setup-NWDash.cmd -Rollback
```

Restores the newest backup; the rolled-away state is kept as an `undone-*`
backup that can never be re-selected by a second `-Rollback`.

**Never touched by either path:** `data\`, `.certs\`, `logs\`, `backups\`.

---

## 6. Backups (what to protect)

| Location | Contents | Notes |
|---|---|---|
| `data\` | Everything that matters: auth hash, encrypted sessions/profiles/schedules (`automations.json`, `profiles.json`, `email_*.json`), snapshots, DPAPI-wrapped keys (`.session_key`, `.auth_key`) | Back up to a **restricted** location. The keys are **DPAPI machine/account-bound** — a copy restored to a different host/account is unreadable; the app then warns loudly, regenerates keys, and stored passwords must be re-entered once |
| `.certs\` | TLS material (self-signed by default) | Regenerable if lost (self-signed case) |
| `backups\` | Installer's own app+runtime backups (newest 3) | Managed automatically |
| `logs\` | Service logs (10 MB rotation) | No secrets by design; still restrict |

There is no database — file copies of `data\` while the service is stopped
are a complete backup.

---

## 7. Security notes

- **HTTPS-only**, self-signed default; real PEM cert+key via the service
  command line (§4). HSTS/CSP headers are set by the app.
- **Auth:** single dashboard password, salted **PBKDF2** hash; HMAC-signed
  HttpOnly session cookie (`SameSite=Strict; Secure`); login rate limiting.
- **CSRF:** stateless synchronizer token (HMAC of the session payload)
  enforced on every state-changing route.
- **Outbound TLS verify default-on** (fail-closed) per connection profile —
  disabling it is a per-profile, deliberate act in the UI.
- **Allow-list:** `DASHBOARD_ALLOWED_HOSTS` / `-AllowedHosts` restricts which
  NetWorker hosts/CIDRs the app will connect out to.
- **Secrets:** NetWorker/SMTP passwords encrypted at rest (DPAPI-bound keys);
  the API masks them, they never return to a browser; the install-time
  password never persists in plaintext.

---

## 8. Monitoring

- **Health endpoint:** `https://<host>:<port>/api/health` — the same endpoint
  the installer's gate uses. Poll it from your monitoring platform; any
  non-200/timeout is actionable.
- **Service state:** `NetWorkerDashboard` should be `Running`; nssm restarts
  it on crash, so a repeated flap (check `service.err.log`) means a real
  fault, not a blip.
- **Logs:** `logs\service.err.log` is the first read for any incident — the
  installer's failure dump prints exactly the lines a failed deploy wrote.
- **Email schedule health:** each saved schedule shows a status chip (live /
  reconnectable / waiting) and its last send result in the Email dialog —
  "waiting" with no usable connection ever recorded is a config gap, not a
  fault.

---

## 9. TV mode (wall display)

Point the wall display's browser at `https://<host>:<port>/tv` and log in
once on that device. The page auto-sizes (1080p–8K), never scrolls, follows
the saved dashboard theme, reconnects automatically (LIVE/OFFLINE indicator),
and auto-cycles the jobs table every 15 s. One click on the jobs table
minimizes it (remembered across reloads). Since v2.13.0 there is **no
tokenized no-login TV URL** — displays still pointed at `/tv/<token>` must be
re-pointed.

---

## 10. Known limitations (accept or mitigate)

- **Single instance:** rate limiter and schedulers are in-process — no
  replicas.
- **Shared password, no user accounts:** one dashboard password gates
  everything; share links and the TV page are the only reduced-privilege
  surfaces.
- **Headless screenshot rendering** (the emailed dashboard image) needs a
  local Chrome or Edge on the host; on some managed hosts Edge silently
  refuses `--screenshot` while Chrome works. Email falls back to HTML-only.
- **DPAPI key portability:** `data\` cannot be moved between machines or
  service accounts with its secrets intact (§6).
