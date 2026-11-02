# Tests for the folder lock (review S5): what each part of an install gets, and
# that locking an existing install changes permissions and nothing else. The
# integration half runs on a throwaway install in %TEMP%, never on a real one;
# /setowner needs elevation, so it only runs for real on an elevated run (CI).
#   powershell -NoProfile -ExecutionPolicy Bypass -File deploy\test\folder-acl.test.ps1
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\..\lib\common.ps1"
function Assert($cond, $msg) { if (-not $cond) { throw "FAIL: $msg" } else { Write-Host "  PASS $msg" } }

$System = 'S-1-5-18'; $Admins = 'S-1-5-32-544'; $Users = 'S-1-5-32-545'
$NetworkService = 'S-1-5-20'; $Everyone = 'S-1-1-0'; $AuthUsers = 'S-1-5-11'
$me = [Security.Principal.WindowsIdentity]::GetCurrent()
$mySid = $me.User.Value
$elevated = ([Security.Principal.WindowsPrincipal]$me).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)

# "<sid> <rights>" for every entry on an item, sorted, without the Synchronize
# bit Windows adds to every file right.
function Get-Grants($Path) {
  $rules = (Get-Acl -LiteralPath $Path).Access
  return @($rules | ForEach-Object {
    "$($_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value) $(("$($_.FileSystemRights)") -replace ', Synchronize', '')"
  } | Sort-Object -Unique)
}
function Expect-Grants($Path, [string[]]$Expected, $Msg) {
  $got = Get-Grants $Path
  $want = @($Expected | Sort-Object)
  Assert (($got -join ';') -eq ($want -join ';')) "$Msg (got: $($got -join '; '))"
}
function Grant-Text($Tier, $Sid) { @(Get-NwdashAclGrants -Tier $Tier -ServiceSid $Sid | ForEach-Object { "$($_.Sid)=$($_.Rights)" }) -join ' ' }

# ---- what each part of an install gets ----
$sysFull = "$System=FullControl $Admins=FullControl"
Assert ((Grant-Text 'root' $System) -eq "$sysFull $Users=ReadAndExecute") 'LocalSystem root: Users read, no extra entry'
Assert ((Grant-Text 'private' $System) -eq $sysFull) 'LocalSystem private: SYSTEM and Administrators only'
Assert ((Grant-Text 'backups' $System) -eq $sysFull) 'LocalSystem backups: SYSTEM and Administrators only'
Assert ((Grant-Text 'root' $NetworkService) -eq "$sysFull $Users=ReadAndExecute $NetworkService=ReadAndExecute") 'named account reads the app, never writes it'
Assert ((Grant-Text 'private' $NetworkService) -eq "$sysFull $NetworkService=Modify") 'named account can change its own data'
Assert ((Grant-Text 'backups' $NetworkService) -eq $sysFull) 'named account gets nothing in backups\'

# ---- account names, in every form a service reports them ----
Assert ((Resolve-NwdashAccountSid -Account '') -eq $System) 'no -ServiceAccount is LocalSystem'
foreach ($name in @('LocalSystem', '.\LocalSystem', 'localsystem', 'NT AUTHORITY\SYSTEM')) {
  Assert ((Resolve-NwdashAccountSid -Account $name) -eq $System) "$name resolves to SYSTEM"
}
Assert ((Resolve-NwdashAccountSid -Account 'NT AUTHORITY\NetworkService') -eq $NetworkService) 'the service manager spelling of NETWORK SERVICE resolves'
Assert ((Resolve-NwdashAccountSid -Account $mySid) -eq $mySid) 'a SID passes through'
Assert ((Resolve-NwdashAccountSid -Account ".\$env:USERNAME") -eq $mySid) '.\name means this machine'
$threw = $false
try { [void](Resolve-NwdashAccountSid -Account 'no-such-domain\no-such-user') } catch { $threw = "$_" -match 'no-such-user' }
Assert $threw 'an unknown account stops the install, naming the account'

# ---- reading the live account: unreadable is not LocalSystem ----
Assert ((Read-NwdashServiceAccount -GetService { [pscustomobject]@{ StartName = ' CORP\svc-nwdash ' } }) -eq 'CORP\svc-nwdash') 'live account read and trimmed'
Assert ((Read-NwdashServiceAccount -GetService { $null }) -eq '') 'no service: empty, not LocalSystem'
Assert ((Read-NwdashServiceAccount -GetService { throw 'WMI is broken' }) -eq '') 'query failure: empty, not LocalSystem'
Assert ((Read-NwdashServiceAccount -Name 'Schedule') -eq 'LocalSystem') 'real query works unelevated'

# ---- folders that must never get this lock ----
$sysDirs = @('C:\Windows', 'C:\Program Files', 'C:\Users\someone')
foreach ($bad in @('C:\', 'D:\', 'C:\Windows', 'C:\Windows\', 'c:\program files', 'C:\Users')) {
  Assert ([bool](Get-NwdashAclTargetProblem -InstallDir $bad -SystemFolders $sysDirs)) "$bad is refused"
}
foreach ($good in @('C:\apps\networker-dashboard', 'D:\apps\nwdash\', 'C:\Program Files\NetWorker Dashboard', 'C:\Users\someone\nwdash')) {
  Assert ((Get-NwdashAclTargetProblem -InstallDir $good -SystemFolders $sysDirs) -eq '') "$good may be locked"
}

# ---- a throwaway install, as an older build left it: open, keys in place ----
function New-FakeInstall {
  $dir = Join-Path ([IO.Path]::GetTempPath()) ("nwdash-acl-" + [guid]::NewGuid().ToString('N'))
  foreach ($d in @('nwdash', 'runtime\python', 'data', '.certs', 'logs', 'backups\3.0.0-20260101-000000\app', 'debug_test\data')) {
    New-Item -ItemType Directory -Force (Join-Path $dir $d) | Out-Null
  }
  $files = @{
    'networker_dashboard.py' = 'launcher'; 'nssm.exe' = 'nssm'; 'nwdash\config.py' = 'config'; 'runtime\python\python.exe' = 'python'
    'data\.session_key' = 'DPAPI1 session'; 'data\.auth_key' = 'DPAPI1 auth'; 'data\auth.json' = '{}'
    '.certs\networker_dashboard.key' = 'tls key'; 'logs\service.out.log' = 'log'
    'backups\3.0.0-20260101-000000\app\old.py' = 'old'; 'debug_test\data\.session_key' = 'stale key'
    'password-notes.txt' = 'stray file in the install root'
  }
  foreach ($rel in $files.Keys) { [IO.File]::WriteAllText((Join-Path $dir $rel), $files[$rel]) }
  return $dir
}
function Remove-FakeInstall($Dir) {
  if (-not (Test-Path -LiteralPath $Dir)) { return }
  & icacls.exe $Dir /grant "*${mySid}:(OI)(CI)F" /T /Q 2>&1 | Out-Null
  Remove-Item -LiteralPath $Dir -Recurse -Force
}
function Get-TreeHash($Dir, [string[]]$Rel) {
  @($Rel | ForEach-Object { (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $Dir $_)).Hash }) -join ','
}
# The real thing except /setowner, which only an elevated run may do. Calls are
# recorded so the order can be checked.
$script:calls = New-Object System.Collections.ArrayList
$runner = {
  param($a)
  [void]$script:calls.Add(($a -join ' '))
  # Without elevation this account cannot list backups\ once it is locked.
  $noWayIn = (-not $elevated) -and "$($a[0])".EndsWith('\backups\*')
  if ($a -notcontains '/setowner' -and -not $noWayIn) { Invoke-NwdashIcacls -Arguments $a }
}

$open = New-FakeInstall
try {
  $keys = @('data\.session_key', 'data\.auth_key', 'data\auth.json', '.certs\networker_dashboard.key')
  $before = Get-TreeHash $open $keys
  # A previous service account on data\, and a hand-made entry on a key file.
  Set-NwdashItemAcl -Item (Get-Item -LiteralPath (Join-Path $open 'data')) -Grants @(
    @{ Sid = $System; Rights = 'FullControl' }, @{ Sid = $Admins; Rights = 'FullControl' },
    @{ Sid = $mySid; Rights = 'FullControl' }, @{ Sid = $NetworkService; Rights = 'Modify' })
  & icacls.exe (Join-Path $open 'data\.session_key') /grant "*${Everyone}:(R)" /Q | Out-Null
  & icacls.exe (Join-Path $open 'nwdash\config.py') /grant "*${AuthUsers}:(M)" /Q | Out-Null

  # This account plays the service, so the checks below can still read the tree.
  Set-NwdashInstallAcl -InstallDir $open -ServiceSid $mySid -Icacls $runner

  $private = @("$System FullControl", "$Admins FullControl", "$mySid Modify")
  foreach ($part in @('data', '.certs', 'logs', 'debug_test')) {
    Assert (Get-Acl -LiteralPath (Join-Path $open $part)).AreAccessRulesProtected "$part\ inherits nothing"
    Expect-Grants (Join-Path $open $part) $private "$part\ is SYSTEM, Administrators and the service only"
  }
  Expect-Grants (Join-Path $open 'data\.session_key') $private 'a hand-made entry on a key file is gone'
  Expect-Grants (Join-Path $open 'data\.auth_key') $private 'the other key file follows data\'
  Expect-Grants (Join-Path $open '.certs\networker_dashboard.key') $private 'the TLS private key follows .certs\'
  Expect-Grants (Join-Path $open 'debug_test\data\.session_key') $private 'an unknown tree is locked like data\'
  Expect-Grants (Join-Path $open 'password-notes.txt') $private 'an unknown file is locked like data\'
  Assert (Get-Acl -LiteralPath (Join-Path $open 'password-notes.txt')).AreAccessRulesProtected 'an unknown file inherits nothing'
  Expect-Grants (Join-Path $open 'backups') @("$System FullControl", "$Admins FullControl") 'backups\ is SYSTEM and Administrators only'

  $rootAcl = @("$System FullControl", "$Admins FullControl", "$Users ReadAndExecute", "$mySid ReadAndExecute")
  Assert (Get-Acl -LiteralPath $open).AreAccessRulesProtected 'the install root inherits nothing from C:\'
  Expect-Grants $open $rootAcl 'the root: users read, the service reads'
  Expect-Grants (Join-Path $open 'nwdash\config.py') $rootAcl 'code: Authenticated Users can no longer change it'
  Expect-Grants (Join-Path $open 'runtime\python\python.exe') $rootAcl 'the runtime follows the root'
  Assert ((Get-TreeHash $open $keys) -eq $before) 'keys and the TLS key are byte-identical afterwards'

  # The service can still do its job, and what it writes is locked too.
  $newFile = Join-Path $open 'data\written-by-service.json'
  [IO.File]::WriteAllText($newFile, '{}')
  Expect-Grants $newFile $private 'a new file in data\ gets the lock'
  [IO.File]::Delete($newFile)
  [IO.File]::WriteAllText((Join-Path $open 'logs\service.out.log'), 'rotated')
  Assert ([IO.File]::ReadAllText((Join-Path $open 'data\.session_key')) -eq 'DPAPI1 session') 'the service can still read its key'

  # The order that keeps a part-way failure safe.
  $text = @($script:calls)
  $firstRootOwner = [array]::IndexOf($text, "$open /setowner *$Admins /Q")
  $dataReset = [array]::IndexOf($text, "$(Join-Path $open 'data')\* /reset /T /Q")
  Assert ($dataReset -ge 0 -and $firstRootOwner -gt $dataReset) 'private parts are locked before the root'
  Assert ($text -contains "$(Join-Path $open 'data') /setowner *$Admins /T /Q") 'data\ changes owner: an owner can rewrite the list'
  Assert ($text -contains "$(Join-Path $open 'nwdash') /reset /T /Q") 'code is reset to the root'
  Assert ($text -contains "$(Join-Path $open 'nssm.exe') /reset /Q") '/T is for folders: on a file it searches subfolders for the name'
  Assert ($text -contains "$(Join-Path $open 'password-notes.txt') /setowner *$Admins /Q") 'and an unknown file gets no /T either'
  Assert (-not ($text | Where-Object { $_ -match ' /C( |$)' })) 'icacls /C never used: it exits 0 after failures'

  # A second run (every upgrade) changes nothing.
  Set-NwdashInstallAcl -InstallDir $open -ServiceSid $mySid -Icacls $runner
  Expect-Grants (Join-Path $open 'data') $private 'idempotent: data\'
  Expect-Grants $open $rootAcl 'idempotent: root'
  Assert ((Get-TreeHash $open $keys) -eq $before) 'idempotent: keys'

  # Rollback brings back files that keep a backup's old permissions.
  $restored = Join-Path $open 'nwdash\config.py'
  if (-not $elevated) {  # an elevated run is an administrator, who may
    Assert ($(try { [IO.File]::AppendAllText($restored, 'x'); $false } catch { $true })) 'the service cannot change the app'
  }
  & icacls.exe $restored /grant "*${AuthUsers}:(M)" /Q | Out-Null
  Assert ((Get-Grants $restored) -contains "$AuthUsers Modify") 'a restored file arrives with its old open entry'
  Reset-NwdashAppAcl -InstallDir $open -Icacls $runner
  Expect-Grants $restored $rootAcl 'a restored file loses the old open entry'
} finally { Remove-FakeInstall $open }

# ---- a failure is loud, and leaves nothing more open than it was ----
$partial = New-FakeInstall
try {
  $rootBefore = (Get-Acl -LiteralPath $partial).Sddl
  $threw = $false
  try { Set-NwdashInstallAcl -InstallDir $partial -ServiceSid $mySid -Icacls { param($a) if ($a -contains '/reset') { throw 'icacls said no' } } }
  catch { $threw = "$_" -match 'icacls said no' }
  Assert $threw 'an icacls failure stops the lock'
  Assert ((Get-Acl -LiteralPath $partial).Sddl -eq $rootBefore) 'a failed lock never reached the root'
} finally { Remove-FakeInstall $partial }

$threw = $false
try { Invoke-NwdashIcacls -Arguments @($env:TEMP, '/grant', '*S-1-5-99-99-99:(R)') } catch { $threw = "$_" -match 'exit code' }
Assert $threw 'a failing icacls throws with its exit code'

# ---- what a non-elevated run sees of a locked folder ----
$denied = Join-Path ([IO.Path]::GetTempPath()) ("nwdash-state-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force $denied | Out-Null
try {
  $file = Join-Path $denied 'auth.json'
  [IO.File]::WriteAllText($file, '{}')
  Assert ((Get-NwdashPathState -Path $file) -eq 'present') 'a readable file is present'
  Assert ((Get-NwdashPathState -Path (Join-Path $denied 'missing.json')) -eq 'absent') 'a missing file is absent'
  Assert ((Get-NwdashPathState -Path (Join-Path $denied 'no\such\dir.json')) -eq 'absent') 'a missing folder is absent'
  # A deny entry, because an elevated run (CI) is allowed by the lock itself.
  & icacls.exe $denied /deny "*${mySid}:(OI)(CI)(R)" /Q | Out-Null
  Assert ((Get-NwdashPathState -Path $file) -eq 'denied') 'a locked file is denied, not absent, and does not throw'
} finally {
  & icacls.exe $denied /remove:d "*$mySid" /T /Q 2>&1 | Out-Null
  Remove-Item -LiteralPath $denied -Recurse -Force
}

# ---- the real /setowner, where this run may use it ----
if ($elevated) {
  $real = New-FakeInstall
  try {
    Set-NwdashInstallAcl -InstallDir $real -ServiceSid $NetworkService
    foreach ($rel in @('.', 'data', 'data\.session_key', 'nwdash\config.py', 'backups')) {
      $owner = (Get-Acl -LiteralPath (Join-Path $real $rel)).GetOwner([Security.Principal.SecurityIdentifier]).Value
      Assert ($owner -eq $Admins) "$rel is owned by Administrators"
    }
    Expect-Grants (Join-Path $real 'data') @("$System FullControl", "$Admins FullControl", "$NetworkService Modify") 'real run: data\'
  } finally { Remove-FakeInstall $real }
} else {
  Write-Host '  SKIP real /setowner: needs an elevated run'
}

Write-Host 'folder-acl: all tests passed'
