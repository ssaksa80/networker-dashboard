# Tests for how the installer handles the dashboard and service-account
# passwords (review S9): never on a command line it starts, never in the
# service's registry environment, and asked for after UAC instead of being
# forwarded across it. Read-Host is replaced, so nothing waits for a person.
#   powershell -NoProfile -ExecutionPolicy Bypass -File deploy\test\secret-handoff.test.ps1
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\..\lib\common.ps1"
function Assert($cond, $msg) { if (-not $cond) { throw "FAIL: $msg" } else { Write-Host "  PASS $msg" } }

# ---- the UAC relaunch carries the plan, never a password ----
$bound = [ordered]@{ InstallDir = 'D:\apps\nwdash'; Port = 9443; AuthPassword = 'dash-secret'; ServiceAccount = 'EXAMPLE\svc-nwdash'
                     ServicePassword = 'svc-secret'; NoFirewall = [switch]$true; Purge = [switch]$false }
$relaunch = Get-NwdashRelaunchArgs -Bound $bound -Given @('AuthPassword', 'ServicePassword')
$text = $relaunch -join ' '
Assert ($text -notmatch 'dash-secret|svc-secret') "no password on the relaunch command line ($text)"
Assert ($relaunch -contains '-PromptAuthPassword') 'the elevated window asks for the dashboard password'
Assert ($relaunch -contains '-PromptServicePassword') 'and for the service-account password'
Assert ($text -match '-InstallDir "D:\\apps\\nwdash"' -and $text -match '-Port "9443"' -and $text -match '-ServiceAccount "EXAMPLE\\svc-nwdash"') 'the rest of the plan is kept'
Assert ($relaunch -contains '-NoFirewall' -and $relaunch -notcontains '-Purge') 'switches as they were'
Assert ($relaunch[-1] -eq '-Elevated') 'marked as the relaunch'

$plain = Get-NwdashRelaunchArgs -Bound ([ordered]@{ Port = 8443 }) -Given @()
Assert (($plain -join ' ') -eq '-Port "8443" -Elevated') 'nothing to ask for when no password was given'
$fromEnv = Get-NwdashRelaunchArgs -Bound ([ordered]@{ Port = 8443 }) -Given @('AuthPassword')
Assert ($fromEnv -contains '-PromptAuthPassword') 'a password handed over by Setup-NWDash.cmd (not bound) is asked for too'

# ---- the auth plan asks instead of refusing when it can ----
Assert ((Get-NwdashAuthPlan -PasswordGiven $false -PasswordStored $false -CanPrompt $true) -eq 'prompt') 'no password, interactive: ask for one'
Assert ((Get-NwdashAuthPlan -PasswordGiven $false -PasswordStored $false -CanPrompt $false) -eq 'refused') 'no password, unattended: refused as before'
Assert ((Get-NwdashAuthPlan -PasswordGiven $false -PasswordStored $true -CanPrompt $true) -eq 'stored') 'a stored password is not asked for again'
Assert ((Get-NwdashAuthPlan -PasswordGiven $false -PasswordStored $false -BindHost '127.0.0.1' -CanPrompt $true) -eq 'loopback') 'loopback needs none'

# ---- asking: hidden, confirmed, and refused when nothing is typed ----
$script:answers = @()
function Read-Host {
  param([string]$Prompt, [switch]$AsSecureString)
  $next, $script:answers = $script:answers
  $secure = New-Object Security.SecureString
  foreach ($c in "$next".ToCharArray()) { $secure.AppendChar($c) }
  return $secure
}
function Write-Warning { param([string]$Message) }

$typed = "p$([char]0xE4)ss w0rd"  # Windows PowerShell 5.1 has no `u{} escape
$script:answers = @('first-try', 'typo', $typed, $typed)
Assert ((Read-NwdashSecret -Label 'Dashboard password' -Twice) -ceq $typed) 'a mismatch asks again; spaces and non-ASCII survive'
$script:answers = @('only-once')
Assert ((Read-NwdashSecret -Label 'Password for EXAMPLE\svc') -eq 'only-once') 'a service password is typed once'
$script:answers = @('', '', '')
$refused = $false
try { Read-NwdashSecret -Label 'Dashboard password' -Twice | Out-Null } catch { $refused = "$_" -match 'no dashboard password was entered' }
Assert $refused 'three empty answers stop the install before anything changed'

Write-Host 'secret-handoff: all tests passed'
