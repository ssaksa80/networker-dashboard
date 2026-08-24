# NetWorker Dashboard — Offline Deployment Guide

End-to-end guide for deploying the NetWorker Dashboard to an **air-gapped**
Windows target host using a self-contained bundle. No internet access and no
pre-installed runtime are required on the target.

> Companion documents: [`NWDash-Deployment-SOP.md`](NWDash-Deployment-SOP.md)
> covers day-to-day operations (service management, monitoring, backups);
> [`../RELEASING.md`](../RELEASING.md) covers how a bundle gets cut in the
> first place. This guide is self-contained for the **bundle-based offline**
> path.

---

## 1. Overview

Deployment is **two phases**:

1. **Build (online):** on an internet-connected host, assemble a single zip
   that contains everything the target needs — embedded CPython runtime,
   nssm.exe, the application, and the installer itself.
2. **Install (offline / air-gapped):** copy the zip to the target, run the
   bootstrap (or the in-bundle installer directly). Zero downloads happen on
   the target.

```
  +------------------------------+         +------------------------------+
  |   PHASE 1 - BUILD HOST       |         |   PHASE 2 - TARGET HOST      |
  |   (internet-connected)       |         |   (air-gapped / offline)     |
  |                              |         |                              |
  |  deploy\build-bundle.ps1     |         |  Setup-NWDash.cmd            |
  |    +- stage app (allow-list) |  USB /  |    +- finds newest bundle    |
  |    +- fetch CPython 3.12     |  SCP /  |    +- unpacks to temp        |
  |    |  (SHA-256 pinned)       | ------> |    +- hands off to the       |
  |    +- fetch nssm 2.24        |  share  |       IN-BUNDLE installer    |
  |    |  (SHA-256 pinned)       |         |                              |
  |    +- cryptography wheel     |         |  deploy\install.ps1          |
  |                              |         |    +- installs NSSM service  |
  |  dist\                       |         |    +- firewall rule          |
  |    nwdash-bundle-<ver>-      |         |    +- health-gates the start |
  |      win-x64.zip             |         |                              |
  +------------------------------+         +--------------+---------------+
                                                           |
                                           +---------------v---------------+
                                           |  EXTERNAL (reachable from     |
                                           |  the target - NOT bundled)    |
                                           |   - Dell NetWorker server(s)  |
                                           |     (REST API / NWUI :9090)   |
                                           |   - SMTP relay (email, opt.)  |
                                           +-------------------------------+
```

### What the bundle contains

| Component | Notes |
|---|---|
| Application | `app\` — launcher + `nwdash\` package, staged from an explicit allow-list (a forgotten module fails the build loudly) |
| Embedded Python 3.12 | `runtime\python\` — python.org embeddable CPython x64, SHA-256 pinned; `python312._pth` pre-adjusted for the install layout |
| `cryptography` wheel | Preinstalled into the embedded runtime's `Lib\site-packages` (encrypted-at-rest credential storage; the app degrades gracefully without it, `-NoCrypto` builds skip it) |
| nssm 2.24 | `runtime\nssm.exe` — service wrapper, SHA-256 pinned |
| Installer | `deploy\install.ps1` + `deploy\lib\common.ps1` — **ships inside the bundle**, so install/upgrade logic can never go stale on the host (the launcher outside the zip is a thin bootstrap only) |
| Metadata | `VERSION` (plain-text version) |

### External prerequisites (NOT bundled)

- **Dell NetWorker server(s)** — the REST API endpoint (and/or the NWUI
  gateway on port 9090 for older servers) must be reachable from the target
  host. Credentials are entered in the dashboard, never at install time.
- **SMTP relay** — only if you use email notifications; configured later in
  the app's Email dialog.
- Optional: **WMI** access to Windows servers you want CPU/memory/disk health
  for.

Everything the **application** itself needs is in the bundle — the target
needs no internet, no Python, no anything preinstalled.

---

## 2. Phase 1 — Build the bundle (internet-connected host)

Build-host prerequisites: Windows, PowerShell, a host Python with pip (used
only to download the `cryptography` wheel into the bundle), git.

```powershell
git clone https://github.com/ssaksa80/networker-dashboard
cd networker-dashboard
pwsh -File deploy\build-bundle.ps1
```

Output: `dist\nwdash-bundle-<ver>-win-x64.zip`. The version is read from
`APP_VERSION` in `nwdash\config.py`.

- Downloads (CPython embeddable zip, nssm zip, cryptography wheels) are
  **SHA-256 pinned** — a download that hashes differently **aborts the
  build** (supply-chain guard) — and cached under `deploy\.cache\` so
  rebuilds are offline-safe.
- `-SkipRuntimeFetch` never touches the network (requires a seeded
  `deploy\.cache`; for air-gapped build hosts).
- `-NoCrypto` ships without the `cryptography` wheel.
- The build **validates the staged runtime** by importing the modules the app
  needs under the real embedded interpreter before packing — a broken runtime
  cannot ship.

---

## 3. Transfer to the target

Copy **two files** to a folder on the air-gapped target by any out-of-band
means (USB, SCP, internal share):

1. `scripts\Setup-NWDash.cmd` (the bootstrap — also attached to every GitHub
   release)
2. `dist\nwdash-bundle-<ver>-win-x64.zip`

Keep them **side by side** — the bootstrap picks the newest
`nwdash-bundle-*-win-x64.zip` next to itself.

Verify the copy against the SHA-256 published in the release notes:

```powershell
Get-FileHash nwdash-bundle-<ver>-win-x64.zip -Algorithm SHA256
```

---

## 4. Phase 2 — Install on the offline target

### One-click install (recommended)

Double-click **`Setup-NWDash.cmd`** (or run it from an elevated prompt) and
approve the UAC prompt. It:

1. **Checks GitHub for a newer published release first** — via an
   authenticated `gh` CLI or a `GITHUB_TOKEN` env var — and downloads a newer
   bundle automatically if one exists. This check is **fail-soft**: offline or
   credential-less hosts silently fall back to the local bundle; it never
   blocks an install. Pass **`-NoUpdate`** to skip the check entirely
   (air-gapped hosts — saves the timeout).
2. Unpacks the newest local bundle to a temp staging folder.
3. Hands off to the installer **inside** the bundle (`deploy\install.ps1`),
   forwarding all other arguments unchanged.

The installer registers a real Windows service **`NetWorkerDashboard`**
(services.msc) via the bundled nssm, running the bundled embedded Python —
crash auto-restart, starts at boot, stdout/stderr logs with 10 MB rotation.
It ends with a **health gate**: the install only reports OK once
`https://<host>:<port>/api/health` answers.

```
Setup-NWDash.cmd                              fresh install / migrate legacy
Setup-NWDash.cmd -Check                       dry-run: print the plan only
Setup-NWDash.cmd -Upgrade                     upgrade (config + data kept)
Setup-NWDash.cmd -Rollback                    restore the newest backup
Setup-NWDash.cmd -Uninstall [-Purge]          remove service [+ data]
Setup-NWDash.cmd -InstallDir D:\apps\nwdash   custom install directory
Setup-NWDash.cmd -Port 9443                   custom HTTPS port
Setup-NWDash.cmd -BindHost 192.0.2.10         pin ONE interface (co-tenancy)
Setup-NWDash.cmd -AuthPassword <pw>           set the dashboard password
Setup-NWDash.cmd -AllowedHosts 198.51.100.0/24  NetWorker host allow-list
Setup-NWDash.cmd -NoUpdate ...                skip the release check
```

Unattended installs can call the in-bundle installer directly:
`powershell -ExecutionPolicy Bypass -File <unpacked>\deploy\install.ps1 <args>`
(same parameters; it self-elevates and **forwards its full argument list**
through the UAC relaunch, so an elevated child never silently runs defaults).

### Install layout

```
C:\apps\networker-dashboard\        (default -InstallDir)
  networker_dashboard.py  nwdash\   application (replaced on upgrade)
  runtime\python\                   embedded CPython (replaced on upgrade)
  nssm.exe                          service manager (pinned, reused if locked)
  data\  .certs\  logs\  backups\   NEVER touched by upgrade/rollback
```

The service's working directory is the install dir, so `data\`, `.certs\`
and `logs\` resolve beside the launcher — exactly like the legacy
scheduled-task install, which is why migration preserves everything.

### `-Check` dry-run

`-Check` needs no admin rights and changes nothing. It prints the fully
resolved plan: bundle version, detected mode (fresh install / reinstall /
legacy-task migration / upgrade / rollback / uninstall), install dir, service
account, exact service command line, port/bind, health-probe URL, firewall
plan, and any existing backups. Run it before every first install on a new
host — it tells you what the real run would do.

### First install — set the dashboard password

```
Setup-NWDash.cmd -AuthPassword <choose-a-password>
```

The app reads `DASHBOARD_AUTH_PASSWORD` once at first boot, persists a
**salted PBKDF2 hash** in `data\auth.json`, and the installer then **removes
the plaintext from the service environment** — it never stays in the
registry. Without a password the dashboard runs unauthenticated; set one on
any host other than a lab box.

### Legacy scheduled-task migration

Versions before 2.8.3 ran as a Windows scheduled task. The installer detects
the legacy task automatically on a fresh install: it stops and unregisters
the task, registers the NSSM service in its place, and leaves `data\` and
`.certs\` where they are — **saved sessions, keys, profiles, and snapshots
survive the migration** with no re-entry. `-Uninstall` removes the legacy
task too, and both paths also stop hand-started `python networker_dashboard.py`
instances (matched by command line, wherever their python came from).

---

## 5. Co-tenant hosts — `-BindHost` port pinning

By default the app binds **all interfaces** (`0.0.0.0`), reserving the port
on every IP. To run the dashboard beside another HTTPS app on the same box,
give each app its **own server IP** and pin the dashboard to one:

```
Setup-NWDash.cmd -BindHost 192.0.2.10 -Port 8443
```

- Windows treats a wildcard bind as **exclusive** against a specific one — an
  unpinned install will collide with the co-tenant even on "different" IPs.
- The installer's **health probe follows the pinned IP automatically** (never
  a localhost probe that a pinned host wouldn't answer), and upgrades read
  the live service's own command line for port/bind, so an upgrade can never
  silently un-pin the service.
- The `-Check` dry-run prints the resolved probe host — confirm it is the
  pinned IP before a real run on a co-tenant host.

---

## 6. TLS certificates

TLS is **always on** — the service is HTTPS-only.

- **Default: self-signed.** First boot writes a development certificate to
  `.certs\` and serves with it. Browsers warn once; import the cert into the
  machine trust store to clear the warning, or supply a real cert.
- **Real certificate:** place PEM cert + key on the host and run the app with
  `--cert`/`--key` — on a service install, edit the service parameters:

  ```powershell
  C:\apps\networker-dashboard\nssm.exe set NetWorkerDashboard AppParameters `
    "networker_dashboard.py --port 8443 --cert C:\certs\nwdash-cert.pem --key C:\certs\nwdash-key.pem --no-launch"
  C:\apps\networker-dashboard\nssm.exe restart NetWorkerDashboard
  ```

  Keep any existing `--bind` argument when editing — the service command line
  is the source of truth for port/bind and upgrades preserve it.
- `.certs\` survives upgrades and is removed only by `-Uninstall -Purge`.
- **Outbound** TLS (dashboard → NetWorker) is separate: verification is on by
  default and controlled per connection profile in the UI ("Verify REST API
  TLS certificate").

---

## 7. Upgrade

Place the new bundle next to `Setup-NWDash.cmd` and run:

```
Setup-NWDash.cmd -Upgrade
```

(On a host with `gh` auth or `GITHUB_TOKEN`, plain `Setup-NWDash.cmd
-Upgrade` also self-fetches the newest published bundle first.)

What `-Upgrade` does, in order:

1. Reads port/bind from the **existing service's own command line** (the
   upgraded service reuses the same config — flags you pass are ignored here).
2. Suppresses nssm auto-restart for the swap window, stops the service, and
   waits for a **clean stop** (SCM settled, port released).
3. **Backs up** the current app + runtime to
   `backups\<version>-<timestamp>\` before touching anything.
4. Swaps in the new app + embedded runtime. **`data\`, `.certs\`, `logs\`,
   `backups\` are never touched** — sessions, profiles, schedules, keys,
   snapshots, and certificates all survive.
5. Restarts and runs the **health gate**: several consecutive healthy probes
   of `/api/health` required (a flapping bind cannot pass), ~150 s budget.
6. **Healthy** → prunes backups to the newest 3 and reports OK.
   **Unhealthy** → prints exactly the service-log lines written **by this
   deploy** (not stale history), **auto-rolls-back** to the pre-deploy
   backup, restarts the old version, and exits non-zero. The service is never
   left down.

After upgrading, users should **hard-refresh the browser once (Ctrl+F5)** —
several past releases looked broken purely because of a cached page.

### Rollback

```
Setup-NWDash.cmd -Rollback
```

Restores the newest backup under `backups\`. The rollback first moves the
current (bad) state aside as an `undone-*` backup — which is **excluded**
from future rollback candidates, so a second `-Rollback` can never restore
the build you just rolled away from. Config and data are untouched.

---

## 8. Uninstall

```
Setup-NWDash.cmd -Uninstall            service + app removed, data kept
Setup-NWDash.cmd -Uninstall -Purge     full wipe, INCLUDING data\ .certs\ logs\
```

Without `-Purge`, `data\` (keys, profiles, schedules, snapshots), `.certs\`
and `logs\` stay in the install dir — a later reinstall picks them straight
back up. `-Purge` deletes everything including the DPAPI-wrapped key files;
saved credentials are unrecoverable after a purge. Both modes also remove the
legacy scheduled task and the firewall rule.

---

## 9. TV mode (wall display)

The dashboard has a purpose-built wall-display mode at **`/tv`**:

1. Install and connect the dashboard normally.
2. On the wall display's browser, open `https://<host>:<port>/tv` and log in
   once.
3. The TV page auto-sizes to any screen (1080p–8K), never scrolls, follows
   the dashboard's saved theme, shows a LIVE/OFFLINE indicator with
   auto-reconnect, and auto-cycles the jobs table (Recent → Failed → Clients,
   15 s). A single click on the jobs table minimizes it and lets the KPI/donut
   blocks grow; the choice is remembered across reloads.

> Since v2.13.0 the TV page is `/tv` (login once on the display). The older
> tokenized `/tv/<token>` no-login URL from 2.10.x was removed with the
> email-engine revert — re-point any wall display still using it.

---

## 10. Troubleshooting

| Symptom | Action |
|---|---|
| Service not starting / health gate failed | Read `logs\service.err.log` (and `.out.log`) in the install dir. The installer already printed the lines written during the failed deploy — the cause is almost always there (port in use, cert path wrong, traceback on boot). |
| `DPAPI key regeneration` warning at startup after copying to a new machine/account | Expected. `data\.session_key` / `data\.auth_key` are **DPAPI-protected per Windows account** — copies are unreadable elsewhere, so the app warns loudly and regenerates. Saved sessions and stored profile/schedule passwords must be re-entered **once**; nothing else is lost. Never copy `data\` between machines expecting secrets to survive. |
| Port already in use / service flapping | The installer warns at install time if a foreign process owns the port (name + PID). Stop it or re-run with `-Port <other>`. On co-tenant hosts, check `-BindHost` is set (see §5). |
| Install reports OK but browser can't reach the dashboard | Firewall: the installer opens inbound TCP on the chosen port (rule group `NetWorkerDashboard`) unless `-NoFirewall` was passed. Also confirm you're browsing the bound IP when `-BindHost` is pinned. |
| Browser TLS warning | Expected with the default self-signed cert — see §6. |
| Dashboard up but NetWorker data empty | Outbound problem, not an install problem: check the NetWorker server address/credentials in the connection dialog, the per-profile "Verify REST API TLS certificate" setting, and any `-AllowedHosts` allow-list you set at install. |
| UI looks broken right after an upgrade | Hard refresh once (Ctrl+F5) — the browser cached the previous release's page. |
| Bootstrap says "no bundle found" | The zip must sit **next to** `Setup-NWDash.cmd` and match `nwdash-bundle-<ver>-win-x64.zip`. On air-gapped hosts also pass `-NoUpdate`. |

---

## 11. Security notes

- **HTTPS-only**, self-signed by default; supply `--cert`/`--key` for
  production (§6).
- **Auth**: single dashboard password → salted **PBKDF2** hash in
  `data\auth.json`; HMAC-signed HttpOnly session cookie
  (`SameSite=Strict; Secure`); login rate limiting; **CSRF** synchronizer
  token enforced on every state-changing route.
- **Secrets at rest**: NetWorker and SMTP passwords are encrypted; the keys
  (`data\.session_key`, `data\.auth_key`) are **DPAPI machine/account-bound**
  on Windows. The API always masks stored passwords — they are never returned
  to the browser.
- **The install password never persists in plaintext**: `-AuthPassword` is
  removed from the service environment right after the first healthy boot.
- **Outbound TLS verification is fail-closed**: on by default per connection
  profile; restored sessions missing the flag default to verify.
- **Allow-list**: `-AllowedHosts` (or `DASHBOARD_ALLOWED_HOSTS`) restricts
  which NetWorker hosts/CIDRs the dashboard will connect out to.
- **Bundle integrity**: runtime downloads are SHA-256 pinned at build time;
  the release notes publish the bundle's SHA-256 for verification on the
  target (§3).
