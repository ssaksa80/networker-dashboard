# DEDB-style deployment: self-contained bundle + NSSM service

Approved 2026-07-19. Ports the deployment logic of the DEDB project
(C:\dev\DExDashBoard\deploy) to the NetWorker Dashboard.

## Goals

- Zero host prerequisites: the bundle carries an embeddable CPython
  runtime and nssm.exe — nothing to install on the target.
- Real Windows service (NSSM) instead of a scheduled task: SCM
  lifecycle, crash auto-restart, stdout/stderr capture with rotation,
  optional service account.
- Safe upgrades: pre-upgrade backup, health-gated verified start,
  automatic rollback, keep the 3 newest backups.
- Deploy logic lives INSIDE the bundle (DEDB/FMD stale-launcher
  lesson); Setup-NWDash.cmd becomes a thin bootstrap.

## Bundle layout (`nwdash-bundle-<version>-win-x64.zip`)

```
app/                     launcher + nwdash package (current allow-list)
runtime/python/          python.org embeddable CPython (x64), SHA-pinned download, cached in deploy/.cache
runtime/nssm.exe         SHA-pinned download, cached
deploy/install.ps1       installer (below)
deploy/lib/common.ps1    shared functions ported from DEDB
VERSION                  plain-text version
```

Built by `deploy/build-bundle.ps1` (replaces scripts/build-bundle.ps1;
two-way app allow-list retained). `-SkipRuntimeFetch` reuses the cache
for air-gapped build hosts. Optional `cryptography` wheel: installed
into the embedded runtime at build time via `pip --target` so the
target needs no pip.

## install.ps1 (runs from inside the unpacked bundle)

Params: `-InstallDir` (default C:\apps\networker-dashboard), `-Port`
(8443), `-BindHost` (optional single-interface pin), `-ServiceAccount/
-ServicePassword` (default LocalSystem), `-AuthPassword`,
`-AllowedHosts`, `-Check` (dry-run: print resolved plan, change
nothing), `-Upgrade`, `-Rollback`, `-Uninstall [-Purge]`,
`-NoFirewall`.

Fresh install: self-elevate (UAC) → copy app/ + runtime/ into
InstallDir → write service via nssm (`AppDirectory=InstallDir`,
program = runtime\python\python.exe, args = networker_dashboard.py
--port N [--bind-host …] --no-launch; AppStdout/AppStderr into logs\
with AppRotateFiles/AppRotateBytes; AppExit Restart) → firewall rule
for the port → verified start (below) → print URL + service summary.

Upgrade: suppress nssm auto-restart during the swap (DEDB resurrection
gotcha) → clean stop with the STOP_PENDING guard (all nssm calls
try/catch-wrapped; PS 5.1 stderr-throw lesson, DEDB PR #144) → backup
current app/ + runtime/ to backups\<version>-<timestamp>\ (keep 3) →
remove old app/+runtime/ (data\, .certs\, logs\, backups\ untouched)
→ copy new → re-enable auto-restart → verified start → on health
failure: dump the last lines of the service err log, auto-rollback to
the backup, verified start again, exit non-zero.

Rollback: same swap using the newest backup.

Migration from the scheduled-task install: if the NetWorkerDashboard
scheduled task exists, stop + unregister it (elevated), keep data\ and
.certs\ in place, then proceed as fresh install into the same dir.

Verified start (`Start-NwdashServiceVerified`): nssm start → wait for
SERVICE_RUNNING → poll /api/health over HTTPS using a COMPILED
trust-all delegate (DEDB 1.39.0 lesson: scriptblock callbacks break) →
probe host follows BindHost (co-tenant :443 lesson: never probe or
kill by bare port). nssm output read with the UTF-16 decode helper
(FMD Set-FmdCert lesson).

## Setup-NWDash.cmd (bootstrap only)

Keeps: latest-release detection/download (gh/GITHUB_TOKEN, -NoUpdate),
progress bars for unpack. New behavior: unpack the bundle zip to a
temp staging dir and exec `deploy\install.ps1` from INSIDE it,
forwarding arguments. All service/upgrade logic moves out. Scheduled
task code path deleted (install.ps1 migrates existing task installs).

## Out of scope (YAGNI)

DEDB's DB backup/migration layers (no SQL here), patch tier (bundle is
small), Linux bundles, code-update.ps1 equivalent (install.ps1
-Upgrade covers it).

## Tests / verification

- Suite stays green; bundle build produces the new layout; zip entries
  forward-slash (existing lesson).
- Sandbox e2e (non-elevated paths): -Check dry-run output; build →
  unpack → install.ps1 fails cleanly without admin for service ops.
- Elevated-path validation on a real host is documented as the
  operator step (same caveat as the scheduled-task version).
- All nssm interactions unit-testable pieces live in lib/common.ps1
  with the DEDB test patterns (nssm-env-decode / stop-guard) ported
  where applicable.
