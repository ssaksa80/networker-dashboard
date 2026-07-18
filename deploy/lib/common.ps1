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
    if (Test-Path $src) { Invoke-NwdashFileOp { Move-Item $src (Join-Path $bakApp $item) } }
  }
  $py = Join-Path $InstallDir 'runtime\python'
  if (Test-Path $py) {
    New-Item -ItemType Directory -Force $bakRt | Out-Null
    Invoke-NwdashFileOp { Move-Item $py (Join-Path $bakRt 'python') }
  }
}

# Restore app payload + runtime\python from backups\<Name> (removing whatever
# is currently in place first). Each destructive op is retried through locks.
function Restore-NwdashApp {
  param([Parameter(Mandatory)][string]$InstallDir, [Parameter(Mandatory)][string]$Name)
  $bakApp = Join-Path $InstallDir "backups\$Name\app"
  $bakRt  = Join-Path $InstallDir "backups\$Name\runtime\python"
  if (Test-Path $bakApp) {
    foreach ($item in $script:NwdashAppItems) {
      $cur = Join-Path $InstallDir $item
      $bak = Join-Path $bakApp $item
      if (Test-Path $cur) { Invoke-NwdashFileOp { Remove-Item -Recurse -Force $cur } }
      if (Test-Path $bak) { Invoke-NwdashFileOp { Move-Item $bak $cur } }
    }
  }
  if (Test-Path $bakRt) {
    $py = Join-Path $InstallDir 'runtime\python'
    if (Test-Path $py) { Invoke-NwdashFileOp { Remove-Item -Recurse -Force $py } }
    Invoke-NwdashFileOp { Move-Item $bakRt $py }
  }
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

# ---- firewall ----

function Set-NwdashFirewall {
  param([Parameter(Mandatory)][int]$Port)
  try {
    Remove-NetFirewallRule -Group $script:NwdashServiceName -ErrorAction SilentlyContinue
    New-NetFirewallRule -DisplayName "NetWorker Dashboard web ($Port)" -Group $script:NwdashServiceName -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -Profile Any -ErrorAction Stop | Out-Null
    Write-NwdashLog "firewall: opened inbound TCP $Port"
  } catch { Write-NwdashWarn "could not configure the firewall ($($_.Exception.Message)). Open inbound TCP $Port manually." }
}

function Remove-NwdashFirewall {
  try { Remove-NetFirewallRule -Group $script:NwdashServiceName -ErrorAction SilentlyContinue; Write-NwdashLog 'firewall: removed NetWorkerDashboard rules' } catch { }
}
