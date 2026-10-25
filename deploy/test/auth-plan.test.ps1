# Unit tests for Get-NwdashAuthPlan: what an install/upgrade does about the
# dashboard password. The app refuses to start on a non-loopback bind with no
# password, so the installer must refuse that plan BEFORE it stops or copies
# anything (otherwise the health gate fails in an NSSM restart loop). Run:
#   powershell -NoProfile -ExecutionPolicy Bypass -File deploy\test\auth-plan.test.ps1
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\..\lib\common.ps1"
function Assert($cond, $msg) { if (-not $cond) { throw "FAIL: $msg" } else { Write-Host "  PASS $msg" } }
function Plan([bool]$Given, [bool]$Stored, [string]$Bind, [bool]$Allow) {
  Get-NwdashAuthPlan -PasswordGiven $Given -PasswordStored $Stored -BindHost $Bind -AllowUnauthenticated $Allow
}

# ---- a password, typed now or stored in data\auth.json, always wins ----
Assert ((Plan $true  $false '' $false) -eq 'password') 'password given: set it on first boot'
Assert ((Plan $true  $true  '' $true)  -eq 'password') 'password given beats every other input'
Assert ((Plan $false $true  '' $false) -eq 'stored')   'stored hash: reinstall/upgrade keeps it'

# ---- no password: only loopback or an explicit opt-in may proceed ----
Assert ((Plan $false $false '' $false)          -eq 'refused')  'all interfaces, no password: refused'
Assert ((Plan $false $false '0.0.0.0' $false)   -eq 'refused')  '0.0.0.0, no password: refused'
Assert ((Plan $false $false '192.0.2.10' $false) -eq 'refused') 'pinned network IP, no password: refused'
foreach ($lo in @('127.0.0.1', 'localhost', '::1')) {
  Assert ((Plan $false $false $lo $false) -eq 'loopback') "loopback bind $lo is allowed without a password"
}
Assert ((Plan $false $false '' $true) -eq 'unauthenticated') '-AllowUnauthenticated opts in explicitly'

# ---- the env var the installer sets is the one the app reads ----
Assert ($script:NwdashAllowUnauthEnv -eq 'DASHBOARD_ALLOW_UNAUTHENTICATED=1') 'env var name/value pinned'

Write-Host 'auth-plan: all tests passed'
