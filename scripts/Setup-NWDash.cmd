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
REM    Setup-NWDash.cmd D:\some\dir -Silent no prompts
REM    Setup-NWDash.cmd -Service            register the boot task instead of a
REM                                         plain background start (needs an
REM                                         elevated prompt); -Port NNNN to
REM                                         change the HTTPS port (default 8443)
REM
REM  The dashboard always runs in the BACKGROUND after setup (scheduled task
REM  with -Service, hidden process otherwise) and the start is health-gated
REM  against /api/health. When setup completes you are offered a real-time
REM  log tail (Ctrl+C stops the tail only, never the dashboard).
REM
REM  With the boot task registered the dashboard runs as SYSTEM, starts on
REM  every Windows boot, and is restarted up to 3 times if it crashes. Note:
REM  if the app was previously run manually under a user account, its
REM  DPAPI-protected keys regenerate once on the first SYSTEM run (the app
REM  prints a warning; saved sessions must be re-entered once).
REM
REM  Requires: Windows PowerShell 5.1+ (in-box) and Python 3.11+ on PATH.
REM ===========================================================================
powershell -NoProfile -ExecutionPolicy Bypass -Command "$m='#PS'+'BEGIN';$sc=[IO.File]::ReadAllText('%~f0');$ps=$sc.Substring($sc.IndexOf($m)+$m.Length);& ([scriptblock]::Create($ps)) '%~dp0' %*"
exit /b %ERRORLEVEL%

REM Everything below runs as PowerShell, not cmd.
#PSBEGIN
param([string]$ScriptDir, [string]$InstallDir = "C:\apps\networker-dashboard", [switch]$Silent, [switch]$Service, [int]$Port = 8443)

$ErrorActionPreference = "Stop"

function Fail([string]$msg) {
    Write-Host ""
    Write-Host "ERROR: $msg" -ForegroundColor Red
    if (-not $Silent) { Read-Host "Press Enter to close" | Out-Null }
    exit 1
}

Write-Host "=== NetWorker Dashboard setup ==="

# Poll the app's own /api/health over HTTPS (self-signed cert -> trust-all)
# instead of trusting process/task state. Returns $true when live.
function Wait-DashHealth([int]$HealthPort, [int]$Seconds) {
    Add-Type -TypeDefinition "using System.Net;using System.Security.Cryptography.X509Certificates;public class TrustAll:ICertificatePolicy{public bool CheckValidationResult(ServicePoint a,X509Certificate b,WebRequest c,int d){return true;}}" -ErrorAction SilentlyContinue
    $oldPolicy = [System.Net.ServicePointManager]::CertificatePolicy
    [System.Net.ServicePointManager]::CertificatePolicy = New-Object TrustAll
    try {
        foreach ($i in 1..$Seconds) {
            Start-Sleep -Seconds 1
            try {
                $resp = (New-Object System.Net.WebClient).DownloadString("https://127.0.0.1:$HealthPort/api/health")
                if ($resp -match '"ok"\s*:\s*true') { return $true }
            } catch { }
        }
        return $false
    }
    finally {
        [System.Net.ServicePointManager]::CertificatePolicy = $oldPolicy
    }
}

# Follow the app's rotating log in real time. Ctrl+C stops tailing only;
# the dashboard keeps running in the background.
function Show-LogTail([string]$Dir) {
    $log = Join-Path $Dir "logs\networker_dashboard.log"
    foreach ($i in 1..10) {
        if (Test-Path $log) { break }
        Start-Sleep -Seconds 1
    }
    if (-not (Test-Path $log)) {
        Write-Host "Log file not found yet: $log"
        return
    }
    Write-Host ""
    Write-Host "--- Tailing $log (Ctrl+C stops tailing; the dashboard keeps running) ---"
    Get-Content -LiteralPath $log -Wait -Tail 20
}

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

# --- 5. Boot-survival: register a startup task so the dashboard is live after
#        every Windows reboot, restarted automatically if it crashes. ----------
$taskName = "NetWorkerDashboard"
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$wantService = $Service
if (-not $Silent -and -not $Service) {
    $ans = Read-Host "Register auto-start at boot (runs as SYSTEM, survives reboot)? [Y/n]"
    if ($ans -eq "" -or $ans -match "^[Yy]") { $wantService = $true }
}
if ($wantService) {
    if (-not $isAdmin) {
        Fail "Registering the boot task requires an elevated prompt. Re-run this script as Administrator (right-click, 'Run as administrator')."
    }
    $pythonExe = (Get-Command $python).Source
    $action = New-ScheduledTaskAction -Execute $pythonExe `
        -Argument "networker_dashboard.py --port $Port --no-launch" `
        -WorkingDirectory $InstallDir
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    # Default execution time limit kills the task after 72h; PT0S = unlimited.
    # (The cmdlet parameter rejects a zero timespan on PowerShell 5.1, so set
    # it on the settings object directly.)
    $settings.ExecutionTimeLimit = "PT0S"
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings | Out-Null
    Write-Host "Task    : '$taskName' registered (at boot, SYSTEM, 3 crash restarts, port $Port)"
    Start-ScheduledTask -TaskName $taskName
    if (Wait-DashHealth $Port 20) {
        Write-Host "Health  : dashboard is LIVE at https://localhost:$Port/ (will come back after every reboot)"
    } else {
        Fail "The boot task was registered but the dashboard did not answer /api/health on port $Port within 20 seconds. Check Task Scheduler ('$taskName') and the app log in $InstallDir\logs."
    }
}
else {
    # --- 6. No boot task: start the dashboard in the background ---------------
    $pythonExe = (Get-Command $python).Source
    Start-Process -FilePath $pythonExe `
        -ArgumentList "networker_dashboard.py --port $Port --no-launch" `
        -WorkingDirectory $InstallDir -WindowStyle Hidden
    Write-Host "Start   : dashboard launched in the background (port $Port)"
    if (Wait-DashHealth $Port 20) {
        Write-Host "Health  : dashboard is LIVE at https://localhost:$Port/"
        Write-Host "Note    : this instance does NOT survive a reboot; re-run with -Service (elevated) for that."
    } else {
        Fail "The dashboard did not answer /api/health on port $Port within 20 seconds. Check the app log in $InstallDir\logs."
    }
}

# --- 7. Offer a real-time log tail --------------------------------------------
Write-Host ""
Write-Host "Setup complete."
if (-not $Silent) {
    $ans = Read-Host "Tail the live log now? [Y/n]"
    if ($ans -eq "" -or $ans -match "^[Yy]") {
        Show-LogTail $InstallDir
    }
} else {
    Write-Host "Tail the log any time with:"
    Write-Host "  powershell -Command `"Get-Content '$InstallDir\logs\networker_dashboard.log' -Wait -Tail 20`""
}
exit 0
