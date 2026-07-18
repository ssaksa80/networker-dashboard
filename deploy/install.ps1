# NetWorker Dashboard installer - runs from INSIDE an unpacked bundle
# (nwdash-bundle-<version>-win-x64.zip). Ported from the DEDB project's
# deploy\install.ps1 and trimmed to this app (no SQL, no patch tier).
#
#   deploy\install.ps1                          fresh install / migrate from the
#                                               legacy scheduled-task install
#   deploy\install.ps1 -Check                   dry-run: print the resolved plan
#   deploy\install.ps1 -Upgrade                 swap app + runtime, keep config
#   deploy\install.ps1 -Rollback                restore the newest backup
#   deploy\install.ps1 -Uninstall [-Purge]      remove service + files
#
# Install layout (InstallDir root; cwd of the service is InstallDir so data\,
# .certs\ and logs\ resolve beside the launcher, exactly like the old
# scheduled-task install):
#   networker_dashboard.py  nwdash\  README.md  pyproject.toml   (app payload)
#   runtime\python\         embedded CPython (bundled, zero host prereqs)
#   nssm.exe                service manager (bundled)
#   data\ .certs\ logs\ backups\    NEVER touched by upgrades/rollbacks
#
# ASCII only + Windows PowerShell 5.1 safe.
[CmdletBinding()] param(
  [Parameter(Position = 0)][string]$InstallDir = 'C:\apps\networker-dashboard',
  [int]$Port = 8443,
  [string]$BindHost = '',            # pin ONE interface (a server IP); empty = all interfaces
  [string]$ServiceAccount,           # e.g. CORP\svc-nwdash (empty = LocalSystem)
  [string]$ServicePassword,
  [string]$AuthPassword = '',        # dashboard password; app stores a salted hash, then the plaintext is removed from the service env
  [string]$AllowedHosts = '',        # comma-separated NetWorker host allow-list
  [switch]$Check,                    # dry-run: print the resolved plan, change nothing
  [switch]$Upgrade,
  [switch]$Rollback,
  [switch]$Uninstall,
  [switch]$Purge,
  [switch]$NoFirewall,
  [switch]$Elevated                  # internal: marks the post-UAC relaunch
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
. "$Here\lib\common.ps1"

$Bundle = Split-Path -Parent $Here          # <bundle>\deploy\install.ps1 -> <bundle>
$AppSrc = Join-Path $Bundle 'app'
$PySrc  = Join-Path $Bundle 'runtime\python'
$NssmSrc = Join-Path $Bundle 'runtime\nssm.exe'
$SvcName = $script:NwdashServiceName

$BundleVersion = '0.0.0'
if (Test-Path "$Bundle\VERSION") { $BundleVersion = (Get-Content -Raw "$Bundle\VERSION").Trim() }

# The launcher command line the service runs. --bind is only pinned when
# requested; the app default (0.0.0.0) serves all interfaces incl. loopback.
function Get-NwdashLauncherArgs {
  param([int]$P, [string]$B)
  $a = "networker_dashboard.py --port $P"
  if ($B) { $a += " --bind $B" }
  return "$a --no-launch"
}

# ---- -Check: dry-run plan (no admin, no changes) ----
if ($Check) {
  $svc = Get-Service -Name $SvcName -ErrorAction SilentlyContinue
  $task = Get-ScheduledTask -TaskName $SvcName -ErrorAction SilentlyContinue
  $existing = Test-Path (Join-Path $InstallDir 'networker_dashboard.py')
  $mode = if ($Uninstall) { 'uninstall' + $(if ($Purge) { ' (purge)' } else { ' (keep data)' }) }
          elseif ($Rollback) { 'rollback (newest backup)' }
          elseif ($Upgrade) { 'upgrade (config + data preserved)' }
          elseif ($svc) { 'REINSTALL over the existing service' }
          elseif ($task) { 'fresh install + MIGRATE from the legacy scheduled task' }
          elseif ($existing) { 'fresh install over existing files (data kept)' }
          else { 'fresh install' }
  $probeHost = Get-NwdashProbeHost -BindHost $BindHost
  Write-NwdashLog 'DRY-RUN (-Check): resolved plan - nothing will be changed'
  Write-NwdashLog "  bundle version : $BundleVersion ($Bundle)"
  Write-NwdashLog "  mode           : $mode"
  Write-NwdashLog "  install dir    : $InstallDir"
  Write-NwdashLog "  service        : $SvcName (nssm), account: $(if ($ServiceAccount) { $ServiceAccount } else { 'LocalSystem' })"
  Write-NwdashLog "  program        : $InstallDir\runtime\python\python.exe"
  Write-NwdashLog "  arguments      : $(Get-NwdashLauncherArgs -P $Port -B $BindHost)"
  Write-NwdashLog "  logs           : $InstallDir\logs\service.out.log / service.err.log (rotate at 10 MB)"
  Write-NwdashLog "  port / bind    : $Port / $(if ($BindHost) { $BindHost } else { 'all interfaces (0.0.0.0)' })"
  Write-NwdashLog "  health probe   : https://${probeHost}:$Port/api/health (trust-all compiled delegate)"
  Write-NwdashLog "  firewall       : $(if ($NoFirewall) { 'skipped (-NoFirewall)' } else { "inbound TCP $Port (group $SvcName)" })"
  Write-NwdashLog "  auth password  : $(if ($AuthPassword) { 'set on first boot (salted hash), then removed from the service env' } else { 'not set here' })"
  Write-NwdashLog "  allowed hosts  : $(if ($AllowedHosts) { $AllowedHosts } else { '(unrestricted)' })"
  Write-NwdashLog "  existing state : service=$(if ($svc) { "$($svc.Status)" } else { 'none' }) task=$(if ($task) { 'present (will be migrated)' } else { 'none' }) files=$(if ($existing) { 'present' } else { 'none' })"
  Write-NwdashLog "  backups        : $InstallDir\backups\<version>-<timestamp>\{app,runtime} (keep 3 newest)"
  Write-NwdashLog '  never touched  : data\, .certs\, logs\, backups\'
  $names = @(Get-NwdashBackups -InstallDir $InstallDir)
  if ($names.Count) { Write-NwdashLog "  backups found  : $($names -join ', ')" }
  exit 0
}

# ---- elevation (everything below changes service/firewall/files) ----
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
if (-not $isAdmin) {
  if ($Elevated) { Stop-Nwdash 'still not elevated after the UAC relaunch - elevation was declined. Re-run from an Administrator prompt.' }
  Write-NwdashLog 'requesting administrator privileges (UAC)...'
  # Rebuild the full argument list so the elevated child runs the SAME plan
  # (DEDB's elevate-without-args gotcha: the child silently used defaults).
  $fwd = @()
  foreach ($k in $PSBoundParameters.Keys) {
    $v = $PSBoundParameters[$k]
    if ($v -is [switch] -or $v -is [bool]) { if ($v) { $fwd += "-$k" } }
    else { $fwd += "-$k"; $fwd += "`"$v`"" }
  }
  $fwd += '-Elevated'
  try {
    $p = Start-Process powershell.exe -Verb RunAs -Wait -PassThru -ArgumentList (@('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"") + $fwd)
    exit $p.ExitCode
  } catch {
    Stop-Nwdash 'elevation was declined or failed. Re-run this script from an Administrator prompt.'
  }
}

# ---- uninstall ----
if ($Uninstall) {
  Write-NwdashLog "uninstalling NetWorker Dashboard from $InstallDir"
  $svc = Get-Service -Name $SvcName -ErrorAction SilentlyContinue
  if ($svc) { Remove-NwdashService -InstallDir $InstallDir; Write-NwdashLog 'service : removed' }
  else { Write-NwdashLog 'service : none installed' }
  # Legacy scheduled-task install (pre-service versions) - remove it too.
  $task = Get-ScheduledTask -TaskName $SvcName -ErrorAction SilentlyContinue
  if ($task) {
    try { Stop-ScheduledTask -TaskName $SvcName -ErrorAction Stop } catch { }
    try { Unregister-ScheduledTask -TaskName $SvcName -Confirm:$false -ErrorAction Stop; Write-NwdashLog 'task    : legacy boot task removed' } catch { Write-NwdashWarn "task    : removal failed ($($_.Exception.Message))" }
  }
  # Instances started by hand with a host python are not under InstallDir -
  # match them by command line (ported from the old Setup-NWDash uninstall).
  $killed = 0
  Get-CimInstance Win32_Process -Filter "Name='python.exe' or Name='py.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'networker_dashboard\.py' } |
    ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; $killed++ } catch { } }
  if ($killed) { Write-NwdashLog "process : $killed stopped"; Start-Sleep -Seconds 1 }
  Remove-NwdashFirewall
  if (-not (Test-Path $InstallDir)) { Write-NwdashLog 'files   : nothing to remove'; exit 0 }
  if ($Purge) {
    Invoke-NwdashFileOp { Remove-Item -LiteralPath $InstallDir -Recurse -Force }
    Write-NwdashLog "files   : removed $InstallDir (INCLUDING data\, .certs\, logs\ - purged)"
  } else {
    foreach ($item in ($script:NwdashAppItems + @('runtime', 'backups', 'nssm.exe'))) {
      $p = Join-Path $InstallDir $item
      if (Test-Path $p) {
        try { Invoke-NwdashFileOp { Remove-Item -LiteralPath $p -Recurse -Force } -Tries 3 -DelayMs 1000 }
        catch { Write-NwdashWarn "could not remove $p ($($_.Exception.Message)) - delete it manually" }
      }
    }
    Write-NwdashLog "files   : application removed; kept data\, .certs\, logs\ in $InstallDir"
    Write-NwdashLog '          (re-run with -Uninstall -Purge to delete those too)'
  }
  Write-NwdashLog 'uninstall complete'
  exit 0
}

# ---- upgrade / rollback (non-destructive; data\, .certs\, logs\, backups\ untouched) ----
if ($Upgrade -or $Rollback) {
  if (-not (Get-Service -Name $SvcName -ErrorAction SilentlyContinue)) { Stop-Nwdash "no $SvcName service installed - run a normal install first (not -Upgrade/-Rollback)" }
  if (-not (Test-Path "$InstallDir\nssm.exe")) { Stop-Nwdash "no nssm.exe at $InstallDir - not an existing install" }
  if ($Upgrade -and -not (Test-Path $AppSrc)) { Stop-Nwdash 'app\ not found in the bundle - run from inside an unpacked bundle' }
  $nssm = "$InstallDir\nssm.exe"

  # The existing service's own command line is authoritative for port/bind (the
  # upgraded service reuses the same config). Read through the UTF-16 decode.
  $svcArgs = Read-NwdashServiceArgs -InstallDir $InstallDir -DefaultPort $Port
  $livePort = $svcArgs.Port
  $liveBind = $svcArgs.Bind
  $probeHost = Get-NwdashProbeHost -BindHost $liveBind
  $health = "https://${probeHost}:$livePort/api/health"
  Write-NwdashLog "existing service uses port $livePort$(if ($liveBind) { ", bind $liveBind" })"

  $ts = Get-Date -Format 'yyyyMMdd-HHmmss'
  $oldVer = Get-NwdashInstalledVersion -InstallDir $InstallDir
  # Mark where the service logs end BEFORE touching anything, so a failed gate
  # prints exactly what THIS deploy wrote (see Show-NwdashFailureLog).
  $outLen0 = Get-NwdashLogLength -InstallDir $InstallDir -Name 'service.out.log'
  $errLen0 = Get-NwdashLogLength -InstallDir $InstallDir -Name 'service.err.log'

  # Suppress nssm auto-restart across the whole stop -> swap -> start window
  # (the DEDB resurrection gotcha: AppExit=Restart resurrects the process that
  # Wait-NwdashCleanStop force-kills mid-swap). The finally below GUARANTEES it
  # is restored on every exit path.
  Set-NwdashNssmAutoRestart -Nssm $nssm -Enabled:$false
  try {
    if ($Rollback) {
      $names = @(Get-NwdashBackups -InstallDir $InstallDir)
      if (-not $names.Count) { Stop-Nwdash 'no backup under backups\ to roll back to' }
      $target = $names[0]
      Write-NwdashLog "rolling back to backups\$target"
      # nssm stop can emit STOP_PENDING to stderr on a slow drain; under PS 5.1
      # EAP=Stop that becomes a TERMINATING error even with 2>$null (DEDB PR
      # #144) - so every nssm stop is try/catch-wrapped. Wait-NwdashCleanStop +
      # Wait-NwdashServiceState are the real arbiters that the SCM settled.
      if ((Get-Service $SvcName -EA SilentlyContinue).Status -ne 'Stopped') { try { & $nssm stop $SvcName 2>$null | Out-Null } catch { } }
      [void](Wait-NwdashCleanStop -InstallDir $InstallDir -Port $livePort -BindAddr $liveBind)
      [void](Wait-NwdashServiceState -State 'Stopped' -TimeoutSec 30)
      # Move the current (bad) state aside so the rollback itself is restorable.
      # 'undone-' prefixed backups are EXCLUDED from Get-NwdashBackups so a
      # second -Rollback can never restore the build we just rolled away from.
      Backup-NwdashApp -InstallDir $InstallDir -Name "undone-$oldVer-$ts"
      Restore-NwdashApp -InstallDir $InstallDir -Name $target
      $restoreOnFail = "undone-$oldVer-$ts"
      $toVer = Get-NwdashInstalledVersion -InstallDir $InstallDir
      Write-NwdashLog "restored $toVer (was $oldVer)"
    } else {
      if (-not (Test-Path "$InstallDir\networker_dashboard.py")) { Stop-Nwdash "no existing app at $InstallDir - run a normal install first" }
      Write-NwdashLog "upgrading NetWorker Dashboard $oldVer -> $BundleVersion (config + data preserved)"
      if ((Get-Service $SvcName -EA SilentlyContinue).Status -ne 'Stopped') { try { & $nssm stop $SvcName 2>$null | Out-Null } catch { } }
      [void](Wait-NwdashCleanStop -InstallDir $InstallDir -Port $livePort -BindAddr $liveBind)
      # Confirm the SCM actually settled to Stopped (not STOP_PENDING) before
      # the file swap, so a stale STOP_PENDING cannot strand the later start.
      [void](Wait-NwdashServiceState -State 'Stopped' -TimeoutSec 30)
      Backup-NwdashApp -InstallDir $InstallDir -Name "$oldVer-$ts"
      $restoreOnFail = "$oldVer-$ts"
      foreach ($item in (Get-ChildItem $AppSrc)) {
        Invoke-NwdashFileOp { Copy-Item -Recurse -Force $item.FullName (Join-Path $InstallDir $item.Name) }
      }
      New-Item -ItemType Directory -Force "$InstallDir\runtime" | Out-Null
      Invoke-NwdashFileOp { Copy-Item -Recurse -Force $PySrc "$InstallDir\runtime\python" }
    }

    # Even after the clean stop, the OS can hold the listening socket a beat.
    if (-not (Wait-NwdashPortFree -Port $livePort -TimeoutSec 20 -HostName $probeHost)) {
      Write-NwdashWarn "port $livePort still held after stop - starting anyway (app bind-retry will absorb it)"
    }
    # Restore auto-restart BEFORE starting so nssm keeps the NEW app alive if
    # it crashes later; a failed verified start falls to the health gate.
    Set-NwdashNssmAutoRestart -Nssm $nssm -Enabled:$true
    if (-not (Start-NwdashServiceVerified -Nssm $nssm)) {
      Write-NwdashWarn 'verified start failed - falling through to health gate + rollback'
    }

    Write-NwdashLog 'health check (allow time for first boot + bind-retry)'
    # Require several CONSECUTIVE healthy probes (guards a flapping bind);
    # only give up after a SUSTAINED Stopped streak, never a single miss.
    $ok = $false; $downStreak = 0; $upStreak = 0
    $maxPolls = 75; $needUp = 3; $maxDown = 8   # 75 * 2s = ~150s budget
    for ($i = 0; $i -lt $maxPolls; $i++) {
      if (Test-NwdashHealth -Url $health) {
        $upStreak++; $downStreak = 0
        if ($upStreak -ge $needUp) { $ok = $true; break }
      } else {
        $upStreak = 0
        $cur = Get-Service -Name $SvcName -ErrorAction SilentlyContinue
        if ($cur -and $cur.Status -eq 'Stopped') { $downStreak++ } else { $downStreak = 0 }
        if ($downStreak -ge $maxDown) { break }
      }
      Start-Sleep 2
    }
    if ($ok) {
      if ($Upgrade) { Remove-OldNwdashBackups -InstallDir $InstallDir -Keep 3 }
      Write-NwdashLog "OK - NetWorker Dashboard healthy at $health"
      exit 0
    }

    # Failed health -> restore the state captured at the start of this run.
    Write-NwdashWarn 'health check failed - rolling back to the pre-deploy state'
    Show-NwdashFailureLog -InstallDir $InstallDir -SinceOut $outLen0 -SinceErr $errLen0 -ProbeUrl $health
    if ((Get-Service $SvcName -EA SilentlyContinue).Status -ne 'Stopped') { try { & $nssm stop $SvcName 2>$null | Out-Null } catch { } }
    [void](Wait-NwdashCleanStop -InstallDir $InstallDir -Port $livePort -BindAddr $liveBind)
    [void](Wait-NwdashServiceState -State 'Stopped' -TimeoutSec 30)
    Restore-NwdashApp -InstallDir $InstallDir -Name $restoreOnFail
    if (-not (Wait-NwdashPortFree -Port $livePort -TimeoutSec 20 -HostName $probeHost)) {
      Write-NwdashWarn "port $livePort still held after stop - starting anyway (app bind-retry will absorb it)"
    }
    Set-NwdashNssmAutoRestart -Nssm $nssm -Enabled:$true
    if (-not (Start-NwdashServiceVerified -Nssm $nssm)) {
      Write-NwdashWarn "ROLLBACK: service did not auto-start - run: Start-Service $SvcName"
    }
    Stop-Nwdash 'deploy failed - rolled back to the pre-deploy state'
  } finally {
    # Guarantee nssm auto-restart is back ON for EVERY exit path so we never
    # strand the service with self-healing disabled.
    Set-NwdashNssmAutoRestart -Nssm $nssm -Enabled:$true
  }
}

# ---- fresh install (+ migration from the legacy scheduled-task install) ----
if (-not (Test-Path $AppSrc)) { Stop-Nwdash 'app\ not found in the bundle - run deploy\install.ps1 from inside an unpacked bundle' }
if (-not (Test-Path "$PySrc\python.exe")) { Stop-Nwdash "bundled python not found at $PySrc\python.exe" }
if (-not (Test-Path $NssmSrc)) { Stop-Nwdash "bundled nssm.exe not found at $NssmSrc" }

Write-NwdashLog "installing NetWorker Dashboard $BundleVersion to $InstallDir"

# 1/6 migrate: stop + unregister the legacy scheduled task; data\ and .certs\
# stay in place, so saved sessions/keys/snapshots survive the migration.
$task = Get-ScheduledTask -TaskName $SvcName -ErrorAction SilentlyContinue
if ($task) {
  Write-NwdashLog "[1/6] migrating from the legacy scheduled-task install (task '$SvcName' found)"
  try { Stop-ScheduledTask -TaskName $SvcName -ErrorAction Stop } catch { }
  try { Unregister-ScheduledTask -TaskName $SvcName -Confirm:$false -ErrorAction Stop; Write-NwdashLog 'legacy boot task removed' } catch { Write-NwdashWarn "could not unregister the legacy task ($($_.Exception.Message))" }
} else {
  Write-NwdashLog '[1/6] no legacy scheduled task found'
}
# Stop any running instance (service-managed instances live under InstallDir;
# hand-started ones are matched by command line, as the old setup did).
$svc = Get-Service -Name $SvcName -ErrorAction SilentlyContinue
if ($svc) { Write-NwdashLog "removing existing $SvcName service for a clean reinstall"; Remove-NwdashService -InstallDir $InstallDir }
$killed = 0
Get-CimInstance Win32_Process -Filter "Name='python.exe' or Name='py.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -match 'networker_dashboard\.py' } |
  ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; $killed++ } catch { } }
if ($killed) { Write-NwdashLog "stopped $killed running instance(s)"; Start-Sleep -Seconds 1 }

# 2/6 copy files. Old app files are removed first (overwrite-in-place leaves
# stale modules behind when a newer bundle deletes or renames files); data\,
# .certs\, logs\, backups\ are never in the removal list.
Write-NwdashLog '[2/6] copying application + embedded runtime'
New-Item -ItemType Directory -Force $InstallDir | Out-Null
foreach ($item in $script:NwdashAppItems) {
  $p = Join-Path $InstallDir $item
  if (Test-Path $p) { Invoke-NwdashFileOp { Remove-Item -LiteralPath $p -Recurse -Force } }
}
foreach ($item in (Get-ChildItem $AppSrc)) {
  Invoke-NwdashFileOp { Copy-Item -Recurse -Force $item.FullName (Join-Path $InstallDir $item.Name) }
}
New-Item -ItemType Directory -Force "$InstallDir\runtime" | Out-Null
if (Test-Path "$InstallDir\runtime\python") { Invoke-NwdashFileOp { Remove-Item -Recurse -Force "$InstallDir\runtime\python" } }
Invoke-NwdashFileOp { Copy-Item -Recurse -Force $PySrc "$InstallDir\runtime\python" }
# Don't overwrite a possibly-locked nssm.exe (the Event Log service holds a
# handle on it). It is a pinned version - reuse the existing copy if present.
if (-not (Test-Path "$InstallDir\nssm.exe")) {
  Copy-Item -Force $NssmSrc "$InstallDir\nssm.exe"
} else {
  try { Copy-Item -Force $NssmSrc "$InstallDir\nssm.exe" -ErrorAction Stop }
  catch { Write-NwdashLog 'nssm.exe already present and in use - reusing it (same pinned version)' }
}
New-Item -ItemType Directory -Force "$InstallDir\logs" | Out-Null

# 3/6 service
Write-NwdashLog "[3/6] installing NSSM service $SvcName"
$nssm = "$InstallDir\nssm.exe"
$launcherArgs = Get-NwdashLauncherArgs -P $Port -B $BindHost
& $nssm install $SvcName "$InstallDir\runtime\python\python.exe" | Out-Null
& $nssm set $SvcName AppParameters $launcherArgs | Out-Null
& $nssm set $SvcName AppDirectory $InstallDir | Out-Null
& $nssm set $SvcName AppStdout "$InstallDir\logs\service.out.log" | Out-Null
& $nssm set $SvcName AppStderr "$InstallDir\logs\service.err.log" | Out-Null
& $nssm set $SvcName AppRotateFiles 1 | Out-Null
& $nssm set $SvcName AppRotateBytes 10485760 | Out-Null
& $nssm set $SvcName AppExit Default Restart | Out-Null
& $nssm set $SvcName Start SERVICE_AUTO_START | Out-Null
& $nssm set $SvcName Description 'Dell NetWorker backup and recovery status dashboard (HTTPS)' | Out-Null
if ($ServiceAccount) { & $nssm set $SvcName ObjectName $ServiceAccount $ServicePassword | Out-Null }

# Service environment: the app reads DASHBOARD_AUTH_PASSWORD once at boot and
# persists a salted PBKDF2 hash in data\auth.json; the plaintext is REMOVED
# from the service env right after the first healthy boot (below), so it never
# stays in the registry. DASHBOARD_ALLOWED_HOSTS is not a secret and persists.
$envExtra = @()
if ($AllowedHosts) { $envExtra += "DASHBOARD_ALLOWED_HOSTS=$AllowedHosts" }
if ($AuthPassword) { $envExtra += "DASHBOARD_AUTH_PASSWORD=$AuthPassword" }
if ($envExtra.Count) { & $nssm set $SvcName AppEnvironmentExtra $envExtra | Out-Null }

# 4/6 firewall
if ($NoFirewall) { Write-NwdashLog '[4/6] firewall: skipped (-NoFirewall)' }
else { Write-NwdashLog '[4/6] configuring firewall'; Set-NwdashFirewall -Port $Port }

# 5/6 start (verified) - warn first if a foreign process already owns the port.
Write-NwdashLog '[5/6] starting service'
$u = Test-NwdashPortInUse -Port $Port -LocalAddress $BindHost
if ($u -and $u.ProcessName -and $u.ProcessName -notin @('python', 'nssm')) {
  Write-NwdashWarn "port $Port is already in use by '$($u.ProcessName)' (PID $($u.ProcessId)). Stop it or re-run with -Port <other>; the service may fail to bind."
}
if (-not (Start-NwdashServiceVerified -Nssm $nssm)) {
  Write-NwdashWarn 'service did not reach Running - the health gate below decides'
}

# 6/6 health gate (~120s: first boot generates a TLS cert and may bind-retry).
Write-NwdashLog '[6/6] waiting for service health'
$probeHost = Get-NwdashProbeHost -BindHost $BindHost
$health = "https://${probeHost}:$Port/api/health"
$ok = $false; $downStreak = 0
for ($i = 0; $i -lt 60; $i++) {
  if (Test-NwdashHealth -Url $health) { $ok = $true; break }
  $cur = Get-Service -Name $SvcName -ErrorAction SilentlyContinue
  if ($cur -and $cur.Status -eq 'Stopped') { $downStreak++ } else { $downStreak = 0 }
  if ($downStreak -ge 5) { Write-NwdashWarn "$SvcName service is not staying up"; break }
  Start-Sleep 2
}
if (-not $ok) {
  Write-NwdashWarn 'health check failed; recent service logs:'
  Show-NwdashFailureLog -InstallDir $InstallDir -ProbeUrl $health
  Stop-Nwdash 'service did not become healthy'
}

# The app has persisted the salted hash by now - remove the plaintext password
# from the service environment (keep the non-secret allow-list).
if ($AuthPassword) {
  $keepEnv = @()
  if ($AllowedHosts) { $keepEnv += "DASHBOARD_ALLOWED_HOSTS=$AllowedHosts" }
  try {
    if ($keepEnv.Count) { & $nssm set $SvcName AppEnvironmentExtra $keepEnv | Out-Null }
    else { & $nssm reset $SvcName AppEnvironmentExtra 2>$null | Out-Null }
    Write-NwdashLog 'auth password applied (salted hash stored); plaintext removed from the service env'
  } catch { Write-NwdashWarn 'could not clear DASHBOARD_AUTH_PASSWORD from the service env - clear it manually (nssm reset NetWorkerDashboard AppEnvironmentExtra)' }
}

$urlHost = if ($BindHost) { $BindHost } else { 'localhost' }
Write-NwdashLog "OK - NetWorker Dashboard $BundleVersion is healthy"
Write-NwdashLog "  URL     : https://${urlHost}:$Port/"
Write-NwdashLog "  service : $SvcName (services.msc) - crash auto-restart, starts at boot"
Write-NwdashLog "  logs    : $InstallDir\logs\service.out.log / service.err.log (10 MB rotation)"
Write-NwdashLog "  data    : $InstallDir\data (keys, snapshots, profiles - survives upgrades)"
exit 0
