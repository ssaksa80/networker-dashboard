# Regression test for review O1: an -Upgrade/-Rollback that fails partway must
# put the previous version back, start the service and exit non-zero. And for
# review O3: the backup a -Rollback restores is used up, so the next -Rollback
# goes one further back, and a backup without the app is refused before the stop.
#
# Most of what can go wrong lives in install.ps1's control flow, not in a helper,
# so this runs the REAL upgrade/rollback block of install.ps1 against a temp
# install, in a child process (the block ends in `exit`). Only the service side
# is faked: Get-Service, nssm, the health probe, Start-Sleep. The file locks are
# real handles, because what a lock does to a directory move is the bug. Run:
#   powershell -NoProfile -ExecutionPolicy Bypass -File deploy\test\swap-recovery.test.ps1
param([string]$RunIn = '', [string]$Mode = 'Upgrade', [string]$Inject = 'none')
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\..\lib\common.ps1"

# ---- child: the real block, with the service side faked ----
if ($RunIn) {
  $InstallDir = "$RunIn\install"
  $AppSrc = "$RunIn\bundle\app"
  $PySrc = "$RunIn\bundle\runtime\python"
  $Port = 8443
  $BundleVersion = '3.0.0'
  $SvcName = $script:NwdashServiceName
  [switch]$Upgrade = ($Mode -eq 'Upgrade')
  [switch]$Rollback = ($Mode -eq 'Rollback')
  [switch]$AllowUnauthenticated = $false
  # Set above the block once the folder lock (review S5) lands; any value skips
  # the lock, which needs real accounts. Unused until then.
  $aclProblem = 'not under test here'
  $script:held = $null

  function Get-Service {
    [CmdletBinding()] param([Parameter(Position = 0)][string]$Name)
    [pscustomobject]@{ Status = (Get-Content -Raw "$RunIn\service-state").Trim() }
  }
  function Invoke-FakeNssm {
    Add-Content "$RunIn\nssm.log" ($args -join ' ')
    if ($args[0] -eq 'stop') { Set-Content "$RunIn\service-state" 'Stopped' }
    if ($args[0] -eq 'start' -and $Inject -ne 'start-fails') { Set-Content "$RunIn\service-state" 'Running' }
  }
  function Read-NwdashServiceArgs { [pscustomobject]@{ Port = 59443; Bind = '' } }
  function Wait-NwdashCleanStop {
    if ($Inject -eq 'stop-throws') { throw 'simulated failure while waiting for the stop' }
    return $true
  }
  # A lock taken with -Inject lock-clears lets go at the first retry back-off.
  function Start-Sleep {
    param([Parameter(Position = 0)][int]$Seconds, [int]$Milliseconds)
    if ($Inject -eq 'lock-clears' -and $script:held) { $script:held.Dispose(); $script:held = $null }
  }
  function Test-NwdashHealth {
    param([string]$Url, [int]$TimeoutSec = 5)
    if ($Inject -eq 'health-fails-locked' -and -not $script:held) {
      $script:held = [IO.File]::Open("$InstallDir\nwdash\sessions.py", 'Open', 'Read', 'None')
    }
    return ($Inject -notlike 'health-fails*')
  }
  # Both leave a partial copy behind, as a real failed copy does.
  if ($Inject -in @('disk-full', 'copy-locked-once')) {
    $script:copyFailed = $false
    function Copy-Item {
      param([switch]$Recurse, [switch]$Force, [Parameter(Position = 0)][string]$Path, [Parameter(Position = 1)][string]$Destination)
      $full = ($Inject -eq 'disk-full' -and $Path -eq $PySrc)
      $locked = ($Inject -eq 'copy-locked-once' -and $Path -like '*\bundle\app\nwdash' -and -not $script:copyFailed)
      if (-not ($full -or $locked)) { Microsoft.PowerShell.Management\Copy-Item -Recurse -Force $Path $Destination; return }
      $script:copyFailed = $true
      New-Item -ItemType Directory -Force $Destination | Out-Null
      Set-Content "$Destination\half-written.tmp" 'half written'
      if ($full) { throw 'There is not enough space on the disk.' }
      throw 'The process cannot access the file because it is being used by another process.'
    }
  }
  if ($Inject -eq 'lock-clears') { $script:held = [IO.File]::Open("$InstallDir\nwdash\sessions.py", 'Open', 'Read', 'None') }

  $src = Get-Content -Raw "$PSScriptRoot\..\install.ps1"
  $from = $src.IndexOf('# ---- upgrade / rollback')
  $to = $src.IndexOf('# ---- fresh install')
  if ($from -lt 0 -or $to -lt $from) { throw 'upgrade/rollback block markers not found in install.ps1' }
  $block = $src.Substring($from, $to - $from)
  $real = '$nssm = "$InstallDir\nssm.exe"'
  if (-not $block.Contains($real)) { throw 'nssm assignment not found in the upgrade/rollback block' }
  . ([scriptblock]::Create($block.Replace($real, '$nssm = ${function:Invoke-FakeNssm}')))
  exit 99   # the block always ends in exit or a terminating error
}

# ---- parent: the scenarios ----
function Assert($cond, $msg) { if (-not $cond) { throw "FAIL: $msg" } else { Write-Host "  PASS $msg" } }

$script:scratch = @()

# The tests lock nwdash\sessions.py: not config.py, which the installer reads for
# the version, and with admin.py sorting before it, so a file-by-file move gets
# partway through the folder before it hits the lock.
function New-Payload([string]$App, [string]$Runtime, [string]$Version) {
  New-Item -ItemType Directory -Force "$App\nwdash\assets", "$Runtime\Lib" | Out-Null
  foreach ($f in 'networker_dashboard.py', 'README.md', 'pyproject.toml', 'nwdash\admin.py', 'nwdash\sessions.py', 'nwdash\assets\app.js') {
    Set-Content "$App\$f" "$Version $f"
  }
  Set-Content "$App\nwdash\config.py" "APP_VERSION = `"$Version`""
  Set-Content "$Runtime\python.exe" "$Version python.exe"
  Set-Content "$Runtime\Lib\os.py" "$Version os.py"
}

# Installed 2.0.0, one older backup (1.0.0), an unpacked 3.0.0 bundle.
function New-Fixture {
  $d = Join-Path $env:TEMP ("nwdash-swaptest-" + [guid]::NewGuid().ToString('N'))
  $script:scratch += $d
  New-Payload "$d\install" "$d\install\runtime\python" '2.0.0'
  New-Payload "$d\install\backups\$script:Target\app" "$d\install\backups\$script:Target\runtime\python" '1.0.0'
  New-Payload "$d\bundle\app" "$d\bundle\runtime\python" '3.0.0'
  New-Item -ItemType Directory -Force "$d\install\data" | Out-Null
  Set-Content "$d\install\data\auth.json" '{}'
  Set-Content "$d\install\nssm.exe" 'placeholder'
  Set-Content "$d\service-state" 'Running'
  return $d
}
$script:Target = '1.0.0-20260101-010101'
$script:Older = '0.9.0-20251201-010101'
$script:Newer = '1.5.0-20260201-010101'

function Add-Backup([string]$Dir, [string]$Name, [string]$Version) {
  New-Payload "$Dir\install\backups\$Name\app" "$Dir\install\backups\$Name\runtime\python" $Version
}
# What a -Rollback from before review O3 left: the folders, not one file.
function Add-SpentBackup([string]$Dir, [string]$Name) {
  New-Item -ItemType Directory -Force "$Dir\install\backups\$Name\app", "$Dir\install\backups\$Name\runtime" | Out-Null
}

# Every file under $Root with its content, apart from backups\ and data\.
function Get-Tree([string]$Root, [switch]$All) {
  $rows = foreach ($f in @(Get-ChildItem -Recurse -File -Force $Root | Sort-Object FullName)) {
    $rel = $f.FullName.Substring($Root.Length)
    if ($All -or $rel -notmatch '^\\(backups|data)\\') { "$rel = $(Get-Content -Raw $f.FullName)" }
  }
  return ($rows -join '|')
}

function Invoke-Deploy([string]$Dir, [string]$Mode, [string]$Inject = 'none', [string]$Lock = '') {
  $ErrorActionPreference = 'Continue'   # under 5.1 a child's stderr would throw here
  $held = if ($Lock) { [IO.File]::Open($Lock, 'Open', 'Read', 'None') } else { $null }
  try {
    $out = & powershell -NoProfile -ExecutionPolicy Bypass -File $PSCommandPath -RunIn $Dir -Mode $Mode -Inject $Inject 2>&1 | Out-String
    $code = $LASTEXITCODE
  } finally { if ($held) { $held.Dispose() } }
  # Flat: the console wraps long error lines, so match with the whitespace gone.
  return [pscustomobject]@{ Code = $code; Flat = ($out -replace '\s+', ''); Out = $out }
}

function Get-ServiceState([string]$Dir) { (Get-Content -Raw "$Dir\service-state").Trim() }
function Get-NssmLog([string]$Dir) { if (Test-Path "$Dir\nssm.log") { @(Get-Content "$Dir\nssm.log") } else { @() } }

function Get-NewestBackup([string]$Dir) { @(Get-NwdashBackups -InstallDir "$Dir\install") | Select-Object -First 1 }

# The 2.0.0 backup this run made holds nwdash whole: not split, not nested.
function Assert-WholeBackup([string]$Dir, [string]$Case) {
  $bak = Get-NewestBackup $Dir
  Assert ($bak -like '2.0.0-*') "$Case : the old version is in a new backup"
  $want = (Get-Tree "$Dir\install\backups\$script:Target\app\nwdash") -replace '1\.0\.0', '2.0.0'
  Assert ((Get-Tree "$Dir\install\backups\$bak\app\nwdash") -ceq $want) "$Case : that backup holds nwdash whole"
}

function Assert-Recovered($Run, [string]$Dir, [string]$Before, [string]$Case) {
  Assert ($Run.Code -notin @(0, 99)) "$Case : exits non-zero"
  Assert ($Run.Flat -match 'deployfailed') "$Case : says the deploy failed"
  Assert ((Get-Tree "$Dir\install") -ceq $Before) "$Case : the previous app and runtime are back, unchanged"
  Assert ((Get-NewestBackup $Dir) -eq $script:Target) "$Case : -Rollback still reaches the real backup, no emptied folder in front of it"
  Assert ((Get-ServiceState $Dir) -eq 'Running') "$Case : the service is running again"
  Assert ((Get-NssmLog $Dir | Where-Object { $_ -match 'AppExit' } | Select-Object -Last 1) -match 'Restart$') "$Case : nssm auto-restart is back on"
}

try {
  # ---- the harness itself: a clean upgrade still upgrades ----
  $case = 'upgrade, nothing goes wrong'
  $d = New-Fixture
  $run = Invoke-Deploy $d 'Upgrade'
  Assert ($run.Code -eq 0) "$case : exits 0"
  Assert ((Get-Content -Raw "$d\install\nwdash\config.py") -match '3\.0\.0') "$case : the new version is in place"
  Assert-WholeBackup $d $case

  # ---- a refusal stops nothing, so it has nothing to undo ----
  $case = 'rollback, no backup to restore'
  $d = New-Fixture
  Remove-Item -Recurse -Force "$d\install\backups"
  $old = Get-Tree "$d\install"
  $run = Invoke-Deploy $d 'Rollback'
  Assert ($run.Code -notin @(0, 99)) "$case : exits non-zero"
  Assert ($run.Flat -match 'nobackupunderbackups') "$case : says why"
  Assert (-not (Get-NssmLog $d | Where-Object { $_ -match '^(stop|start)' })) "$case : the service is not touched"
  Assert ((Get-Tree "$d\install") -ceq $old) "$case : no file changed"

  # ---- a lock that clears is ridden through, leaving a usable backup ----
  $case = 'upgrade, a lock clears during the retries'
  $d = New-Fixture
  $run = Invoke-Deploy $d 'Upgrade' 'lock-clears'
  Assert ($run.Code -eq 0) "$case : exits 0"
  Assert-WholeBackup $d $case

  # ---- a copy retried after a lock starts clean, or it nests ----
  $case = 'upgrade, the copy hits a lock once'
  $d = New-Fixture
  $run = Invoke-Deploy $d 'Upgrade' 'copy-locked-once'
  Assert ($run.Code -eq 0) "$case : exits 0"
  Assert ((Get-Tree "$d\install\nwdash") -ceq (Get-Tree "$d\bundle\app\nwdash")) "$case : nwdash is exactly the new version, nothing nested or left over"

  # ---- the swap fails while the old version is being moved out ----
  $case = 'upgrade, a file stays locked'
  $d = New-Fixture
  $old = Get-Tree "$d\install"
  $run = Invoke-Deploy $d 'Upgrade' 'none' "$d\install\nwdash\sessions.py"
  Assert-Recovered $run $d $old $case

  $case = 'rollback, a file stays locked'
  $d = New-Fixture
  $old = Get-Tree "$d\install"
  $oldTarget = Get-Tree "$d\install\backups\$script:Target" -All
  $run = Invoke-Deploy $d 'Rollback' 'none' "$d\install\nwdash\sessions.py"
  Assert-Recovered $run $d $old $case
  Assert ((Get-Tree "$d\install\backups\$script:Target" -All) -ceq $oldTarget) "$case : the backup it was rolling back to is untouched"

  # ---- the swap fails while the replacement is going in ----
  $case = 'upgrade, the disk fills during the copy'
  $d = New-Fixture
  $old = Get-Tree "$d\install"
  $run = Invoke-Deploy $d 'Upgrade' 'disk-full'
  Assert-Recovered $run $d $old $case
  Assert ($run.Flat -match 'notenoughspace') "$case : names the cause"

  $case = 'rollback, a file in the backup is locked'
  $d = New-Fixture
  $old = Get-Tree "$d\install"
  $oldTarget = Get-Tree "$d\install\backups\$script:Target" -All
  $run = Invoke-Deploy $d 'Rollback' 'none' "$d\install\backups\$script:Target\app\nwdash\sessions.py"
  Assert-Recovered $run $d $old $case
  Assert ((Get-Tree "$d\install\backups\$script:Target" -All) -ceq $oldTarget) "$case : the backup got back every file it had given up"

  # ---- nothing was moved yet, but the service is already down ----
  $case = 'upgrade, the stop fails before any file moves'
  $d = New-Fixture
  $old = Get-Tree "$d\install"
  $run = Invoke-Deploy $d 'Upgrade' 'stop-throws'
  Assert-Recovered $run $d $old $case

  # ---- the health gate path: the restore must run exactly once ----
  $case = 'upgrade, the health gate fails'
  $d = New-Fixture
  $old = Get-Tree "$d\install"
  $run = Invoke-Deploy $d 'Upgrade' 'health-fails'
  Assert-Recovered $run $d $old $case

  $case = 'rollback, the health gate fails'
  $d = New-Fixture
  $old = Get-Tree "$d\install"
  $oldTarget = Get-Tree "$d\install\backups\$script:Target" -All
  $run = Invoke-Deploy $d 'Rollback' 'health-fails'
  Assert-Recovered $run $d $old $case
  Assert ((Get-Tree "$d\install\backups\$script:Target" -All) -ceq $oldTarget) "$case : the backup it rolled back to keeps its files"

  # ---- restored, but the service will not come back ----
  $case = 'upgrade, the service will not start again'
  $d = New-Fixture
  $old = Get-Tree "$d\install"
  $run = Invoke-Deploy $d 'Upgrade' 'start-fails' "$d\install\nwdash\sessions.py"
  Assert ($run.Code -notin @(0, 99)) "$case : exits non-zero"
  Assert ((Get-Tree "$d\install") -ceq $old) "$case : the previous app and runtime are back, unchanged"
  Assert ($run.Flat -match 'Start-Service') "$case : tells the operator to start it"
  Assert ($run.Flat -notmatch 'rolledbacktothepre-deploystate') "$case : does not claim the rollback worked"

  # ---- the restore itself fails: never claim success, never lose the backup ----
  $case = 'upgrade, the restore after a failed health gate is locked out'
  $d = New-Fixture
  $run = Invoke-Deploy $d 'Upgrade' 'health-fails-locked'
  Assert ($run.Code -notin @(0, 99)) "$case : exits non-zero"
  Assert ($run.Flat -notmatch 'rolledbacktothepre-deploystate') "$case : does not claim the rollback worked"
  Assert ((Get-ServiceState $d) -eq 'Stopped') "$case : a half-restored app is left stopped, not started"
  Assert ($run.Flat -match [regex]::Escape("backups\$(Get-NewestBackup $d)")) "$case : names the folder holding the previous files"
  Assert-WholeBackup $d $case

  # ---- review O3: a rollback uses its backup up, so the next goes one further back ----
  $case = 'rollback, twice in a row'
  $d = New-Fixture
  Add-Backup $d $script:Older '0.9.0'
  $want1 = (Get-Tree "$d\install") -replace '2\.0\.0', '1.0.0'
  $want2 = (Get-Tree "$d\install") -replace '2\.0\.0', '0.9.0'
  $run = Invoke-Deploy $d 'Rollback'
  Assert ($run.Code -eq 0) "$case : the first exits 0"
  Assert ((Get-Tree "$d\install") -ceq $want1) "$case : the first puts the newest backup in place, whole"
  Assert (-not (Test-Path "$d\install\backups\$script:Target")) "$case : the first leaves no emptied folder behind"
  $run = Invoke-Deploy $d 'Rollback'
  Assert ($run.Code -eq 0) "$case : the second exits 0"
  Assert ((Get-Tree "$d\install") -ceq $want2) "$case : the second puts the backup before it in place, whole"
  Assert (-not @(Get-NwdashBackups -InstallDir "$d\install").Count) "$case : nothing is left to roll back to"
  $run = Invoke-Deploy $d 'Rollback'
  Assert ($run.Flat -match 'nobackupunderbackups') "$case : a third says there is no backup"
  Assert ((Get-Tree "$d\install") -ceq $want2) "$case : and changes nothing"

  $case = 'rollback, an earlier rollback left its emptied backup newest'
  $d = New-Fixture
  Add-SpentBackup $d $script:Newer
  $want = (Get-Tree "$d\install") -replace '2\.0\.0', '1.0.0'
  $run = Invoke-Deploy $d 'Rollback'
  Assert ($run.Code -eq 0) "$case : exits 0"
  Assert ((Get-Tree "$d\install") -ceq $want) "$case : the real backup is in place, whole"

  $case = 'upgrade, an earlier rollback left its emptied backup'
  $d = New-Fixture
  Add-SpentBackup $d $script:Newer
  $run = Invoke-Deploy $d 'Upgrade'
  Assert ($run.Code -eq 0) "$case : exits 0"
  Assert (-not (Test-Path "$d\install\backups\$script:Newer")) "$case : the prune removes the emptied folder"
  Assert (Test-Path "$d\install\backups\$script:Target\app\networker_dashboard.py") "$case : and keeps the real backup"

  # ---- review O3: a backup without the app is refused before anything stops ----
  $case = 'rollback, the newest backup has no runtime'
  $d = New-Fixture
  Add-Backup $d $script:Newer '1.5.0'
  Remove-Item -Recurse -Force "$d\install\backups\$script:Newer\runtime"
  $old = Get-Tree "$d\install" -All
  $run = Invoke-Deploy $d 'Rollback'
  Assert ($run.Code -notin @(0, 99)) "$case : exits non-zero"
  Assert ($run.Flat -match [regex]::Escape("backups\$($script:Newer)isincomplete(noruntime\python\python.exe)")) "$case : says which backup lacks what"
  Assert ($run.Flat -match [regex]::Escape("rollbackto$($script:Target)instead")) "$case : names the backup it would reach with that one moved out"
  Assert (-not (Get-NssmLog $d | Where-Object { $_ -match '^(stop|start)' })) "$case : the service is not touched"
  Assert ((Get-Tree "$d\install" -All) -ceq $old) "$case : no file changed"
} finally {
  foreach ($d in $script:scratch) { Remove-Item -Recurse -Force $d -ErrorAction SilentlyContinue }
}

Write-Host 'ALL PASS'
