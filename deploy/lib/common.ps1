## Shared deployment functions for the NetWorker Dashboard.
# Ported from the DEDB project (C:\dev\DExDashBoard\deploy\lib\common.ps1),
# renamed Dedb -> Nwdash and trimmed to what this app needs (no SQL, no patch
# tier, no Node). ASCII only + Windows PowerShell 5.1 safe throughout.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Service + install constants shared by every deploy script.
$script:NwdashServiceName = 'NetWorkerDashboard'
# The application payload at the InstallDir ROOT (the launcher runs with
# cwd = InstallDir so data\, .certs\ and logs\ resolve beside it, exactly as
# the scheduled-task install did). Everything an upgrade may replace is listed
# here; data\, .certs\, logs\ and backups\ are NEVER in this list.
$script:NwdashAppItems = @('networker_dashboard.py', 'nwdash', 'README.md', 'pyproject.toml', '__pycache__')

function Write-NwdashLog  { param([string]$Msg) Write-Host "[nwdash] $Msg" }
function Write-NwdashWarn { param([string]$Msg) Write-Warning "[nwdash] $Msg" }
function Stop-Nwdash      { param([string]$Msg) Write-Error "[nwdash] $Msg"; exit 1 }

function Get-Sha256 { param([Parameter(Mandatory)][string]$Path)
  (Get-FileHash -Algorithm SHA256 -Path $Path).Hash.ToLower()
}

function Test-Sha256 { param([Parameter(Mandatory)][string]$Path,[string]$Expected)
  if (-not (Test-Path $Path)) { Stop-Nwdash "file not found: $Path" }
  if ([string]::IsNullOrWhiteSpace($Expected)) { Write-NwdashWarn "no sha256 pinned for $(Split-Path $Path -Leaf) - skipping verify"; return }
  $got = Get-Sha256 $Path
  # throw (catchable) rather than exit, so callers can branch; build scripts run
  # with $ErrorActionPreference='Stop', so an uncaught throw still aborts.
  if ($got -ne $Expected.ToLower()) { throw "sha256 mismatch for $(Split-Path $Path -Leaf): got $got expected $Expected" }
}

function Invoke-Download { param([Parameter(Mandatory)][string]$Url,[Parameter(Mandatory)][string]$Dest,[int]$Tries=3)
  for ($n=1; $n -le $Tries; $n++) {
    try { Invoke-WebRequest -Uri $Url -OutFile $Dest -TimeoutSec 120 -UseBasicParsing; return }
    catch { Write-NwdashWarn "download failed ($n/$Tries): $Url"; Start-Sleep 2 }
  }
  Stop-Nwdash "could not download $Url (no internet? run build-bundle on a connected host, or re-run with -SkipRuntimeFetch once deploy\.cache is seeded)"
}

# ---- readiness probe (HTTPS /api/health) ----
# The app serves a self-signed cert by default, so the probe installs a
# trust-everything certificate callback and RESTORES the previous callback in
# finally. The callback MUST be a COMPILED delegate, not a PowerShell
# scriptblock: a scriptblock assigned to ServerCertificateValidationCallback is
# invoked by .NET on a TLS-handshake threadpool thread that has NO PowerShell
# runspace, so it throws "There is no Runspace available", the handshake is
# aborted, and the probe returns $false for a perfectly healthy endpoint (DEDB
# 1.39.0: that failed EVERY https probe and rolled back every upgrade). A
# compiled delegate runs on any thread. Defined once per session.
function Get-NwdashTrustAllCertsDelegate {
  if (-not ([System.Management.Automation.PSTypeName]'NwdashCertBypass').Type) {
    Add-Type -TypeDefinition @'
using System.Net;
using System.Net.Security;
using System.Security.Cryptography.X509Certificates;
public static class NwdashCertBypass {
  public static readonly RemoteCertificateValidationCallback TrustAll =
    delegate(object s, X509Certificate c, X509Chain ch, SslPolicyErrors e) { return true; };
}
'@
  }
  return [NwdashCertBypass]::TrustAll
}

function Invoke-NwdashHttpGet {
  param([Parameter(Mandatory)][string]$Url, [int]$TimeoutSec = 5)
  $prevCb = [System.Net.ServicePointManager]::ServerCertificateValidationCallback
  try {
    [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor [System.Net.SecurityProtocolType]::Tls12
    # Trust any cert for THIS probe only (self-signed HTTPS), restored in
    # finally so the rest of the installer session keeps normal validation.
    [System.Net.ServicePointManager]::ServerCertificateValidationCallback = (Get-NwdashTrustAllCertsDelegate)
    return Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec $TimeoutSec
  } catch {
    return $null
  } finally {
    [System.Net.ServicePointManager]::ServerCertificateValidationCallback = $prevCb
  }
}

# Normalise a caller-supplied URL to exactly one /api/health path. Callers pass
# EITHER a bare origin (https://host[:port][/]) OR the full health URL already
# ending in /api/health; both resolve to a single /api/health so we never probe
# /api/health/api/health. Tolerates trailing slashes on either form.
function Resolve-NwdashHealthUrl {
  param([Parameter(Mandatory)][string]$Url)
  $u = "$Url".Trim()
  $u = $u -replace '/+$', ''              # drop any trailing slash(es)
  $u = $u -replace '(?i)/api/health$', '' # drop an already-present /api/health
  $u = $u -replace '/+$', ''              # drop a slash left before it, if any
  return "$u/api/health"
}

# Is the app SERVING? HTTP 200 from /api/health, and nothing else. Returns
# $true/$false, never throws - health-gate loops call it directly.
function Test-NwdashHealth {
  param([Parameter(Mandatory)][string]$Url, [int]$TimeoutSec = 5)
  $resp = Invoke-NwdashHttpGet -Url (Resolve-NwdashHealthUrl $Url) -TimeoutSec $TimeoutSec
  if ($null -eq $resp) { return $false }
  try { return ([int]$resp.StatusCode -eq 200) } catch { return $false }
}

# ---- nssm output decode + service config readers ----

# Normalize the output of `nssm get ...` into matchable text.
#
# nssm writes its `get` output as UTF-16LE. PowerShell decodes that stream a
# byte at a time, so every real character arrives followed by a NUL - INVISIBLE
# in a console, so the output looks perfectly normal when an operator runs it
# by hand. A literal pattern like '--port ' can never match the null-laden
# string, so a config reader silently returns nothing on a host where the value
# IS set (FMD Set-FmdCert lesson; DEDB BIND_HOST lesson: the health probe then
# targeted an interface the app never bound and every deploy rolled back).
# Stripping the NULs recovers the text (the values we read - ports, IPs, paths
# - are ASCII). Already-clean output passes through unchanged. Never throws.
function ConvertFrom-NwdashNssmOutput {
  param([string[]]$Raw)
  if (-not $Raw) { return '' }
  return (($Raw -join "`n") -replace "`0", '')
}

# Parse the launcher command line ('networker_dashboard.py --port 8443
# [--bind 10.0.0.5] --no-launch') into @{ Port; Bind }. Pure + testable; used
# on the text ConvertFrom-NwdashNssmOutput recovers from `nssm get
# NetWorkerDashboard AppParameters`. Missing values fall back to the defaults.
function Get-NwdashServiceArgs {
  param([string]$Parameters = '', [int]$DefaultPort = 8443)
  $port = $DefaultPort; $bind = ''
  if ($Parameters -match '--port\s+(\d+)') { $port = [int]$Matches[1] }
  if ($Parameters -match '--bind\s+(\S+)') { $bind = $Matches[1] }
  return [pscustomobject]@{ Port = $port; Bind = $bind }
}

# Read the installed service's launcher args (port/bind) from nssm. Returns the
# defaults when no service/nssm is readable. Never throws.
function Read-NwdashServiceArgs {
  param([Parameter(Mandatory)][string]$InstallDir, [int]$DefaultPort = 8443)
  $nssm = Join-Path $InstallDir 'nssm.exe'
  $raw = ''
  if (Test-Path $nssm) {
    try { $raw = ConvertFrom-NwdashNssmOutput -Raw (& $nssm get $script:NwdashServiceName AppParameters 2>$null) } catch { $raw = '' }
  }
  return (Get-NwdashServiceArgs -Parameters $raw -DefaultPort $DefaultPort)
}

# The host a same-box health/port probe should target: the pinned bind host
# when one is set (a pinned install does NOT listen on loopback - probing
# 127.0.0.1 would fail health on a healthy app and roll back a good deploy),
# else loopback. '0.0.0.0' means "all interfaces", which includes loopback.
function Get-NwdashProbeHost {
  param([string]$BindHost = '')
  if ($BindHost -and $BindHost -ne '0.0.0.0') { return $BindHost }
  return '127.0.0.1'
}

# ---- port / process probes ----

# Does a listening socket's LocalAddress belong to the instance we care about?
# $Want empty = unscoped (single-service box) -> match any listener. $Want set
# = a pinned bind IP -> match ONLY that exact IP or a wildcard bind. This keeps
# a co-tenant that listens on the same port on a DIFFERENT IP from being
# mistaken for a leftover - and from being force-killed (DEDB/FMD :443 lesson).
function Test-NwdashAddrMatch {
  param([string]$Row, [string]$Want)
  if (-not $Want) { return $true }
  return ($Row -eq $Want -or $Row -eq '0.0.0.0' -or $Row -eq '::')
}

# Who (if anyone) is listening on $Port, scoped to -LocalAddress when pinned.
function Test-NwdashPortInUse {
  param([Parameter(Mandatory)][int]$Port, [string]$LocalAddress = '')
  try {
    $rows = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
      Where-Object { Test-NwdashAddrMatch -Row "$($_.LocalAddress)" -Want $LocalAddress })
    $c = $rows | Select-Object -First 1
    if (-not $c) { return $null }
    $p = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue
    return [pscustomobject]@{ Port = $Port; ProcessId = $c.OwningProcess; ProcessName = $p.Name; Address = "$($c.LocalAddress)" }
  } catch { return $null }
}

# Wait until a local TCP port is FREE (nothing listening) before starting a new
# instance. Returns $true as soon as the port is free, $false if still held at
# the timeout (the caller warns and proceeds). Pure sockets, never throws.
function Wait-NwdashPortFree {
  param([int]$Port, [int]$TimeoutSec = 20, [string]$HostName = '127.0.0.1')
  if ($Port -le 0) { return $true }
  $deadline = (Get-Date).AddSeconds($TimeoutSec)
  while ($true) {
    $held = $false
    $client = $null
    try {
      $client = New-Object Net.Sockets.TcpClient
      $iar = $client.BeginConnect($HostName, $Port, $null, $null)
      if ($iar.AsyncWaitHandle.WaitOne([TimeSpan]::FromSeconds(1))) {
        try { $client.EndConnect($iar); $held = [bool]$client.Connected } catch { $held = $false }
      }
    } catch { $held = $false }
    finally { if ($client) { $client.Close() } }
    if (-not $held) { return $true }
    if ((Get-Date) -ge $deadline) { return $false }
    Start-Sleep -Milliseconds 500
  }
}

# ---- failure-log capture (what did THIS deploy write?) ----

# Byte length of a service log, or 0. Capture BEFORE the stop so a later
# Show-NwdashFailureLog can print ONLY what this deploy actually wrote. Length
# is the right marker, not LastWriteTime (append-open bumps mtime even when
# nothing is logged). Never throws.
function Get-NwdashLogLength {
  param([Parameter(Mandatory)][string]$InstallDir, [Parameter(Mandatory)][string]$Name)
  $log = Join-Path $InstallDir "logs\$Name"
  if (-not (Test-Path $log)) { return 0 }
  try { return [long](Get-Item $log).Length } catch { return 0 }
}

# Read a log from byte offset $From to the end (exactly what this deploy
# appended). Opens read/write-shared so the running app holding the handle
# cannot make this throw. Returns '' when nothing was added. Never throws.
function Get-NwdashLogSince {
  param([Parameter(Mandatory)][string]$InstallDir, [Parameter(Mandatory)][string]$Name, [long]$From = 0)
  $log = Join-Path $InstallDir "logs\$Name"
  if (-not (Test-Path $log)) { return '' }
  $fs = $null
  try {
    $fs = New-Object System.IO.FileStream($log, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
    if ($From -gt 0 -and $From -le $fs.Length) { [void]$fs.Seek($From, [System.IO.SeekOrigin]::Begin) }
    $sr = New-Object System.IO.StreamReader($fs)
    $txt = $sr.ReadToEnd()
    $sr.Close()
    return $txt
  } catch { return '' } finally { if ($fs) { try { $fs.Dispose() } catch { } } }
}

# On a failed health gate, print what THIS deploy actually logged (service
# stdout AND stderr since the deploy started). A healthy boot writes nothing to
# stderr, so "no new stderr" alone proves only "did not crash" - the stdout
# tail carries the app's own bind/listen lines. Never throws.
function Show-NwdashFailureLog {
  param(
    [Parameter(Mandatory)][string]$InstallDir,
    [long]$SinceOut = 0,
    [long]$SinceErr = 0,
    [string]$ProbeUrl = ''
  )
  $out = Get-NwdashLogSince -InstallDir $InstallDir -Name 'service.out.log' -From $SinceOut
  $err = Get-NwdashLogSince -InstallDir $InstallDir -Name 'service.err.log' -From $SinceErr
  if ($ProbeUrl) { Write-NwdashWarn "health probe target was: $ProbeUrl" }
  if (-not $out -and -not $err) {
    Write-NwdashWarn 'the app logged NOTHING to stdout or stderr during this deploy - it may not have started at all.'
  }
  if ($out) {
    Write-NwdashWarn '--- service.out.log (this deploy only) ---'
    ($out -split "`r?`n" | Where-Object { $_ } | Select-Object -Last 20) | ForEach-Object { $_ }
  }
  if ($err) {
    Write-NwdashWarn '--- service.err.log (this deploy only) ---'
    ($err -split "`r?`n" | Where-Object { $_ } | Select-Object -Last 20) | ForEach-Object { $_ }
  } else {
    Write-NwdashWarn 'no new stderr this deploy (a healthy boot writes none - this alone does NOT mean the app is at fault).'
  }
}

# ---- service teardown ----
# nssm registers an event-log source whose EventMessageFile is nssm.exe; the
# Windows Event Log service then holds a handle on nssm.exe, which is why a
# plain delete/overwrite of nssm.exe fails even after the service is stopped.

function Remove-NwdashEventSource {
  foreach ($n in $script:NwdashServiceName, 'nssm') {
    Remove-Item "HKLM:\SYSTEM\CurrentControlSet\Services\EventLog\Application\$n" -Recurse -Force -ErrorAction SilentlyContinue
  }
}

function Stop-NwdashProcesses { param([Parameter(Mandatory)][string]$InstallDir)
  $procs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.ExecutablePath -and $_.ExecutablePath -like "$InstallDir\*" }
  foreach ($p in $procs) {
    try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop; Write-NwdashLog "killed leftover PID $($p.ProcessId) ($($p.Name))" } catch { }
  }
}

# Fully remove the service: stop, deregister, kill stragglers. Prefers the
# installed nssm.exe; falls back to sc.exe. Safe when nothing is installed.
# Every nssm/sc call is try/catch-wrapped: under PS 5.1 with EAP=Stop, native
# stderr (e.g. "Can't open service!", STOP_PENDING) becomes a TERMINATING error
# even with 2>$null (DEDB PR #144 / FMD v2.9.4 lesson).
function Remove-NwdashService { param([Parameter(Mandatory)][string]$InstallDir)
  $svcName = $script:NwdashServiceName
  $nssm = Join-Path $InstallDir 'nssm.exe'
  if (Test-Path $nssm) {
    try { & $nssm stop $svcName 2>$null | Out-Null } catch { }
    try { & $nssm remove $svcName confirm 2>$null | Out-Null } catch { }
  } else {
    try { & sc.exe stop $svcName 2>$null | Out-Null } catch { }
    try { & sc.exe delete $svcName 2>$null | Out-Null } catch { }
  }
  Remove-NwdashEventSource
  Stop-NwdashProcesses -InstallDir $InstallDir
  # Wait for killed processes to actually exit so their port and file handles
  # are released before a reinstall starts a new instance.
  for ($i = 0; $i -lt 15; $i++) {
    $alive = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
      Where-Object { $_.ExecutablePath -and $_.ExecutablePath -like "$InstallDir\*" }
    if (-not $alive) { break }
    Start-Sleep -Milliseconds 500
  }
}

# ---- non-destructive upgrade (file ops + backups) ----

# Retry a filesystem op through a transient Windows lock. After the service
# stops, Windows (or behavioural EDR - CrowdStrike Falcon, Defender) can hold
# handles under the install dir for seconds to tens of seconds, so an immediate
# Move/Copy/Remove throws "Access to the path ... is denied" even though the
# process is gone. Retrying on that lock class rides through the release lag
# instead of failing the whole upgrade. Non-lock errors rethrow at once.
function Invoke-NwdashFileOp {
  param([Parameter(Mandatory)][scriptblock]$Op, [int]$Tries = 24, [int]$DelayMs = 2500)
  for ($i = 1; $i -le $Tries; $i++) {
    try { & $Op; return }
    catch {
      $m = "$($_.Exception.Message)"
      $locked = ($m -match 'denied' -or $m -match 'being used' -or $m -match 'access to the path')
      if (-not $locked -or $i -eq $Tries) {
        if ($locked) {
          $secs = [int]($Tries * $DelayMs / 1000)
          Write-NwdashWarn "install files still locked after $Tries tries (~${secs}s). On a host with behavioural EDR (CrowdStrike Falcon / Microsoft Defender) the install directory can be held during a directory swap. To clear it: add an EDR exclusion for the install directory, OR reboot the host and run the update once right after boot."
        }
        throw
      }
      Write-NwdashWarn "file op locked (attempt $i/$Tries), retrying in $DelayMs ms: $m"
      Start-Sleep -Milliseconds $DelayMs
    }
  }
}

# Move a file or directory in one step: it either moved or it did not. Move-Item
# is not that for a directory. Under Windows PowerShell 5.1, when the rename is
# refused (any open handle below the folder refuses it) it falls back to moving
# file by file, so one locked file leaves the folder split between source and
# destination, a retry nests the rest inside the destination, and no restore can
# put that back together. Source and destination must be on one volume, which
# holds for everything under InstallDir.
function Move-NwdashItem {
  param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Destination)
  if (Test-Path -LiteralPath $Path -PathType Container) { [IO.Directory]::Move($Path, $Destination) }
  else { [IO.File]::Move($Path, $Destination) }
}

# Copy a file or folder so $Destination ends up holding exactly that, retried
# like any file op. Each try starts clean: Copy-Item -Recurse into the folder an
# earlier try left behind nests the copy inside it, and that tree can still pass
# the health gate.
function Copy-NwdashItem {
  param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Destination)
  Invoke-NwdashFileOp {
    if (Test-Path -LiteralPath $Destination) { Remove-Item -LiteralPath $Destination -Recurse -Force }
    Copy-Item -Recurse -Force $Path $Destination
  }
}

# Installed app version, read from nwdash\config.py at the InstallDir root
# (same source of truth the build uses). '0.0.0' when unreadable.
function Get-NwdashInstalledVersion {
  param([Parameter(Mandatory)][string]$InstallDir)
  $cfg = Join-Path $InstallDir 'nwdash\config.py'
  if (-not (Test-Path $cfg)) { return '0.0.0' }
  try {
    $m = Select-String -Path $cfg -Pattern '^APP_VERSION\s*=\s*"([^"]+)"' | Select-Object -First 1
    if ($m) { return $m.Matches[0].Groups[1].Value }
  } catch { }
  return '0.0.0'
}

# List backup names under InstallDir\backups (newest first, by the trailing
# yyyyMMdd-HHmmss timestamp). Always returns an array. Name format:
# <version>-<yyyyMMdd-HHmmss>, each containing app\ and runtime\. 'undone-*'
# entries (the state a -Rollback moved ASIDE) are excluded so a second
# -Rollback can never restore the build that was just rolled away from.
function Get-NwdashBackups {
  param([Parameter(Mandatory)][string]$InstallDir)
  $dir = Join-Path $InstallDir 'backups'
  if (-not (Test-Path $dir)) { return @() }
  $items = Get-ChildItem -Path $dir -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match '-\d{8}-\d{6}$' -and $_.Name -notlike 'undone-*' } |
    Sort-Object { $_.Name -replace '^.*-(\d{8}-\d{6})$', '$1' } -Descending |
    ForEach-Object { $_.Name }
  return @($items)
}

# Move the current app payload + runtime\python into backups\<Name>\{app,runtime}.
# data\, .certs\, logs\ and backups\ are never part of the moved set. Each
# destructive op is retried through a transient post-stop lock.
function Backup-NwdashApp {
  param([Parameter(Mandatory)][string]$InstallDir, [Parameter(Mandatory)][string]$Name)
  $bakApp = Join-Path $InstallDir "backups\$Name\app"
  $bakRt  = Join-Path $InstallDir "backups\$Name\runtime"
  New-Item -ItemType Directory -Force $bakApp | Out-Null
  foreach ($item in $script:NwdashAppItems) {
    $src = Join-Path $InstallDir $item
    if (Test-Path $src) { Invoke-NwdashFileOp { Move-NwdashItem $src (Join-Path $bakApp $item) } }
  }
  $py = Join-Path $InstallDir 'runtime\python'
  if (Test-Path $py) {
    New-Item -ItemType Directory -Force $bakRt | Out-Null
    Invoke-NwdashFileOp { Move-NwdashItem $py (Join-Path $bakRt 'python') }
  }
}

# Restore app payload + runtime\python from backups\<Name>, replacing what is in
# place. Only what the backup holds is replaced: after a swap that failed partway
# an item with no backup is the live one, never moved, and restoring from an
# emptied backup must not delete the app. Each destructive op is retried through
# locks.
function Restore-NwdashApp {
  param([Parameter(Mandatory)][string]$InstallDir, [Parameter(Mandatory)][string]$Name)
  $bakApp = Join-Path $InstallDir "backups\$Name\app"
  $bakRt  = Join-Path $InstallDir "backups\$Name\runtime\python"
  if (Test-Path $bakApp) {
    foreach ($item in $script:NwdashAppItems) {
      $cur = Join-Path $InstallDir $item
      $bak = Join-Path $bakApp $item
      if (Test-Path $bak) {
        if (Test-Path $cur) { Invoke-NwdashFileOp { Remove-Item -Recurse -Force $cur } }
        Invoke-NwdashFileOp { Move-NwdashItem $bak $cur }
      }
    }
  }
  if (Test-Path $bakRt) {
    $py = Join-Path $InstallDir 'runtime\python'
    if (Test-Path $py) { Invoke-NwdashFileOp { Remove-Item -Recurse -Force $py } }
    Invoke-NwdashFileOp { Move-NwdashItem $bakRt $py }
  }
  # Never fail a restore over this: getting the service back comes first.
  try { Reset-NwdashAppAcl -InstallDir $InstallDir }
  catch { Write-NwdashWarn "restored files may keep the permissions of their backup ($($_.Exception.Message)). Run Setup-NWDash.cmd -Upgrade to fix them." }
}

# Prune backups beyond the newest $Keep.
function Remove-OldNwdashBackups {
  param([Parameter(Mandatory)][string]$InstallDir, [int]$Keep = 3)
  $names = @(Get-NwdashBackups -InstallDir $InstallDir)
  if ($names.Count -le $Keep) { return }
  foreach ($n in ($names | Select-Object -Skip $Keep)) {
    Remove-Item -Recurse -Force (Join-Path $InstallDir "backups\$n") -ErrorAction SilentlyContinue
  }
}

# ---- deterministic stop / start (Windows service via nssm) ----

# After an nssm stop, GUARANTEE the old service is fully down before swapping
# app files or starting a new instance. Waits (up to GraceSec) for every
# process under $InstallDir to exit AND port $Port to be released; if a
# straggler is still up at the deadline, force-kills the leftover install-dir
# processes and waits again. Returns $true when clean, $false if the port is
# still held after the force-kill (caller logs + proceeds; the health gate +
# rollback remain the backstop). Probes are injectable for tests.
function Wait-NwdashCleanStop {
  param(
    [Parameter(Mandatory)][string]$InstallDir,
    [int]$Port = 0,
    [int]$GraceSec = 12,
    [string]$BindAddr = '',              # this app's pinned bind IP; '' = unscoped/loopback default
    [scriptblock]$GetProcs = $null,      # ($dir) -> @( objects with .ProcessId ) under $dir
    [scriptblock]$TestPort = $null,      # ($p)   -> truthy if the port is held
    [scriptblock]$GetPortOwner = $null,  # ($p)   -> owning PID of the port (0/$null if none)
    [scriptblock]$KillProc = $null,      # ($id)  -> force kill
    [scriptblock]$Sleep    = $null       # ($ms)
  )
  # Scope every port probe to THIS app's bound IP when one is pinned (a
  # multi-service box: two apps on the same port, different IPs). Loopback /
  # unpinned -> unscoped match. Defaults are PLAIN scriptblocks (never
  # .GetNewClosure()): a closure-bound default cannot see dot-sourced functions
  # when the installer itself is invoked through a nested '&' boundary (DEDB
  # 1.48.1 lesson) and breaks under ConstrainedLanguage.
  # NOTE: PowerShell variables are case-INsensitive, so the scoped copy must
  # NOT be named $bindAddr (assigning it would wipe the $BindAddr parameter
  # before it is read).
  $scopedAddr = ''
  if ($BindAddr -and $BindAddr -ne '0.0.0.0' -and $BindAddr -ne '127.0.0.1') { $scopedAddr = $BindAddr }
  $getProcs = if ($GetProcs) { $GetProcs } else { { param($d) @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.ExecutablePath -and $_.ExecutablePath -like "$d\*" }) } }
  $testPort = if ($TestPort) { $TestPort } else { { param($p) if ($p -gt 0) { [bool](Test-NwdashPortInUse -Port $p -LocalAddress $scopedAddr) } else { $false } } }
  $getPortOwner = if ($GetPortOwner) { $GetPortOwner } else { { param($p) if ($p -gt 0) { $o = Test-NwdashPortInUse -Port $p -LocalAddress $scopedAddr; if ($o) { [int]$o.ProcessId } else { 0 } } else { 0 } } }
  $killProc = if ($KillProc) { $KillProc } else { { param($procId) try { Stop-Process -Id $procId -Force -ErrorAction Stop } catch {} } }
  $sleep    = if ($Sleep)    { $Sleep }    else { { param($ms) Start-Sleep -Milliseconds $ms } }

  $clean = $false
  $steps = [Math]::Max(1, [int]($GraceSec * 2))   # 500ms steps
  for ($i = 0; $i -lt $steps; $i++) {
    $procs = @(& $getProcs $InstallDir)
    $busy = [bool](& $testPort $Port)
    if ($procs.Count -eq 0 -and -not $busy) { $clean = $true; break }
    & $sleep 500
  }
  if (-not $clean) {
    Write-NwdashWarn "service did not fully release port $Port after stop - force-killing leftover install-dir processes"
    foreach ($p in @(& $getProcs $InstallDir)) { & $killProc $p.ProcessId }
    # The socket can be held by a process that does NOT live under $InstallDir.
    # Kill the ACTUAL owner of the port as a decisive backstop - but ONLY when
    # the probe is scoped to our own pinned IP. Unscoped means DO NOT TOUCH:
    # on a co-tenant host the first listener returned may belong to ANOTHER app
    # on the same port (its own IP), and killing it is an outage in software we
    # are not even deploying (this exact incident happened on DEDB). A stuck
    # port is survivable (health gate + rollback); killing a bystander is not.
    # Also never touch protected low PIDs (0=Idle, 4=System/http.sys).
    & $sleep 1000
    if ($Port -gt 0 -and [bool](& $testPort $Port)) {
      if (-not $scopedAddr) {
        Write-NwdashWarn "port $Port still held, but no pinned bind address is set - NOT force-killing the port owner (it may belong to another app on this host)."
      } else {
        $ownerId = 0; try { $ownerId = [int](& $getPortOwner $Port) } catch { $ownerId = 0 }
        if ($ownerId -gt 4) {
          Write-NwdashWarn "port $Port ($scopedAddr) still held by PID $ownerId after path-filtered kill - force-killing the port owner"
          & $killProc $ownerId
        }
      }
    }
    for ($i = 0; $i -lt 20; $i++) {
      $procs = @(& $getProcs $InstallDir)
      $busy = [bool](& $testPort $Port)
      if ($procs.Count -eq 0 -and -not $busy) { $clean = $true; break }
      & $sleep 500
    }
  }
  return $clean
}

# Toggle nssm's AppExit Default action. Enabled:$false -> 'Exit' (nssm will NOT
# resurrect the app if it exits or is force-killed during a swap - the DEDB
# resurrection gotcha); Enabled:$true -> 'Restart' (normal auto-recovery). nssm
# writes to stderr on some sets; swallow it and never throw - a set failure
# must never abort an upgrade.
function Set-NwdashNssmAutoRestart {
  param([Parameter(Mandatory)]$Nssm, [bool]$Enabled)
  $action = if ($Enabled) { 'Restart' } else { 'Exit' }
  try { & $Nssm set $script:NwdashServiceName AppExit Default $action 2>$null | Out-Null } catch { }
}

# Poll the Windows service until its Status equals $State ('Stopped' |
# 'Running' | ...), up to ~$TimeoutSec. Returns $true once it matches, $false
# at timeout. Never throws. GetState/Sleep are injectable so the poll loop is
# unit-testable with no real service. Used to confirm the SCM settled to
# Stopped (not STOP_PENDING) before a file swap, and that a start took.
function Wait-NwdashServiceState {
  param(
    [string]$Name = $script:NwdashServiceName,
    [Parameter(Mandatory)][string]$State,
    [int]$TimeoutSec = 30,
    [int]$PollMs = 500,
    [scriptblock]$GetState = $null,
    [scriptblock]$Sleep = $null
  )
  $getState = if ($GetState) { $GetState } else { { param($n) $svc = Get-Service $n -ErrorAction SilentlyContinue; if ($svc) { "$($svc.Status)" } else { '' } } }
  $sleep    = if ($Sleep)    { $Sleep }    else { { param($ms) Start-Sleep -Milliseconds $ms } }
  $steps = [Math]::Max(1, [int]([double]$TimeoutSec * 1000 / $PollMs))
  for ($i = 0; $i -le $steps; $i++) {
    $status = ''
    try { $status = "$(& $getState $Name)" } catch { $status = '' }
    if ($status -eq $State) { return $true }
    if ($i -lt $steps) { & $sleep $PollMs }
  }
  return $false
}

# Start the service and VERIFY it reaches Running, retrying up to $Retries.
# Each attempt issues the start, then waits up to $SettleSec for Running; if
# the service is still not Running (e.g. an nssm start dropped while the SCM
# was settling a prior STOP_PENDING), it retries. Returns $true as soon as
# Running is observed, else $false. Injected callbacks keep the retry loop
# unit-testable without a real service. Never throws.
function Start-NwdashServiceVerified {
  param(
    $Nssm,
    [string]$Name = $script:NwdashServiceName,
    [int]$Retries = 3,
    [int]$SettleSec = 15,
    [scriptblock]$StartSvc = $null,
    [scriptblock]$WaitState = $null,
    [scriptblock]$GetState = $null,
    [scriptblock]$Log = $null
  )
  $startSvc  = if ($StartSvc)  { $StartSvc }  else { { & $Nssm start $Name 2>$null | Out-Null } }
  $waitState = if ($WaitState) { $WaitState } else { { param($st, $to) Wait-NwdashServiceState -Name $Name -State $st -TimeoutSec $to } }
  $getState  = if ($GetState)  { $GetState }  else { { $svc = Get-Service $Name -ErrorAction SilentlyContinue; if ($svc) { "$($svc.Status)" } else { '' } } }
  $log       = if ($Log)       { $Log }       else { { param($m) Write-NwdashLog $m } }

  for ($attempt = 1; $attempt -le $Retries; $attempt++) {
    & $log "starting service $Name (attempt $attempt/$Retries)"
    try { & $startSvc } catch { }
    if (& $waitState 'Running' $SettleSec) { return $true }
    $cur = ''
    try { $cur = "$(& $getState)" } catch { $cur = '' }
    & $log "service $Name not Running after attempt $attempt (status: $cur)"
  }
  return $false
}

# Put the pre-deploy version back and start it, as the last step of a swap that
# failed partway or a health gate that failed. $Name is the backup this run moved
# the live app into ('' = nothing moved yet); $ReturnTo, on -Rollback once the
# live app is out, is the backup whose files are in place by now: they go back
# there, because the restore would delete them. The caller names both from what
# it did, never from Get-NwdashBackups, whose newest entry can be an emptied
# folder (review O3). A half-restored app must not run against data\, so when the
# files cannot be put back the service stays stopped. Never throws: returns the
# outcome for the caller's final error message.
function Undo-NwdashDeploy {
  param(
    [Parameter(Mandatory)][string]$InstallDir,
    [Parameter(Mandatory)]$Nssm,
    [string]$Name = '',
    [string]$ReturnTo = '',
    [int]$Port = 0,
    [string]$HostName = '127.0.0.1'
  )
  try {
    if ($ReturnTo) { Backup-NwdashApp -InstallDir $InstallDir -Name $ReturnTo }
    if ($Name) { Restore-NwdashApp -InstallDir $InstallDir -Name $Name }
  } catch {
    return "could not put the previous version back ($($_.Exception.Message)). The service is left STOPPED. " +
      "Once nothing holds the files, move what is left in $InstallDir\backups\$Name back over the install folder " +
      "(app\* to its root, runtime\python to runtime\python), then run: Start-Service $script:NwdashServiceName"
  }
  # This run made that folder and the restore emptied it. Left behind it would
  # pass for the newest backup and stop the next -Rollback reaching a real one.
  $spent = Join-Path $InstallDir "backups\$Name"
  if ($Name -and -not @(Get-ChildItem -LiteralPath $spent -Recurse -File -Force -ErrorAction SilentlyContinue).Count) {
    Remove-Item -LiteralPath $spent -Recurse -Force -ErrorAction SilentlyContinue
  }
  if (-not (Wait-NwdashPortFree -Port $Port -TimeoutSec 20 -HostName $HostName)) {
    Write-NwdashWarn "port $Port still held after stop - starting anyway (app bind-retry will absorb it)"
  }
  Set-NwdashNssmAutoRestart -Nssm $Nssm -Enabled:$true
  if (-not (Start-NwdashServiceVerified -Nssm $Nssm)) {
    return "the previous version is back but the service did not start - run: Start-Service $script:NwdashServiceName"
  }
  return 'rolled back to the pre-deploy state'
}

# ---- dashboard password gate ----

# The app refuses to start on a non-loopback bind with no dashboard password
# unless this is in its environment (nwdash/main.py). It is an env var, not the
# --allow-unauthenticated flag, because -Rollback keeps the service command
# line: an older build ignores an unknown variable but rejects an unknown flag.
$script:NwdashAllowUnauthEnv = 'DASHBOARD_ALLOW_UNAUTHENTICATED=1'

# What an install/upgrade does about the dashboard password, decided BEFORE
# anything is stopped or copied: a password-less network install is refused up
# front instead of failing its health gate in an NSSM restart loop. Returns
# 'password' (set now), 'stored' (data\auth.json already holds one), 'unknown'
# (data\ is locked and this run is not elevated, so the elevated run decides),
# 'loopback' (bound to loopback, which the app permits), 'unauthenticated'
# (explicitly allowed) or 'refused'. Pure + testable. Empty $BindHost = all
# interfaces.
function Get-NwdashAuthPlan {
  param([bool]$PasswordGiven, [bool]$PasswordStored, [string]$BindHost = '', [bool]$AllowUnauthenticated, [bool]$PasswordUnknown = $false)
  if ($PasswordGiven) { return 'password' }
  if ($PasswordStored) { return 'stored' }
  if ($PasswordUnknown) { return 'unknown' }
  if ($BindHost -in @('127.0.0.1', 'localhost', '::1')) { return 'loopback' }
  if ($AllowUnauthenticated) { return 'unauthenticated' }
  return 'refused'
}

# ---- reinstall over an existing service (review O2) ----

# The settings the installed service runs with, read through nssm as -Upgrade
# reads them: its launcher command line, its extra environment (one variable per
# line) and the account it logs on as. Any nssm.exe can read the service, so the
# caller may pass the bundle's own; -Nssm may also be a scriptblock. $null when
# the command line cannot be read (no nssm, not this app, or a run that may not
# query the service): a reinstall must not guess what it would drop. Never throws.
function Read-NwdashLiveService {
  param(
    $Nssm = '',
    [string]$Name = $script:NwdashServiceName,
    [scriptblock]$NssmGet = $null,     # ($setting) -> raw `nssm get` output
    [scriptblock]$GetService = $null   # passed to Read-NwdashServiceAccount
  )
  $nssmGet = if ($NssmGet) { $NssmGet } else { { param($setting) & $Nssm get $Name $setting 2>$null } }
  $params = ''
  try { $params = ConvertFrom-NwdashNssmOutput -Raw (& $nssmGet 'AppParameters') } catch { $params = '' }
  if ($params -notmatch 'networker_dashboard\.py') { return $null }
  $envText = ''
  try { $envText = ConvertFrom-NwdashNssmOutput -Raw (& $nssmGet 'AppEnvironmentExtra') } catch { $envText = '' }
  return [pscustomobject]@{
    Parameters = $params.Trim()
    Env        = @($envText -split "`r?`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    Account    = (Read-NwdashServiceAccount -Name $Name -GetService $GetService)
  }
}

# What a plain install over the installed service runs with. It used to rebuild
# the service from its own command line alone, so re-running it to change one
# thing dropped the port, bind, allow-list, account and -AllowUnauthenticated the
# service had (review O2). Now whatever $Given (the names in $PSBoundParameters)
# leaves out comes from $Live (Read-NwdashLiveService).
#
# The environment is kept whole, with two exceptions: -AllowedHosts replaces the
# allow-list, and a DASHBOARD_AUTH_PASSWORD that an install failed to clear is
# dropped, because the app would set the password from it at every start and
# undo a change made in the admin console. Env holds the lines the installer does
# not manage itself; AllowedHosts and AllowUnauthenticated carry the other two.
#
# Windows never gives a service account's password back, so an account that
# needs one is kept only with -ServicePassword. Otherwise Problem says why the
# install must stop, before it changes anything. Kept lists what was carried, for
# the log. Pure + testable.
function Get-NwdashCarryOver {
  param(
    [Parameter(Mandatory)]$Live,
    [string[]]$Given = @(),
    [int]$Port = 8443,
    [string]$BindHost = '',
    [string]$ServiceAccount = '',
    [string]$AllowedHosts = ''
  )
  $out = [pscustomobject]@{
    Port = $Port; BindHost = $BindHost; ServiceAccount = $ServiceAccount; AllowedHosts = $AllowedHosts
    AllowUnauthenticated = $false; Env = @(); Kept = @(); Problem = ''
  }
  $liveArgs = Get-NwdashServiceArgs -Parameters $Live.Parameters -DefaultPort $Port
  if ('Port' -notin $Given) { $out.Port = $liveArgs.Port; $out.Kept += "port $($liveArgs.Port)" }
  if ('BindHost' -notin $Given) {
    $out.BindHost = $liveArgs.Bind
    if ($liveArgs.Bind) { $out.Kept += "bind $($liveArgs.Bind)" }
  }
  foreach ($line in @($Live.Env)) {
    $value = $line.Substring($line.IndexOf('=') + 1)
    if ($line -like 'DASHBOARD_ALLOWED_HOSTS=*') {
      if ('AllowedHosts' -notin $Given -and $value) { $out.AllowedHosts = $value; $out.Kept += "allowed hosts $value" }
    } elseif ($line -like 'DASHBOARD_ALLOW_UNAUTHENTICATED=*') {
      # The app honours exactly 1 (nwdash/main.py); anything else meant no.
      if ($value.Trim() -eq '1') { $out.AllowUnauthenticated = $true; $out.Kept += '-AllowUnauthenticated' }
    } elseif ($line -notlike 'DASHBOARD_AUTH_PASSWORD=*') {
      $out.Env += $line
    }
  }
  if ($out.Env.Count) { $out.Kept += "$($out.Env.Count) other environment variable(s)" }
  if ('ServiceAccount' -notin $Given) {
    $account = "$($Live.Account)".Trim()
    if (-not $account) {
      $out.Problem = 'could not read the account the installed service logs on as, so a reinstall cannot keep it. ' +
        'Nothing was changed. Re-run with -ServiceAccount <account> (and -ServicePassword), or use -Upgrade, which keeps the service as it is.'
    } elseif ($account -in @('LocalSystem', '.\LocalSystem', 'NT AUTHORITY\SYSTEM')) {
      $out.ServiceAccount = ''   # nssm's default, nothing to set
      $out.Kept += 'account LocalSystem'
    } elseif ($account -like 'NT AUTHORITY\*' -or 'ServicePassword' -in $Given) {
      $out.ServiceAccount = $account
      $out.Kept += "account $account"
    } else {
      $out.Problem = "the installed service logs on as $account, and Windows does not give a service account's password back, " +
        'so a reinstall cannot keep that account by itself. Nothing was changed. Re-run with -ServicePassword <its password> to keep it, ' +
        'or -ServiceAccount <account> -ServicePassword <password> to change it, or use -Upgrade, which keeps the service as it is.'
    }
  }
  return $out
}

# ---- folder permissions ----

# An install folder must not inherit from C:\: Users can read there on every
# edition, and Authenticated Users can change on client editions. The keys in
# data\ are machine-scope DPAPI, which unwraps them for anyone who can read the
# file, so reading is enough to decrypt every stored credential and forge a
# login cookie, and write access edits code that runs as LocalSystem (review
# S5). The keys stay machine-scope on purpose: a user-scope key cannot survive a
# change of service account. SIDs, not names: the names are translated on
# non-English Windows.
$script:NwdashSidSystem = 'S-1-5-18'
$script:NwdashSidAdmins = 'S-1-5-32-544'
$script:NwdashSidUsers  = 'S-1-5-32-545'
# What other users may read, but never change. Anything else in InstallDir gets
# the lock data\ gets: it may be an old data tree, and the service keeps its
# access, so a --cert path inside InstallDir still works.
$script:NwdashReadableItems = $script:NwdashAppItems + @('runtime', 'nssm.exe')

# Does a path exist, as far as this process can tell? 'present', 'absent' or
# 'denied'. Test-Path cannot say: on a locked folder it raises access denied,
# which is fatal under $ErrorActionPreference = 'Stop'. Never throws.
function Get-NwdashPathState {
  param([Parameter(Mandatory)][string]$Path)
  try { [void][IO.File]::GetAttributes($Path); return 'present' }
  catch [UnauthorizedAccessException] { return 'denied' }
  catch { return 'absent' }
}

# The SID an install grants access to, for a name as it appears in -ServiceAccount
# or in the service's own configuration. Empty and LocalSystem are SYSTEM. Throws
# when the account does not resolve, so a typo stops an install before it starts.
function Resolve-NwdashAccountSid {
  param([string]$Account = '')
  $name = "$Account".Trim()
  if (-not $name -or $name -ieq 'LocalSystem' -or $name -ieq '.\LocalSystem') { return $script:NwdashSidSystem }
  if ($name -match '^S-1-\d+(-\d+)+$') { return $name }
  if ($name.StartsWith('.\')) { $name = "$env:COMPUTERNAME\$($name.Substring(2))" }
  try { return (New-Object Security.Principal.NTAccount($name)).Translate([Security.Principal.SecurityIdentifier]).Value }
  catch { throw "service account '$Account' does not resolve to a Windows account" }
}

# The account the installed service logs on as, as the service manager reports
# it ('LocalSystem', 'CORP\svc-nwdash', ...). '' when it cannot be read, which is
# NOT the same as LocalSystem: locking on that guess would shut a named account
# out of data\. Never throws.
function Read-NwdashServiceAccount {
  param([string]$Name = $script:NwdashServiceName, [scriptblock]$GetService = $null)
  $getService = if ($GetService) { $GetService } else { { param($n) Get-CimInstance Win32_Service -Filter "Name='$n'" -ErrorAction SilentlyContinue } }
  try {
    $svc = & $getService $Name
    if ($svc) { return "$($svc.StartName)".Trim() }
  } catch { }
  return ''
}

# Why a folder must not get this lock, or '' when it may. Removing inheritance
# from a drive root or a system folder would break the machine, and -InstallDir
# is free text. Pure + testable.
function Get-NwdashAclTargetProblem {
  param(
    [Parameter(Mandatory)][string]$InstallDir,
    [string[]]$SystemFolders = @($env:SystemRoot, $env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:ProgramW6432, $env:ProgramData, $env:USERPROFILE, $env:PUBLIC)
  )
  $full = [IO.Path]::GetFullPath($InstallDir).TrimEnd('\')
  if ($full -ieq [IO.Path]::GetPathRoot($full).TrimEnd('\')) { return "$InstallDir is a drive root" }
  foreach ($folder in $SystemFolders) {
    if (-not $folder) { continue }
    $sys = $folder.TrimEnd('\')
    if ($sys -ieq $full -or $sys.StartsWith("$full\", [StringComparison]::OrdinalIgnoreCase)) {
      return "$InstallDir is or contains the system folder $sys"
    }
  }
  return ''
}

# The access list one part of an install gets. Pure + testable. 'root' is
# InstallDir itself and, by inheritance, the readable items; 'private' is data\,
# .certs\, logs\ and anything unknown; 'backups' is for the installer alone.
function Get-NwdashAclGrants {
  param(
    [Parameter(Mandatory)][ValidateSet('root', 'private', 'backups')][string]$Tier,
    [Parameter(Mandatory)][string]$ServiceSid
  )
  $grants = @(
    @{ Sid = $script:NwdashSidSystem; Rights = 'FullControl' },
    @{ Sid = $script:NwdashSidAdmins; Rights = 'FullControl' }
  )
  $named = $ServiceSid -notin @($script:NwdashSidSystem, $script:NwdashSidAdmins)
  if ($Tier -eq 'root') {
    $grants += @{ Sid = $script:NwdashSidUsers; Rights = 'ReadAndExecute' }
    if ($named) { $grants += @{ Sid = $ServiceSid; Rights = 'ReadAndExecute' } }
  } elseif ($Tier -eq 'private' -and $named) {
    $grants += @{ Sid = $ServiceSid; Rights = 'Modify' }
  }
  return $grants
}

# Replace an item's access list with exactly these grants, inheriting nothing. A
# new security object, because icacls /grant:r only replaces the SIDs it names,
# so an entry left by a previous service account or by hand would survive. Not
# Set-Acl: on an item that is already protected it also writes the audit list,
# which wipes any audit rules and needs SeSecurityPrivilege. This writes the
# access list alone, in one call, so old and new entries never mix.
function Set-NwdashItemAcl {
  param([Parameter(Mandatory)]$Item, [Parameter(Mandatory)][hashtable[]]$Grants)
  $isDir = [bool]$Item.PSIsContainer
  $security = if ($isDir) { New-Object System.Security.AccessControl.DirectorySecurity } else { New-Object System.Security.AccessControl.FileSecurity }
  $inherit = if ($isDir) { 'ContainerInherit,ObjectInherit' } else { 'None' }
  $security.SetAccessRuleProtection($true, $false)
  foreach ($grant in $Grants) {
    $sid = New-Object System.Security.Principal.SecurityIdentifier($grant.Sid)
    $security.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($sid, $grant.Rights, $inherit, 'None', 'Allow')))
  }
  # The method is .NET Framework (Windows PowerShell 5.1); pwsh 7 has the class.
  if ($Item | Get-Member -Name SetAccessControl) { $Item.SetAccessControl($security) }
  else { [System.IO.FileSystemAclExtensions]::SetAccessControl($Item, $security) }
}

# Run icacls and throw unless it worked. Never pass /C: with it icacls exits 0
# even after every file failed, which would hide a lock that did not take. It
# also exits 0 for a path that does not exist, so callers check first, and /T is
# for folders only: on a file it searches every subfolder for that file name.
function Invoke-NwdashIcacls {
  param([Parameter(Mandatory)][string[]]$Arguments)
  # PS 5.1 turns native stderr into a terminating error under 'Stop'; the exit
  # code is the real arbiter here (DEDB PR #144 lesson).
  $ErrorActionPreference = 'Continue'
  $out = & icacls.exe @Arguments 2>&1
  if ($LASTEXITCODE -ne 0) { throw "icacls $($Arguments -join ' ') failed with exit code ${LASTEXITCODE}: $(($out | ForEach-Object { "$_" }) -join ' ')" }
}

# Put the app files back on InstallDir's permissions. Move-Item keeps a file's
# access list, so whatever comes back from backups\ carries what it left with,
# and a backup taken before the lock is open to every user. -Icacls is injectable
# because /setowner needs elevation.
function Reset-NwdashAppAcl {
  param([Parameter(Mandatory)][string]$InstallDir, [scriptblock]$Icacls = $null)
  $icacls = if ($Icacls) { $Icacls } else { { param($a) Invoke-NwdashIcacls -Arguments $a } }
  foreach ($name in $script:NwdashReadableItems) {
    $path = Join-Path $InstallDir $name
    if (-not (Test-Path -LiteralPath $path)) { continue }
    $tree = @(if (Test-Path -LiteralPath $path -PathType Container) { '/T' })
    & $icacls (@($path, '/setowner', "*$($script:NwdashSidAdmins)") + $tree + '/Q')
    & $icacls (@($path, '/reset') + $tree + '/Q')
  }
}

# Lock an install (review S5). Idempotent, and safe with the service running: its
# account is granted what it needs in the same call that takes everyone else's
# access away. Private parts go first and the root last, so stopping part-way
# never leaves a secret more open than it was.
function Set-NwdashInstallAcl {
  param(
    [Parameter(Mandatory)][string]$InstallDir,
    [Parameter(Mandatory)][string]$ServiceSid,
    [scriptblock]$Icacls = $null
  )
  $icacls = if ($Icacls) { $Icacls } else { { param($a) Invoke-NwdashIcacls -Arguments $a } }
  $root = $InstallDir.TrimEnd('\')
  $admins = "*$($script:NwdashSidAdmins)"
  # Created here, not left to the app: a folder made later would inherit the
  # root's Users access, and that is where the TLS private key goes.
  foreach ($dir in 'data', '.certs', 'logs', 'backups') { New-Item -ItemType Directory -Force (Join-Path $root $dir) | Out-Null }
  foreach ($item in @(Get-ChildItem -LiteralPath $root -Force)) {
    if ($item.Name -in $script:NwdashReadableItems) { continue }
    $tier = if ($item.Name -eq 'backups') { 'backups' } else { 'private' }
    $tree = @(if ($item.PSIsContainer) { '/T' })
    # An owner can always rewrite an access list, so a folder a standard user
    # created (a hand-started or legacy install) has to change hands first.
    & $icacls (@($item.FullName, '/setowner', $admins) + $tree + '/Q')
    Set-NwdashItemAcl -Item $item -Grants (Get-NwdashAclGrants -Tier $tier -ServiceSid $ServiceSid)
    # Explicit entries on what is inside would outlive the new list above.
    if ($item.PSIsContainer) { & $icacls @("$($item.FullName)\*", '/reset', '/T', '/Q') }
  }
  & $icacls @($root, '/setowner', $admins, '/Q')
  Set-NwdashItemAcl -Item (Get-Item -LiteralPath $root -Force) -Grants (Get-NwdashAclGrants -Tier 'root' -ServiceSid $ServiceSid)
  Reset-NwdashAppAcl -InstallDir $root -Icacls $icacls
}

# ---- firewall ----

# The inbound rule opens the port on Domain and Private networks only. A network
# Windows classifies as Public (an unidentified one, a guest or hotel network) is
# where an exposed admin port does the most harm (review S4); -FirewallProfile Any
# opens it there too.
$script:NwdashFirewallDefault = 'Domain,Private'

# -FirewallProfile as New-NetFirewallRule takes it, or '' when it is not one:
# Any, or any of Domain, Private and Public. Commas or spaces separate them,
# because Setup-NWDash.cmd hands `Domain,Private` on as `Domain Private`. Pure +
# testable.
function Resolve-NwdashFirewallProfile {
  param([string]$Value = '')
  $known = @('Domain', 'Private', 'Public')
  $names = @("$Value" -split '[,\s]+' | Where-Object { $_ })
  if ($names.Count -eq 1 -and $names[0] -ieq 'Any') { return 'Any' }
  if (-not $names.Count) { return '' }
  foreach ($n in $names) { if ($n -notin $known) { return '' } }
  return (@($known | Where-Object { $names -contains $_ }) -join ',')
}

# The connected networks a rule for $NetworkProfile leaves closed, from
# Get-NetConnectionProfile (NetworkCategory Public, Private or DomainAuthenticated).
# Pure + testable.
function Get-NwdashFirewallGap {
  param([string]$NetworkProfile = $script:NwdashFirewallDefault, [object[]]$Networks = @())
  $covered = @($NetworkProfile -split ',')
  if ($covered -contains 'Any') { return @() }
  $gaps = @()
  foreach ($n in @($Networks)) {
    $category = "$($n.NetworkCategory)"
    if ($category -eq 'DomainAuthenticated') { $category = 'Domain' }
    if ($category -and $category -notin $covered) {
      $gaps += [pscustomobject]@{ Name = "$($n.Name)"; InterfaceAlias = "$($n.InterfaceAlias)"; Category = $category }
    }
  }
  return $gaps
}

# What each closed network means, for the install log and -Check.
function Format-NwdashFirewallGap {
  param([Parameter(Mandatory)]$Gap, [int]$Port)
  return "network '$($Gap.Name)' on $($Gap.InterfaceAlias) is classified $($Gap.Category), so the firewall does not open " +
    "port $Port there and the dashboard cannot be reached through it. Re-run with -FirewallProfile Any to open it there " +
    'too, or have that network classified Private or Domain.'
}

# Replace the rule group with one inbound rule for $Port on $NetworkProfile, then
# warn about each connected network it leaves closed. Never throws: a firewall
# failure is a warning, as before. The cmdlets are injectable for tests.
function Set-NwdashFirewall {
  param(
    [Parameter(Mandatory)][int]$Port,
    [string]$NetworkProfile = $script:NwdashFirewallDefault,
    [scriptblock]$RemoveRules = $null,   # () -> remove the group
    [scriptblock]$NewRule = $null,       # ($port, $profile) -> create the rule
    [scriptblock]$GetNetworks = $null    # () -> Get-NetConnectionProfile rows
  )
  $removeRules = if ($RemoveRules) { $RemoveRules } else { { Remove-NetFirewallRule -Group $script:NwdashServiceName -ErrorAction SilentlyContinue } }
  $newRule = if ($NewRule) { $NewRule } else { { param($p, $prof) New-NetFirewallRule -DisplayName "NetWorker Dashboard web ($p)" -Group $script:NwdashServiceName -Direction Inbound -Action Allow -Protocol TCP -LocalPort $p -Profile $prof -ErrorAction Stop | Out-Null } }
  $getNetworks = if ($GetNetworks) { $GetNetworks } else { { @(Get-NetConnectionProfile -ErrorAction SilentlyContinue) } }
  try {
    & $removeRules
    & $newRule $Port $NetworkProfile
    Write-NwdashLog "firewall: opened inbound TCP $Port on $NetworkProfile networks"
  } catch {
    Write-NwdashWarn "could not configure the firewall ($($_.Exception.Message)). Open inbound TCP $Port manually."
    return
  }
  $networks = @()
  try { $networks = @(& $getNetworks) } catch { $networks = @() }
  foreach ($gap in @(Get-NwdashFirewallGap -NetworkProfile $NetworkProfile -Networks $networks)) {
    Write-NwdashWarn (Format-NwdashFirewallGap -Gap $gap -Port $Port)
  }
}

function Remove-NwdashFirewall {
  try { Remove-NetFirewallRule -Group $script:NwdashServiceName -ErrorAction SilentlyContinue; Write-NwdashLog 'firewall: removed NetWorkerDashboard rules' } catch { }
}
