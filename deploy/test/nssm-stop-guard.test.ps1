# Regression test for the STOP_PENDING-throw guard (ported from DEDB PR #144).
#
# nssm.exe prints "Unexpected status SERVICE_STOP_PENDING in response to STOP
# control." to stderr when a stop control returns STOP_PENDING (a slow-draining
# app). Under Windows PowerShell 5.1 with $ErrorActionPreference='Stop', that
# native stderr is promoted to a TERMINATING error EVEN WITH 2>$null - which
# would abort an -Upgrade BEFORE the file swap. The guard wraps every
# `nssm stop NetWorkerDashboard` in try/catch; Wait-NwdashCleanStop +
# Wait-NwdashServiceState remain the real arbiters that the SCM reached
# Stopped. This test locks the guard at the SOURCE level plus a behavioral
# check that the wrapper swallows a terminating stop error. Run:
#   powershell -NoProfile -ExecutionPolicy Bypass -File deploy\test\nssm-stop-guard.test.ps1
$ErrorActionPreference = 'Stop'
function Assert($cond, $msg) { if (-not $cond) { throw "FAIL: $msg" } else { Write-Host "  PASS $msg" } }

$root = Join-Path $PSScriptRoot '..'
# file -> minimum number of nssm-stop sites expected in it (guards against a
# future edit silently dropping a site the test thought it was covering).
$targets = @{
  'install.ps1'     = 3
  'lib\common.ps1'  = 1
}

# A stop site is guarded when the try { & $x stop <svc> ... } catch { } is on
# the same line (the pattern every site in this repo uses).
$stop  = 'stop\s+(\$SvcName|\$svcName|NetWorkerDashboard)'
$guard = 'try\s*\{[^}]*stop\s+(\$SvcName|\$svcName|NetWorkerDashboard)[^}]*2>\$null[^}]*\}\s*catch\s*\{'

foreach ($rel in $targets.Keys) {
  $path = Join-Path $root $rel
  Assert (Test-Path $path) "$rel found"
  $stopLines = @(Get-Content $path | Where-Object { $_ -match $stop -and $_ -notmatch '^\s*#' })
  Assert ($stopLines.Count -ge $targets[$rel]) "$rel has >= $($targets[$rel]) nssm-stop site(s) (found $($stopLines.Count))"
  foreach ($ln in $stopLines) {
    Assert ($ln -match $guard) "$rel : nssm stop is try/catch-guarded -> $($ln.Trim())"
  }
}

# Behavioral: the try/catch wrapper swallows a terminating stop error (models
# the STOP_PENDING native-stderr throw) and never propagates it to the caller.
$threw = $false
try { try { & { throw 'NetWorkerDashboard: Unexpected status SERVICE_STOP_PENDING in response to STOP control.' } 2>$null | Out-Null } catch { } }
catch { $threw = $true }
Assert (-not $threw) 'the stop wrapper swallows a terminating STOP_PENDING error (does not propagate)'

Write-Host 'ALL PASS'
