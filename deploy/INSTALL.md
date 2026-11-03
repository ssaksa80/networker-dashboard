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
   authenticated `gh` CLI or a `GITHUB_TOKEN` env var — and **says** when one
   exists. It downloads it only with **`-Update`**: into a temporary folder,
   and it moves the zip next to the script only if its SHA-256 matches both
   GitHub's digest for the asset and the `Bundle SHA-256` line of the release
   notes. Otherwise it says why and uses the local bundle. Both hashes come
   from the same GitHub release, so this catches a corrupted or altered
   download, not a forged release. The check is **fail-soft**: offline or
   credential-less hosts fall back to the local bundle; it never blocks an
   install. Pass **`-NoUpdate`** to skip the check entirely (air-gapped hosts —
   saves the timeout).
2. Unpacks the newest local bundle to a temp staging folder. A zip with an
   entry that would land outside that folder (`..\`, an absolute path) is
   refused before anything is unpacked.
3. Hands off to the installer **inside** the bundle (`deploy\install.ps1`),
   forwarding all other arguments unchanged.

The installer registers a real Windows service **`NetWorkerDashboard`**
(services.msc) via the bundled nssm, running the bundled embedded Python —
crash auto-restart, starts at boot, stdout/stderr logs with 10 MB rotation.
It ends with a **health gate**: the install only reports OK once
`https://<host>:<port>/api/health` answers.

```
Setup-NWDash.cmd                              fresh install / migrate legacy / reinstall
Setup-NWDash.cmd -Check                       dry-run: print the plan only
Setup-NWDash.cmd -Upgrade                     upgrade (config + data kept)
Setup-NWDash.cmd -Rollback                    restore the newest backup
Setup-NWDash.cmd -Uninstall [-Purge]          remove service [+ data]
Setup-NWDash.cmd -InstallDir D:\apps\nwdash   custom install directory
Setup-NWDash.cmd -Port 9443                   custom HTTPS port
Setup-NWDash.cmd -BindHost 192.0.2.10         pin ONE interface (co-tenancy)
Setup-NWDash.cmd -AuthPassword <pw>           set the password unattended (else you are asked)
Setup-NWDash.cmd -AllowUnauthenticated        run with NO password (isolated networks only)
Setup-NWDash.cmd -AllowedHosts 198.51.100.0/24  NetWorker host allow-list
Setup-NWDash.cmd -FirewallProfile Any         open the port on Public networks too
Setup-NWDash.cmd -Update ...                  download a newer release (SHA-256 verified)
Setup-NWDash.cmd -NoUpdate ...                skip the release check
```

Unattended installs can call the in-bundle installer directly:
`powershell -ExecutionPolicy Bypass -File <unpacked>\deploy\install.ps1 <args>`
(same parameters; it self-elevates and **forwards its full argument list**
through the UAC relaunch, so an elevated child never silently runs defaults).
The exception is the passwords: `-AuthPassword` and `-ServicePassword` are not
put on the elevated command line, so the elevated window asks for them again.
Run unattended installs from an elevated prompt (or as SYSTEM), where there is
no relaunch.

### Install layout

```
C:\apps\networker-dashboard\        (default -InstallDir)
  networker_dashboard.py  nwdash\   application (replaced on upgrade)
  runtime\python\                   embedded CPython (replaced on upgrade)
  nssm.exe                          service manager (pinned, reused if locked)
  data\  .certs\  logs\  backups\   contents NEVER touched by upgrade/rollback
```

The service's working directory is the install dir, so `data\`, `.certs\`
and `logs\` resolve beside the launcher — exactly like the legacy
scheduled-task install, which is why migration preserves everything.

### Folder permissions

The install folder does **not** inherit from `C:\`, because there every user
can read and, on client editions, change what is underneath. Anyone who can
read `data\.session_key` or `data\.auth_key` on this host can unwrap them, so
every install, reinstall, `-Upgrade` and `-Rollback` sets:

| Folder | SYSTEM, Administrators | Service account | Other users |
|---|---|---|---|
| `data\`, `.certs\`, `logs\` | Full control | Modify | None |
| `backups\` | Full control | None | None |
| The app: `nwdash\`, `runtime\`, `nssm.exe`, the launcher | Full control | Read | Read |

Anything else found in the install folder gets the `data\` row, so the service
keeps its access and nobody else has any. Administrators become the owner of
everything, because an owner can always rewrite an access list. The lock
replaces each list outright: entries left by an earlier service account or
added by hand are removed.

What this means day to day:

- **Open `logs\`, `data\` and `.certs\` from an elevated prompt.** If Explorer
  offers *"You don't currently have permission… Continue"*, choose **Cancel**:
  **Continue** permanently adds your account to the folder's access list. The
  next `-Upgrade` removes that entry.
- **Starting the app by hand** needs an elevated prompt too. Otherwise it stops
  with `data\.session_key could not be read by this account` and changes nothing.
- **`-Check` from a normal prompt** cannot read `data\`, so it reports the
  password state as *not known*. Run it elevated to see it. A real run decides
  after the UAC prompt.
- **Keep `--cert` and `--key` in `.certs\` or in a folder of their own.** The TLS
  tab writes the new pair and the `*.previous` copies beside them, and those take
  that folder's permissions, so a key in the root of the install folder would end
  up readable by users. A folder outside the install keeps whatever permissions
  you gave it: restrict it yourself, and let the service account write there.
- **Changing the service account** is safe: re-run the install with
  `-ServiceAccount <account>` (and `-ServicePassword`; see
  [Re-running the install](#re-running-the-install)). The keys are bound to
  this machine, not to an account, and the new account gets the row above.
- **`-InstallDir`** must be a folder of its own: a drive root, or a system folder
  such as `C:\Windows`, is refused before anything changes. An existing install
  in such a place keeps working, and `-Upgrade` warns that it is not locked.
- **Installers from before this change** read `data\` before they ask for
  elevation, so run an older bundle's installer from an elevated prompt.

The admin console's **Health → self-test** has a **Folder permissions** step that
fails if other local users can read the keys, the TLS key, `data\` or `logs\`, or
can change the app, for example after restoring from a backup.
`Setup-NWDash.cmd -Upgrade` puts the lock back.

### `-Check` dry-run

`-Check` needs no admin rights and changes nothing. It prints the fully
resolved plan: bundle version, detected mode (fresh install / reinstall /
legacy-task migration / upgrade / rollback / uninstall), install dir, service
account, exact service command line, port/bind, health-probe URL, firewall
plan, folder permissions, and any existing backups. Run it before every first
install on a new host — it tells you what the real run would do.

### First install — set the dashboard password

Run `Setup-NWDash.cmd` without a password. After UAC, the installer asks for
one (hidden, typed twice) before it changes anything. For an unattended install
from an elevated prompt:

```
Setup-NWDash.cmd -AuthPassword <choose-a-password>
```

The installer stores a **salted PBKDF2 hash** in `data\auth.json` itself,
before the service first starts. The plaintext never goes into the service
environment (the registry) or onto a command line the installer starts: the
bootstrap hands it to the installer in its environment, and a UAC relaunch
asks for it again rather than passing it on. A password typed on the
`Setup-NWDash.cmd` command line is still in that command's own command line
and history, which is why the prompt is the recommended way. Passwords with
spaces or double quotes need the prompt, because `cmd.exe` splits them.

A password is **required** unless the dashboard is bound to loopback
(`-BindHost 127.0.0.1`): with no password typed or given and no stored
`data\auth.json`, an unattended (non-interactive) install refuses before it
changes anything, and the
app itself refuses to start on a network bind without one. `-Upgrade` checks
the installed service the same way before stopping it. To run without a
password on an isolated network anyway, pass `-AllowUnauthenticated`: the
installer then sets `DASHBOARD_ALLOW_UNAUTHENTICATED=1` in the service
environment, and anyone who can reach the port can read backup data and change
the configuration.

### Re-running the install

Running `Setup-NWDash.cmd` without `-Upgrade` on a host where the
`NetWorkerDashboard` service already exists reinstalls the service. Anything the
command line leaves out is taken from the installed service, read the way
`-Upgrade` reads it:

- the port and the `-BindHost` pin;
- the service environment, including the `-AllowedHosts` allow-list, the
  `-AllowUnauthenticated` setting and any variable added by hand. A
  `DASHBOARD_AUTH_PASSWORD` left there by an earlier install is dropped, because
  the app would reset the password from it at every start;
- the account the service logs on as.

What you pass wins, so a re-run changes only what you name. The firewall rule
is made again for `-FirewallProfile`, which is Domain and Private networks
unless you pass it (see [Firewall](#firewall)), so a rule an older version opened
on every network is narrowed. For example,
`Setup-NWDash.cmd -AuthPassword <password>` adds a password and keeps the rest.
To un-pin the bind, pass `-BindHost 0.0.0.0`. `-Check` lists what would be kept.
The install log names it too, before the old service is removed.

Windows never gives a service account's password back. A service that logs on
as a named account (not LocalSystem, LocalService or NetworkService) is
reinstalled only with `-ServicePassword <its password>`, or with
`-ServiceAccount` and `-ServicePassword` for another account. Without them the
installer stops before it changes anything. It also stops if the service's
settings cannot be read. To update the app and keep the service exactly as it
is, use [`-Upgrade`](#7-upgrade).

### Firewall

The installer opens the port inbound in the rule group `NetWorkerDashboard`, on
networks Windows classifies as **Domain** or **Private** only. A network
classified **Public** is how Windows marks one it does not trust, such as an
unidentified network or a guest network, and a dashboard that controls backup
reporting should not answer there. When a connected network is Public the
install log and `-Check` say so, with the network and adapter name.

- `-FirewallProfile Any` opens the port on every network, as versions before
  this did. Any of `Domain`, `Private` and `Public` can also be listed, for
  example `-FirewallProfile Domain`.
- A server on a trusted network that Windows still shows as Public (common on
  a fresh install or a network with no domain controller) can be reclassified
  instead: `Set-NetConnectionProfile -InterfaceAlias <adapter> -NetworkCategory Private`.
- `-NoFirewall` leaves the firewall alone. `-Upgrade` and `-Rollback` never
  touch the rule, so an existing rule stays as it is until the next install
  without `-Upgrade`.

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
- **Real certificate, from the browser:** open `/admin` → **TLS**, paste or
  load the PEM certificate (intermediates after it) and its key (no
  passphrase), and choose **Check and use**. The key is checked against the
  certificate, and the dates and names against the address you opened the
  console with, before anything is replaced. New connections get it straight
  away, and it is written over the files the service started with, so it
  survives restarts and upgrades. The pair it replaced is kept beside them as
  `*.previous`, with a **Put it back** button. If browsers then refuse the
  dashboard altogether, copy the two `*.previous` files back over the
  originals in `.certs\` and restart the service. `*.previous` includes the
  old private key, so if you're replacing a leaked key, delete both files once
  the new certificate works. The service account needs write access to the
  certificate, the key and their folder. If an upload on v3.1.0 says "The
  certificate files could not be written", copy the certificate's `.previous`
  file back over the certificate before the service next restarts: that
  version can replace the certificate without its key, and that pair won't
  start. Later versions leave both files as they were. A `.pfx` must be
  converted first:
  `openssl pkcs12 -in site.pfx -nokeys -out site.crt` and
  `openssl pkcs12 -in site.pfx -nocerts -nodes -out site.key`.
- **Real certificate, on the host:** place PEM cert + key on the host and run the app with
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

(On a host with `gh` auth or `GITHUB_TOKEN`, `Setup-NWDash.cmd -Update
-Upgrade` fetches the newest published bundle first and verifies its SHA-256.
Plain `-Upgrade` only says when a newer one exists.)

What `-Upgrade` does, in order:

1. Reads port/bind from the **existing service's own command line** (the
   upgraded service reuses the same config — flags you pass are ignored here).
2. Applies the [folder permissions](#folder-permissions) for the account the
   service runs as, with the service still running. An install from before
   this lock gets it here, with its keys unchanged and no re-entry of any
   password. If this fails the upgrade stops here, with the service still up.
3. Suppresses nssm auto-restart for the swap window, stops the service, and
   waits for a **clean stop** (SCM settled, port released).
4. **Backs up** the current app + runtime to
   `backups\<version>-<timestamp>\` before touching anything.
5. Swaps in the new app + embedded runtime. **What is in `data\`, `.certs\`,
   `logs\` and `backups\` is never touched** — sessions, profiles, schedules,
   keys, snapshots, and certificates all survive. If the swap itself fails (a
   file stays locked past the ~60 s retry window, the disk fills), everything
   already moved goes back, the old version is restarted, and the installer
   exits non-zero with the cause.
6. Restarts and runs the **health gate**: several consecutive healthy probes
   of `/api/health` required (a flapping bind cannot pass), ~150 s budget.
7. **Healthy** → prunes backups to the newest 3 and reports OK.
   **Unhealthy** → prints exactly the service-log lines written **by this
   deploy** (not stale history), **auto-rolls-back** to the pre-deploy
   backup, restarts the old version, and exits non-zero. The service is left
   down only if the old files cannot be put back either (a lock that outlasts
   the retries): a half-restored app must not run, so it stays stopped and the
   error names the backup folder to finish from by hand.

After upgrading, users should **hard-refresh the browser once (Ctrl+F5)** —
several past releases looked broken purely because of a cached page.

### Rollback

```
Setup-NWDash.cmd -Rollback
```

Restores the newest backup under `backups\`. The rollback first moves the
current (bad) state aside as an `undone-*` backup — which is **excluded**
from future rollback candidates, so a second `-Rollback` can never restore
the build you just rolled away from. Config and data are untouched. The
restored files take the current folder permissions, so rolling back to a build
from before the lock does not reopen the install. A rollback that fails is
undone the same way as an upgrade: the version you were running goes back, and
the backup it was restoring gets its files back.

The backup a rollback restores is used up: its files become the install and
its emptied folder is removed, so a second `-Rollback` goes one backup further
back, and with none left it says so and stops nothing. An emptied folder that
a rollback by an earlier release left in `backups\` is skipped, and the next
successful `-Upgrade` removes it.

Before the service is stopped, the rollback checks that the backup holds the
launcher (`networker_dashboard.py`), the `nwdash` package and
`runtime\python\python.exe`. If any is missing it refuses, with the service
still running and no file moved. The error names the folder and what it
lacks; move that folder out of `backups\` and `-Rollback` reaches the one
before it. `-Check -Rollback`, from an elevated prompt, shows which backup a
rollback would use and whether it would be refused.

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
| Service stops at boot with `data\.session_key exists but is unusable` after copying `data\` to a new machine | Expected. `data\.session_key` / `data\.auth_key` are **DPAPI-protected per machine**, so a copy is unreadable on another host. The app refuses to start rather than replace them, because a new key would orphan every saved password. Restore the original `data\`, or, accepting that every stored secret is lost, delete those two files and re-enter the credentials. Never copy `data\` between machines expecting secrets to survive. |
| `data\.session_key could not be read by this account` when starting the app by hand | The folder lock is doing its job, and nothing was changed. Start the service, or run from an elevated prompt (see [Folder permissions](#folder-permissions)). |
| `Access is denied` opening `logs\`, `data\` or `.certs\` | Expected: use an elevated prompt. In Explorer choose **Cancel** rather than **Continue** (see [Folder permissions](#folder-permissions)). |
| Install or `-Upgrade` stops with `could not restrict the install folder` | The volume may not support permissions (FAT/exFAT), or security software may be holding the folder. On `-Upgrade` the service was left running. Fix the cause and re-run. |
| Health self-test fails **Folder permissions** | Other local users can reach what the step names, usually after restoring from a backup or copying files in. `Setup-NWDash.cmd -Upgrade` restores the lock. |
| Port already in use / service flapping | The installer warns at install time if a foreign process owns the port (name + PID). Stop it or re-run with `-Port <other>`. On co-tenant hosts, check `-BindHost` is set (see §5). |
| Install reports OK but browser can't reach the dashboard | Firewall: the installer opens inbound TCP on the chosen port (rule group `NetWorkerDashboard`) on Domain and Private networks, unless `-NoFirewall` was passed. If the install log warned that a network is classified Public, see [Firewall](#firewall). Also confirm you're browsing the bound IP when `-BindHost` is pinned. |
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
  (`data\.session_key`, `data\.auth_key`) are **DPAPI machine-bound** on
  Windows, so a copy is unreadable on another host. Machine-bound also means
  any account on this host can unwrap a key file it can read, which is why the
  install folder is locked: see [Folder permissions](#folder-permissions). Keep
  copies of `data\` somewhere equally restricted, because a copy elsewhere on
  this host is not protected by that lock. The API always masks stored passwords
  — they are never returned to the browser.
- **The install password never persists in plaintext**: the installer stores
  only its salted hash, and the plaintext never enters the service
  environment. `-Upgrade` removes a plaintext `DASHBOARD_AUTH_PASSWORD` that
  an earlier version left there after a failed install.
- **Outbound TLS verification is fail-closed**: on by default per connection
  profile; restored sessions missing the flag default to verify.
- **Allow-list**: `-AllowedHosts` (or `DASHBOARD_ALLOWED_HOSTS`) restricts
  which NetWorker hosts/CIDRs the dashboard will connect out to.
- **Bundle integrity**: runtime downloads are SHA-256 pinned at build time;
  the release notes publish the bundle's SHA-256 for verification on the
  target (§3).
