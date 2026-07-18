# Standalone test for the nssm `get` output decode + the service-arg parser
# that depends on it. Ported from DEDB's nssm-env-decode test. Run:
#   powershell -NoProfile -ExecutionPolicy Bypass -File deploy\test\nssm-env-decode.test.ps1
#
# Regression class: nssm writes its `get` output as UTF-16LE. PowerShell
# decodes it with a NUL after every real character - INVISIBLE in a console,
# so the output looks perfectly fine when an operator runs it by hand. A
# literal pattern like '--port ' can never match the null-laden string, so an
# upgrade would silently fall back to the default port/bind, probe an
# interface the app never bound, fail health, and roll back a healthy deploy.
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\..\lib\common.ps1"
function Assert($cond, $msg) { if (-not $cond) { throw "FAIL: $msg" } else { Write-Host "  PASS $msg" } }

# The real shape of the service's AppParameters, as PowerShell receives it.
$real = 'networker_dashboard.py --port 9443 --bind 192.0.2.26 --no-launch'
# Simulate the UTF-16LE-decoded-as-8bit mangling: every char followed by a NUL.
function Mangle([string]$s) { ($s.ToCharArray() | ForEach-Object { "$_`0" }) -join '' }
$nullLaden = Mangle $real

# ---- case 1: the raw mangled text defeats a literal match (this is the bug) ----
Assert ($nullLaden.Contains([char]0)) 'repro: nssm-style output contains NULs'
Assert (-not ($nullLaden -match '--port\s+(\d+)')) 'repro: literal --port does NOT match the null-laden text'

# ---- case 2: ConvertFrom-NwdashNssmOutput strips the NULs so the match works ----
$clean = ConvertFrom-NwdashNssmOutput -Raw @($nullLaden)
Assert (-not $clean.Contains([char]0)) 'decode: NULs are stripped'
$parsed = Get-NwdashServiceArgs -Parameters $clean
Assert ($parsed.Port -eq 9443) 'decode: --port value recovered exactly'
Assert ($parsed.Bind -eq '192.0.2.26') 'decode: --bind value recovered exactly'

# ---- case 3: already-clean output passes through unharmed ----
$parsed2 = Get-NwdashServiceArgs -Parameters (ConvertFrom-NwdashNssmOutput -Raw @($real))
Assert ($parsed2.Port -eq 9443) 'clean input: port unchanged'
Assert ($parsed2.Bind -eq '192.0.2.26') 'clean input: bind unchanged'

# ---- case 4: empty / null input never throws, falls back to defaults ----
Assert ((ConvertFrom-NwdashNssmOutput -Raw @()) -eq '') 'empty input -> empty string'
Assert ((ConvertFrom-NwdashNssmOutput -Raw $null) -eq '') 'null input -> empty string'
$def = Get-NwdashServiceArgs -Parameters '' -DefaultPort 8443
Assert ($def.Port -eq 8443) 'no parameters: default port used'
Assert ($def.Bind -eq '') 'no parameters: no bind pin'

# ---- case 5: an unpinned command line yields no bind (loopback probe) ----
$unpinned = Get-NwdashServiceArgs -Parameters 'networker_dashboard.py --port 8443 --no-launch'
Assert ($unpinned.Bind -eq '') 'unpinned: bind empty'
Assert ((Get-NwdashProbeHost -BindHost $unpinned.Bind) -eq '127.0.0.1') 'unpinned: probe targets loopback'
Assert ((Get-NwdashProbeHost -BindHost '192.0.2.26') -eq '192.0.2.26') 'pinned: probe targets the pinned IP'
Assert ((Get-NwdashProbeHost -BindHost '0.0.0.0') -eq '127.0.0.1') 'wildcard bind: probe targets loopback'

Write-Host 'ALL PASS'
