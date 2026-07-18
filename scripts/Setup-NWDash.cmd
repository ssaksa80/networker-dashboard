@echo off
REM ===========================================================================
REM  NetWorker Dashboard - one-click installer.
REM
REM  Put this file NEXT TO a networker-dashboard-<version>-bundle.zip and
REM  double-click it. It extracts the newest bundle to the install directory,
REM  preserves existing runtime state (data\, .certs\, logs\) on upgrade,
REM  offers to set the dashboard password on first install, and can start
REM  the app when done.
REM
REM  Usage:
REM    Setup-NWDash.cmd                     install to C:\apps\networker-dashboard
REM    Setup-NWDash.cmd D:\some\dir         install to a custom directory
REM    Setup-NWDash.cmd D:\some\dir -Silent no prompts (no password set, no start)
REM
REM  Requires: Windows PowerShell 5.1+ (in-box) and Python 3.11+ on PATH.
REM ===========================================================================
powershell -NoProfile -ExecutionPolicy Bypass -Command "$m='#PS'+'BEGIN';$sc=[IO.File]::ReadAllText('%~f0');$ps=$sc.Substring($sc.IndexOf($m)+$m.Length);& ([scriptblock]::Create($ps)) '%~dp0' %*"
exit /b %ERRORLEVEL%

REM Everything below runs as PowerShell, not cmd.
#PSBEGIN
param([string]$ScriptDir, [string]$InstallDir = "C:\apps\networker-dashboard", [switch]$Silent)

$ErrorActionPreference = "Stop"

function Fail([string]$msg) {
    Write-Host ""
    Write-Host "ERROR: $msg" -ForegroundColor Red
    if (-not $Silent) { Read-Host "Press Enter to close" | Out-Null }
    exit 1
}

Write-Host "=== NetWorker Dashboard setup ==="

# Console progress bar: [##########------------------]  42%  (ASCII, in-place)
function Show-Bar([string]$label, [int]$pct) {
    if ($pct -lt 0) { $pct = 0 }
    if ($pct -gt 100) { $pct = 100 }
    $width = 30
    $filled = [int][math]::Floor($width * $pct / 100)
    $bar = ("#" * $filled) + ("-" * ($width - $filled))
    Write-Host -NoNewline ("`r{0,-12} [{1}] {2,3}%" -f $label, $bar, $pct)
    if ($pct -eq 100) { Write-Host "" }
}

# --- 1. Find the newest bundle next to this script ---------------------------
$bundle = Get-ChildItem -Path $ScriptDir -Filter "networker-dashboard-*-bundle.zip" |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $bundle) {
    Fail "No networker-dashboard-*-bundle.zip found next to this script. Download the bundle from the GitHub release and place it in the same folder."
}
Write-Host "Bundle : $($bundle.Name)"

# --- 2. Check Python ----------------------------------------------------------
$python = $null
foreach ($cand in @("python", "py")) {
    $cmd = Get-Command $cand -ErrorAction SilentlyContinue
    if ($cmd) {
        try {
            $v = & $cand -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($v -and ([version]$v -ge [version]"3.11")) { $python = $cand; $pyver = $v; break }
        } catch { }
    }
}
if (-not $python) {
    Fail "Python 3.11+ was not found on PATH. Install Python and re-run."
}
Write-Host "Python : $python ($pyver)"

# --- 3. Extract (preserving runtime state on upgrade) -------------------------
Write-Host "Target : $InstallDir"
$isUpgrade = Test-Path (Join-Path $InstallDir "networker_dashboard.py")
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive = [System.IO.Compression.ZipFile]::OpenRead($bundle.FullName)
try {
    $entries = @($archive.Entries | Where-Object { $_.Name })  # skip directory entries
    $total = $entries.Count
    $done = 0
    $installed = @()
    Show-Bar "Unarchiving" 0
    foreach ($entry in $entries) {
        $rel = $entry.FullName -replace "/", "\"
        # The bundle never contains runtime state, but guard anyway so a
        # hand-modified zip can never clobber keys or certificates.
        if ($rel -notmatch '^(data|logs|\.certs)\\') {
            $dest = Join-Path $InstallDir $rel
            $destDir = Split-Path -Parent $dest
            if (-not (Test-Path $destDir)) { New-Item -ItemType Directory -Force -Path $destDir | Out-Null }
            [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $dest, $true)
            $installed += @{ Path = $dest; Size = $entry.Length }
        }
        $done++
        Show-Bar "Unarchiving" ([int](100 * $done / $total))
    }
}
finally {
    $archive.Dispose()
}

# Installation pass: verify every extracted file landed with the right size,
# and byte-compile the Python sources so first start is import-error-free.
$done = 0
Show-Bar "Installing" 0
foreach ($item in $installed) {
    $f = Get-Item -LiteralPath $item.Path -ErrorAction SilentlyContinue
    if (-not $f -or $f.Length -ne $item.Size) {
        Write-Host ""
        Fail ("Installed file failed verification: {0}" -f $item.Path)
    }
    $done++
    Show-Bar "Installing" ([int](90 * $done / $installed.Count))
}
& $python -m compileall -q $InstallDir 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Fail "Python byte-compilation of the installed sources failed."
}
Show-Bar "Installing" 100
if ($isUpgrade) {
    Write-Host "Result : upgraded application files (data\, .certs\, logs\ untouched)"
} else {
    Write-Host "Result : fresh install"
}

# --- 4. First-install password ------------------------------------------------
$authFile = Join-Path $InstallDir "data\auth.json"
$passwordArg = @()
if (-not $Silent -and -not (Test-Path $authFile)) {
    Write-Host ""
    $sec = Read-Host "Set a dashboard password now (Enter to skip)" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    if ($plain) {
        # Passed only to the app process; the app stores a salted PBKDF2 hash.
        $env:DASHBOARD_AUTH_PASSWORD = $plain
        $plain = $null
    }
}

# --- 5. Offer to start ----------------------------------------------------------
Write-Host ""
Write-Host "Installed. Start later with:"
Write-Host "  cd `"$InstallDir`" && $python networker_dashboard.py"
if (-not $Silent) {
    $ans = Read-Host "Start the dashboard now? [Y/n]"
    if ($ans -eq "" -or $ans -match "^[Yy]") {
        Set-Location $InstallDir
        & $python networker_dashboard.py
    }
}
exit 0
