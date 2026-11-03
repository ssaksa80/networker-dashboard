# Unit tests (no real service, injected probes) for the deterministic
# stop/start helpers ported from DEDB: Wait-NwdashServiceState,
# Start-NwdashServiceVerified and Wait-NwdashCleanStop. Run:
#   powershell -NoProfile -ExecutionPolicy Bypass -File deploy\test\service-start.test.ps1
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\..\lib\common.ps1"
function Assert($cond, $msg) { if (-not $cond) { throw "FAIL: $msg" } else { Write-Host "  PASS $msg" } }

# ---- Wait-NwdashServiceState: reaches the state after a few polls ----
$script:seq = @('StopPending', 'StopPending', 'Stopped')
$script:idx = 0
$got = Wait-NwdashServiceState -State 'Stopped' -TimeoutSec 5 -PollMs 1 `
  -GetState { param($n) $s = $script:seq[[Math]::Min($script:idx, $script:seq.Count - 1)]; $script:idx++; $s } `
  -Sleep { param($ms) }
Assert $got 'Wait-NwdashServiceState: returns $true once the SCM settles to Stopped'
Assert ($script:idx -ge 3) 'Wait-NwdashServiceState: polled through the STOP_PENDING window'

# ---- Wait-NwdashServiceState: times out without ever matching ----
$got = Wait-NwdashServiceState -State 'Running' -TimeoutSec 1 -PollMs 500 `
  -GetState { param($n) 'Stopped' } -Sleep { param($ms) }
Assert (-not $got) 'Wait-NwdashServiceState: returns $false at timeout'

# ---- Start-NwdashServiceVerified: succeeds on a later retry ----
$script:starts = 0
$script:waits = 0
$script:logMsgs = @()
$got = Start-NwdashServiceVerified -Nssm 'unused' -Retries 3 -SettleSec 1 `
  -StartSvc { $script:starts++ } `
  -WaitState { param($st, $to) $script:waits++; ($script:waits -ge 2) } `
  -GetState { 'Stopped' } `
  -Log { param($m) $script:logMsgs += $m }
Assert $got 'Start-NwdashServiceVerified: returns $true when a retry reaches Running'
Assert ($script:starts -eq 2) 'Start-NwdashServiceVerified: issued exactly 2 start attempts'

# ---- Start-NwdashServiceVerified: a throwing start never propagates ----
$got = Start-NwdashServiceVerified -Nssm 'unused' -Retries 2 -SettleSec 1 `
  -StartSvc { throw 'nssm stderr noise' } `
  -WaitState { param($st, $to) $false } `
  -GetState { '' } `
  -Log { param($m) }
Assert (-not $got) 'Start-NwdashServiceVerified: returns $false after every attempt fails (no throw)'

# ---- Wait-NwdashCleanStop: clean when procs gone and port free ----
$got = Wait-NwdashCleanStop -InstallDir 'C:\nowhere' -Port 8443 -GraceSec 1 `
  -GetProcs { param($d) @() } `
  -TestPort { param($p) $false } `
  -GetPortOwner { param($p) 0 } `
  -KillProc { param($id) throw 'must not kill anything on a clean stop' } `
  -Sleep { param($ms) }
Assert $got 'Wait-NwdashCleanStop: clean when no procs and port free'

# ---- Wait-NwdashCleanStop: force-kills a straggler, then reports clean ----
$script:killed = @()
$script:alive = $true
$got = Wait-NwdashCleanStop -InstallDir 'C:\nowhere' -Port 8443 -GraceSec 1 `
  -GetProcs { param($d) if ($script:alive) { @([pscustomobject]@{ ProcessId = 4242 }) } else { @() } } `
  -TestPort { param($p) $script:alive } `
  -GetPortOwner { param($p) 0 } `
  -KillProc { param($id) $script:killed += $id; $script:alive = $false } `
  -Sleep { param($ms) }
Assert $got 'Wait-NwdashCleanStop: clean after force-killing the straggler'
Assert ($script:killed -contains 4242) 'Wait-NwdashCleanStop: killed the leftover install-dir process'

# ---- Wait-NwdashCleanStop: UNSCOPED port owner is never force-killed ----
# (co-tenant safety: with no pinned bind address the first listener on the
# port may belong to ANOTHER app - unscoped means DO NOT TOUCH.)
$script:ownerKills = @()
$got = Wait-NwdashCleanStop -InstallDir 'C:\nowhere' -Port 8443 -GraceSec 1 -BindAddr '' `
  -GetProcs { param($d) @() } `
  -TestPort { param($p) $true } `
  -GetPortOwner { param($p) 9999 } `
  -KillProc { param($id) $script:ownerKills += $id } `
  -Sleep { param($ms) }
Assert (-not $got) 'Wait-NwdashCleanStop: reports not-clean when the port stays held'
Assert ($script:ownerKills.Count -eq 0) 'Wait-NwdashCleanStop: NEVER kills the port owner when unscoped (co-tenant safety)'

# ---- Wait-NwdashCleanStop: scoped (pinned) port owner IS force-killed ----
$script:ownerKills = @()
$script:held = $true
$got = Wait-NwdashCleanStop -InstallDir 'C:\nowhere' -Port 8443 -GraceSec 1 -BindAddr '10.0.0.5' `
  -GetProcs { param($d) @() } `
  -TestPort { param($p) $script:held } `
  -GetPortOwner { param($p) 7777 } `
  -KillProc { param($id) $script:ownerKills += $id; $script:held = $false } `
  -Sleep { param($ms) }
Assert $got 'Wait-NwdashCleanStop: clean after killing the scoped port owner'
Assert ($script:ownerKills -contains 7777) 'Wait-NwdashCleanStop: kills the port owner when scoped to our pinned IP'

# ---- backup name ordering (Get-NwdashBackups) ----
$tmp = Join-Path $env:TEMP ("nwdash-baktest-" + [guid]::NewGuid().ToString('N'))
foreach ($n in '2.8.2-20260101-010101', '2.8.1-20251231-235959', '2.9.0-20260301-120000', 'undone-2.9.1-20260401-120000', 'not-a-backup') {
  New-Item -ItemType Directory -Force "$tmp\backups\$n\app" | Out-Null
  Set-Content "$tmp\backups\$n\app\networker_dashboard.py" $n
}
# Emptied by a restore: the folders stay, no file does (review O3).
New-Item -ItemType Directory -Force "$tmp\backups\2.9.2-20260501-120000\app\nwdash", "$tmp\backups\2.9.2-20260501-120000\runtime" | Out-Null
try {
  $names = @(Get-NwdashBackups -InstallDir $tmp)
  Assert ($names.Count -eq 3) 'Get-NwdashBackups: only real backups listed (undone-*, junk and spent excluded)'
  Assert ($names[0] -eq '2.9.0-20260301-120000') 'Get-NwdashBackups: newest first (by timestamp), a newer spent one skipped'
  Assert ($names[2] -eq '2.8.1-20251231-235959') 'Get-NwdashBackups: oldest last'
  $spent = @(Get-NwdashBackups -InstallDir $tmp -Spent)
  Assert (($spent.Count -eq 1) -and ($spent[0] -eq '2.9.2-20260501-120000')) 'Get-NwdashBackups -Spent: lists only the emptied one'
  Assert (-not (Test-NwdashBackupSpent -Path "$tmp\backups\no-such-folder")) 'Test-NwdashBackupSpent: a folder it cannot read is not spent'

  Remove-NwdashSpentBackup -InstallDir $tmp -Name '2.9.0-20260301-120000'
  Assert (Test-Path "$tmp\backups\2.9.0-20260301-120000\app\networker_dashboard.py") 'Remove-NwdashSpentBackup: a backup with files is kept'
  Remove-NwdashSpentBackup -InstallDir $tmp -Name '2.9.2-20260501-120000'
  Assert (-not (Test-Path "$tmp\backups\2.9.2-20260501-120000")) 'Remove-NwdashSpentBackup: a spent backup is removed'

  # ---- what a -Rollback needs from its backup (Get-NwdashBackupProblem) ----
  $bak = "$tmp\backups\2.8.2-20260101-010101"
  Assert ((Get-NwdashBackupProblem -InstallDir $tmp -Name '2.8.2-20260101-010101') -eq 'app\nwdash\config.py, runtime\python\python.exe') 'Get-NwdashBackupProblem: names every missing part'
  New-Item -ItemType Directory -Force "$bak\app\nwdash", "$bak\runtime\python" | Out-Null
  Set-Content "$bak\app\nwdash\config.py" 'APP_VERSION = "2.8.2"'
  Set-Content "$bak\runtime\python\python.exe" 'python'
  Assert ((Get-NwdashBackupProblem -InstallDir $tmp -Name '2.8.2-20260101-010101') -eq '') 'Get-NwdashBackupProblem: a whole backup has none'
  Assert ((Get-NwdashBackupProblem -InstallDir $tmp -Name 'no-such-backup') -eq 'app\networker_dashboard.py, app\nwdash\config.py, runtime\python\python.exe') 'Get-NwdashBackupProblem: a missing backup lacks everything'
} finally {
  Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}

# ---- health URL normalisation ----
Assert ((Resolve-NwdashHealthUrl 'https://127.0.0.1:8443') -eq 'https://127.0.0.1:8443/api/health') 'Resolve-NwdashHealthUrl: bare origin'
Assert ((Resolve-NwdashHealthUrl 'https://127.0.0.1:8443/') -eq 'https://127.0.0.1:8443/api/health') 'Resolve-NwdashHealthUrl: trailing slash'
Assert ((Resolve-NwdashHealthUrl 'https://127.0.0.1:8443/api/health') -eq 'https://127.0.0.1:8443/api/health') 'Resolve-NwdashHealthUrl: already-full URL not doubled'

Write-Host 'ALL PASS'
