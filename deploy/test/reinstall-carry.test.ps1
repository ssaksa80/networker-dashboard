# Regression test for review O2: a plain install over the installed service
# keeps whatever its command line leaves out (port, bind, environment, account)
# instead of rebuilding the service from that command line alone.
#
# Get-NwdashCarryOver and Read-NwdashLiveService are tested directly. The rest
# runs the REAL install.ps1 in a child process: its top (from the launcher
# helper to the elevation step) and its fresh-install block, against a temp
# folder, as an elevated run would. Only the service side is faked: Get-Service,
# the account query, nssm, the folder lock, the firewall, the start and the
# health probe. Anything else that would reach this machine throws. Run:
#   powershell -NoProfile -ExecutionPolicy Bypass -File deploy\test\reinstall-carry.test.ps1
param(
  [string]$RunIn = '',
  [string]$LiveShape = 'network',   # the installed service the fakes describe; 'none' = no service
  [switch]$AsAdmin,
  # install.ps1's own parameters, so a case passes exactly what an operator types
  [int]$Port = 8443,
  [string]$BindHost = '',
  [string]$ServiceAccount,
  [string]$ServicePassword,
  [string]$AuthPassword = '',
  [string]$AllowedHosts = '',
  [switch]$AllowUnauthenticated,
  [switch]$Check
)
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\..\lib\common.ps1"

# nssm writes UTF-16LE; PowerShell reads it a byte at a time, so every
# character arrives followed by a NUL (see nssm-env-decode.test.ps1).
function Mangle([string]$s) { ($s.ToCharArray() | ForEach-Object { "$_`0" }) -join '' }
function Get-NssmLines([string[]]$Lines) { @((Mangle ($Lines -join "`r`n")) -split "`n") }

$script:Me = "$env:USERDOMAIN\$env:USERNAME"   # an account that resolves and needs a password
$script:Shapes = @{
  network = @{
    Parameters = 'networker_dashboard.py --port 9443 --bind 192.0.2.10 --no-launch'
    Env        = @('NWDASH_EXTRA=kept', 'DASHBOARD_ALLOWED_HOSTS=198.51.100.0/24', 'DASHBOARD_ALLOW_UNAUTHENTICATED=1', 'DASHBOARD_AUTH_PASSWORD=left-behind')
    Account    = 'NT AUTHORITY\NetworkService'
  }
  named = @{
    Parameters = 'networker_dashboard.py --port 9443 --no-launch'
    Env        = @('DASHBOARD_ALLOWED_HOSTS=198.51.100.0/24')
    Account    = $script:Me
  }
}

# ---- child: the real installer, with the service side faked ----
if ($RunIn) {
  $InstallDir = "$RunIn\install"
  $Bundle = "$RunIn\bundle"
  $AppSrc = "$Bundle\app"
  $PySrc = "$Bundle\runtime\python"
  $NssmSrc = "$Bundle\runtime\nssm.exe"
  $SvcName = $script:NwdashServiceName
  $BundleVersion = '9.9.9'
  foreach ($s in 'Upgrade', 'Rollback', 'Uninstall', 'Purge', 'NoFirewall', 'Elevated') { Set-Variable -Name $s -Value ([switch]$false) }
  $script:asAdmin = $AsAdmin.IsPresent
  $script:given = @($PSBoundParameters.Keys | Where-Object { $_ -notin @('RunIn', 'LiveShape', 'AsAdmin') })
  $shape = $script:Shapes[$LiveShape]
  function Write-Call([string]$Line) { Add-Content -LiteralPath "$RunIn\calls.log" -Value $Line }

  function Get-Service {
    [CmdletBinding()] param([Parameter(Position = 0)][string]$Name)
    if ($LiveShape -ne 'none') { [pscustomobject]@{ Name = $Name; Status = 'Running' } }
  }
  # Win32_Service answers the account query; Win32_Process finds nothing, so no
  # process is ever stopped.
  function Get-CimInstance {
    [CmdletBinding()] param([Parameter(Position = 0)][string]$ClassName, [string]$Filter)
    if ($ClassName -eq 'Win32_Service' -and $shape) { [pscustomobject]@{ StartName = $shape.Account } }
  }
  function Get-ScheduledTask { [CmdletBinding()] param([string]$TaskName) }
  function Invoke-FakeNssm {
    $flat = foreach ($a in $args) { if ($a -is [array]) { $a -join ';' } else { "$a" } }
    Write-Call ("nssm " + ($flat -join ' | '))
    if ($args[0] -ne 'get') { return }
    if (-not $shape) { throw "Can't open service!" }   # what nssm says when it may not read it
    switch ($args[2]) {
      'AppParameters' { Get-NssmLines @($shape.Parameters) }
      'AppEnvironmentExtra' { Get-NssmLines $shape.Env }
    }
  }
  function Remove-NwdashService { param([string]$InstallDir) Write-Call 'REMOVE-SERVICE' }
  function Set-NwdashInstallAcl { param([string]$InstallDir, [string]$ServiceSid) Write-Call "LOCK $ServiceSid" }
  function Set-NwdashFirewall { param([int]$Port) Write-Call "FIREWALL $Port" }
  function Test-NwdashPortInUse { $null }
  function Start-NwdashServiceVerified { Write-Call 'START'; $true }
  function Test-NwdashHealth { $true }
  function Start-Sleep { }
  # Nothing here may reach the machine. A path that does fails the case.
  foreach ($f in 'Stop-Process', 'Start-Process', 'New-NetFirewallRule', 'Remove-NetFirewallRule', 'Stop-ScheduledTask', 'Unregister-ScheduledTask', 'Start-Service', 'Stop-Service') {
    Set-Item -Path "function:$f" -Value ([scriptblock]::Create("throw 'test reached $f'"))
  }

  $src = Get-Content -Raw "$PSScriptRoot\..\install.ps1"
  function Get-Block([string]$From, [string]$To) {
    $a = $src.IndexOf($From); $b = if ($To) { $src.IndexOf($To) } else { $src.Length }
    if ($a -lt 0 -or $b -lt $a) { throw "markers not found in install.ps1: $From" }
    return $src.Substring($a, $b - $a)
  }
  function Set-Line([string]$Block, [string]$Real, [string]$Fake) {
    if (-not $Block.Contains($Real)) { throw "not found in install.ps1: $Real" }
    return $Block.Replace($Real, $Fake)
  }
  $top = Get-Block 'function Get-NwdashLauncherArgs' '# ---- elevation'
  $top = Set-Line $top '$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)' '$isAdmin = $script:asAdmin'
  $top = Set-Line $top '$givenParams = @($PSBoundParameters.Keys)' '$givenParams = $script:given'
  $top = Set-Line $top "`$readNssm = if ((Get-NwdashPathState -Path `$NssmSrc) -eq 'present') { `$NssmSrc } else { Join-Path `$InstallDir 'nssm.exe' }" '$readNssm = ${function:Invoke-FakeNssm}'
  $fresh = Set-Line (Get-Block '# ---- fresh install' '') '$nssm = "$InstallDir\nssm.exe"' '$nssm = ${function:Invoke-FakeNssm}'

  . ([scriptblock]::Create($top))
  # A real run asks for elevation here; a non-elevated one stops at it.
  if (-not $script:asAdmin) { Write-Host 'REACHED-ELEVATION'; exit 42 }
  . ([scriptblock]::Create($fresh))
  exit 99   # the block always ends in exit or a terminating error
}

# ---- parent ----
function Assert($cond, $msg) { if (-not $cond) { throw "FAIL: $msg" } else { Write-Host "  PASS $msg" } }
function Live([string]$Params, [string[]]$Env = @(), [string]$Account = 'LocalSystem') {
  [pscustomobject]@{ Parameters = $Params; Env = $Env; Account = $Account }
}
$p = 'networker_dashboard.py --port 9443 --bind 192.0.2.10 --no-launch'

# ---- Get-NwdashCarryOver: what is kept ----
$c = Get-NwdashCarryOver -Live (Live $p $script:Shapes.network.Env 'NT AUTHORITY\NetworkService')
Assert ($c.Port -eq 9443 -and $c.BindHost -eq '192.0.2.10') 'nothing given: the live port and bind are kept'
Assert ($c.AllowedHosts -eq '198.51.100.0/24') 'nothing given: the allow-list is kept'
Assert ($c.AllowUnauthenticated) 'nothing given: -AllowUnauthenticated is kept'
Assert ((@($c.Env) -join ';') -eq 'NWDASH_EXTRA=kept') 'other variables are kept as they are; the managed three are not in Env'
Assert (-not (@($c.Env) -match 'AUTH_PASSWORD')) 'a password left in the env is dropped: it would reset the password at every start'
Assert ($c.ServiceAccount -eq 'NT AUTHORITY\NetworkService' -and -not $c.Problem) 'a built-in account needs no password and is kept'
Assert (($c.Kept -join ', ') -eq 'port 9443, bind 192.0.2.10, allowed hosts 198.51.100.0/24, -AllowUnauthenticated, 1 other environment variable(s), account NT AUTHORITY\NetworkService') 'Kept names everything carried, for the log'

$c = Get-NwdashCarryOver -Live (Live $p @('DASHBOARD_ALLOWED_HOSTS=198.51.100.0/24')) -Given @('Port', 'BindHost', 'AllowedHosts') -Port 9555 -BindHost '0.0.0.0' -AllowedHosts '203.0.113.0/24'
Assert ($c.Port -eq 9555 -and $c.BindHost -eq '0.0.0.0' -and $c.AllowedHosts -eq '203.0.113.0/24') 'what the command line gives wins'
Assert (-not ($c.Kept -match 'port|bind|allowed')) 'and is not reported as kept'

$c = Get-NwdashCarryOver -Live (Live 'networker_dashboard.py --no-launch' @('DASHBOARD_ALLOW_UNAUTHENTICATED=0'))
Assert ($c.Port -eq 8443 -and $c.BindHost -eq '') 'a command line without --port/--bind keeps the defaults'
Assert (-not $c.AllowUnauthenticated -and -not @($c.Env).Count) 'DASHBOARD_ALLOW_UNAUTHENTICATED=0 is not an opt-in (the app honours exactly 1)'
Assert ($c.ServiceAccount -eq '' -and ($c.Kept -contains 'account LocalSystem')) 'LocalSystem is kept as nssm''s default: nothing to set'

# ---- Get-NwdashCarryOver: an account Windows will not give the password of ----
$c = Get-NwdashCarryOver -Live (Live $p @() 'EXAMPLE\svc-nwdash')
Assert ($c.Problem -match '-ServicePassword' -and $c.Problem -match '-Upgrade' -and $c.Problem -match 'Nothing was changed') 'a named account without -ServicePassword: refused, saying what to pass'
$c = Get-NwdashCarryOver -Live (Live $p @() 'EXAMPLE\svc-nwdash') -Given @('ServicePassword')
Assert ($c.ServiceAccount -eq 'EXAMPLE\svc-nwdash' -and -not $c.Problem) 'with -ServicePassword the account is kept'
$c = Get-NwdashCarryOver -Live (Live $p @() 'EXAMPLE\svc-nwdash') -Given @('ServiceAccount') -ServiceAccount 'EXAMPLE\other'
Assert ($c.ServiceAccount -eq 'EXAMPLE\other' -and -not $c.Problem) 'an account given on the command line is used as before'
$c = Get-NwdashCarryOver -Live (Live $p @() '')
Assert ($c.Problem -match 'could not read the account') 'an account that cannot be read is never guessed as LocalSystem'

# ---- Read-NwdashLiveService ----
$fakeGet = { param($setting) if ($setting -eq 'AppParameters') { Get-NssmLines @($p) } else { Get-NssmLines @('A=1', 'B=2') } }
$fakeSvc = { param($n) [pscustomobject]@{ StartName = 'LocalSystem' } }
$r = Read-NwdashLiveService -NssmGet $fakeGet -GetService $fakeSvc
Assert ($r.Parameters -eq $p) 'reads the command line through the UTF-16 decode'
Assert ((@($r.Env) -join ';') -eq 'A=1;B=2') 'reads the environment one variable per line'
Assert ($r.Account -eq 'LocalSystem') 'reads the account the service logs on as'
Assert ($null -eq (Read-NwdashLiveService -NssmGet { param($s) throw "Can't open service!" } -GetService $fakeSvc)) 'nssm refusing: $null, nothing guessed'
Assert ($null -eq (Read-NwdashLiveService -NssmGet { param($s) Get-NssmLines @('C:\other\app.exe --serve') } -GetService $fakeSvc)) 'a service that is not this app: $null'
$r = Read-NwdashLiveService -NssmGet { param($s) if ($s -eq 'AppParameters') { Get-NssmLines @($p) } } -GetService $fakeSvc
Assert (@($r.Env).Count -eq 0) 'no AppEnvironmentExtra: an empty environment'

# ---- the real installer ----
$script:scratch = @()
function New-Fixture([switch]$StoredPassword) {
  $d = Join-Path $env:TEMP ("nwdash-reinstall-" + [guid]::NewGuid().ToString('N'))
  $script:scratch += $d
  New-Item -ItemType Directory -Force "$d\bundle\app\nwdash", "$d\bundle\runtime\python", "$d\install\data" | Out-Null
  Set-Content "$d\bundle\app\networker_dashboard.py" 'launcher'
  Set-Content "$d\bundle\app\nwdash\config.py" 'APP_VERSION = "9.9.9"'
  Set-Content "$d\bundle\runtime\python\python.exe" 'python'
  Set-Content "$d\bundle\runtime\nssm.exe" 'nssm'
  if ($StoredPassword) { Set-Content "$d\install\data\auth.json" '{}' }
  return $d
}
function Invoke-Install([string]$Dir, [string[]]$Arguments) {
  $ErrorActionPreference = 'Continue'   # under 5.1 a child's stderr would throw here
  $out = & powershell -NoProfile -ExecutionPolicy Bypass -File $PSCommandPath -RunIn $Dir @Arguments 2>&1 | Out-String
  $code = $LASTEXITCODE
  $calls = if (Test-Path "$Dir\calls.log") { @(Get-Content "$Dir\calls.log") } else { @() }
  # Flat: the console wraps long error lines, so match with the whitespace gone.
  return [pscustomobject]@{ Code = $code; Out = $out; Flat = ($out -replace '\s+', ''); Calls = $calls }
}
function Get-Set($Run, [string]$Setting) { @($Run.Calls | Where-Object { $_ -like "nssm set | NetWorkerDashboard | $Setting | *" }) }
function Assert-Untouched($Run, [string]$Case) {
  Assert (-not ($Run.Calls | Where-Object { $_ -notlike 'nssm get *' })) "$Case : nothing but reads - no service removed, nothing set"
}

try {
  $case = '-Check over a service'
  $d = New-Fixture
  $run = Invoke-Install $d @('-LiveShape', 'network', '-Check')
  Assert ($run.Code -eq 0) "$case : exits 0"
  Assert ($run.Out -match 'reinstall over the existing service \(keeps its settings unless given here\)') "$case : names the mode"
  Assert ($run.Out -match 'port / bind\s+: 9443 / 192\.0\.2\.10') "$case : shows the port and bind it will keep"
  Assert ($run.Out -match 'allowed hosts\s+: 198\.51\.100\.0/24') "$case : shows the allow-list it will keep"
  Assert ($run.Out -match 'account: NT AUTHORITY\\NetworkService') "$case : shows the account it will keep"
  Assert ($run.Flat -match 'keptfromit:port9443,bind192\.0\.2\.10,allowedhosts198\.51\.100\.0/24,-AllowUnauthenticated,1otherenvironmentvariable\(s\),accountNTAUTHORITY\\NetworkService') "$case : lists everything kept"
  Assert-Untouched $run $case

  $case = 'plain reinstall'
  $d = New-Fixture
  $run = Invoke-Install $d @('-LiveShape', 'network', '-AsAdmin')
  Assert ($run.Code -eq 0) "$case : exits 0"
  Assert ((Get-Set $run 'AppParameters') -eq 'nssm set | NetWorkerDashboard | AppParameters | networker_dashboard.py --port 9443 --bind 192.0.2.10 --no-launch') "$case : the service keeps its port and bind"
  Assert ((Get-Set $run 'ObjectName') -eq 'nssm set | NetWorkerDashboard | ObjectName | NT AUTHORITY\NetworkService | ') "$case : and its account"
  Assert ((Get-Set $run 'AppEnvironmentExtra') -eq 'nssm set | NetWorkerDashboard | AppEnvironmentExtra | NWDASH_EXTRA=kept;DASHBOARD_ALLOWED_HOSTS=198.51.100.0/24;DASHBOARD_ALLOW_UNAUTHENTICATED=1') "$case : and its environment, without the leftover password"
  Assert ($run.Calls -contains 'FIREWALL 9443') "$case : the firewall opens the kept port"
  Assert ($run.Calls -contains 'LOCK S-1-5-20') "$case : the folder lock is for the kept account"
  Assert ([array]::IndexOf($run.Calls, 'REMOVE-SERVICE') -gt [array]::IndexOf($run.Calls, 'nssm get | NetWorkerDashboard | AppEnvironmentExtra')) "$case : the settings are read before the old service is removed"
  Assert ($run.Out -match 'keeping the existing service settings: port 9443') "$case : says what it kept"

  $case = 'reinstall that changes some settings'
  $d = New-Fixture
  $run = Invoke-Install $d @('-LiveShape', 'network', '-AsAdmin', '-Port', '9555', '-AllowedHosts', '203.0.113.0/24', '-AuthPassword', 'test-pass-123')
  Assert ($run.Code -eq 0) "$case : exits 0"
  Assert ((Get-Set $run 'AppParameters') -eq 'nssm set | NetWorkerDashboard | AppParameters | networker_dashboard.py --port 9555 --bind 192.0.2.10 --no-launch') "$case : the given port wins, the bind is kept"
  $sets = @(Get-Set $run 'AppEnvironmentExtra')
  Assert ($sets.Count -eq 2) "$case : the environment is set for the first boot, then again without the password"
  Assert ($sets[0] -eq 'nssm set | NetWorkerDashboard | AppEnvironmentExtra | NWDASH_EXTRA=kept;DASHBOARD_ALLOWED_HOSTS=203.0.113.0/24;DASHBOARD_AUTH_PASSWORD=test-pass-123;DASHBOARD_ALLOW_UNAUTHENTICATED=1') "$case : first boot: the given allow-list and password, the rest kept"
  Assert ($sets[1] -eq 'nssm set | NetWorkerDashboard | AppEnvironmentExtra | NWDASH_EXTRA=kept;DASHBOARD_ALLOWED_HOSTS=203.0.113.0/24;DASHBOARD_ALLOW_UNAUTHENTICATED=1') "$case : after it, the password goes and everything else stays"

  $case = 'a named account and no -ServicePassword'
  $d = New-Fixture -StoredPassword
  $run = Invoke-Install $d @('-LiveShape', 'named', '-AsAdmin')
  Assert ($run.Code -notin @(0, 42, 99)) "$case : exits non-zero"
  Assert ($run.Flat -match ([regex]::Escape(($script:Me -replace '\s', '')) + ',andWindowsdoesnotgive')) "$case : names the account and why"
  Assert ($run.Flat -match '-ServicePassword' -and $run.Flat -match '-Upgrade') "$case : says what to do"
  Assert-Untouched $run $case

  $case = 'a named account kept with -ServicePassword'
  $d = New-Fixture -StoredPassword
  $run = Invoke-Install $d @('-LiveShape', 'named', '-AsAdmin', '-ServicePassword', 'test-secret')
  Assert ($run.Code -eq 0) "$case : exits 0"
  Assert ((Get-Set $run 'ObjectName') -eq "nssm set | NetWorkerDashboard | ObjectName | $script:Me | test-secret") "$case : the service logs on as the same account"
  Assert ((Get-Set $run 'AppParameters') -like '*--port 9443 --no-launch') "$case : and keeps its port"

  $case = 'settings that cannot be read, elevated'
  $d = New-Fixture -StoredPassword
  $run = Invoke-Install $d @('-LiveShape', 'unreadable', '-AsAdmin')
  Assert ($run.Code -notin @(0, 42, 99)) "$case : exits non-zero"
  Assert ($run.Flat -match 'itssettingscouldnotberead' -and $run.Flat -match 'Nothingwaschanged') "$case : says why, and that nothing changed"
  Assert-Untouched $run $case

  $case = 'settings that cannot be read, before elevation'
  $d = New-Fixture
  $run = Invoke-Install $d @('-LiveShape', 'unreadable')
  Assert ($run.Code -eq 42) "$case : nothing is refused on a guess; the elevated run decides"
  $run = Invoke-Install $d @('-LiveShape', 'unreadable', '-Check')
  Assert ($run.Code -eq 0 -and $run.Out -match 'kept from it\s+: not known') "$case : -Check says it cannot tell"

  $case = 'no service installed'
  $d = New-Fixture
  $run = Invoke-Install $d @('-LiveShape', 'none', '-AsAdmin', '-AuthPassword', 'test-pass-123')
  Assert ($run.Code -eq 0) "$case : exits 0"
  Assert (-not ($run.Calls | Where-Object { $_ -like 'nssm get *' })) "$case : nothing to read"
  Assert ((Get-Set $run 'AppParameters') -eq 'nssm set | NetWorkerDashboard | AppParameters | networker_dashboard.py --port 8443 --no-launch') "$case : a fresh install uses the defaults, as before"
  Assert (-not (Get-Set $run 'ObjectName')) "$case : and LocalSystem"
} finally {
  foreach ($d in $script:scratch) { Remove-Item -LiteralPath $d -Recurse -Force -ErrorAction SilentlyContinue }
}

Write-Host 'reinstall-carry: all tests passed'
