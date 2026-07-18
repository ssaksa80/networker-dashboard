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
REM    Setup-NWDash.cmd -Uninstall          stop the app, remove the boot task
REM                                         and application files; KEEPS data\,
REM                                         .certs\ and logs\ (keys, snapshots,
REM                                         saved profiles)
REM    Setup-NWDash.cmd -Uninstall -Purge   full removal including runtime state
REM    Setup-NWDash.cmd -NoUpdate           skip the latest-release check and
REM                                         use only the bundle beside this
REM                                         script (air-gapped hosts)
REM    (add the install dir as first argument if it is not the default)
REM
REM  Version detection: setup checks GitHub for the newest published release
REM  (via an authenticated gh CLI, else a GITHUB_TOKEN env var) and downloads
REM  a newer bundle automatically. Offline or credential-less hosts fall back
REM  to the local bundle - the check never blocks an install.
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
param([string]$ScriptDir, [string]$InstallDir = "C:\apps\networker-dashboard", [switch]$Silent, [switch]$Service, [int]$Port = 8443, [switch]$Uninstall, [switch]$Purge, [switch]$NoUpdate)

$ErrorActionPreference = "Stop"

function Fail([string]$msg) {
    Write-Host ""
    Write-Host "ERROR: $msg" -ForegroundColor Red
    if (-not $Silent) { Read-Host "Press Enter to close" | Out-Null }
    exit 1
}

if ($Uninstall) {
    Write-Host "=== NetWorker Dashboard uninstall ==="
    if (-not (Test-Path (Join-Path $InstallDir "networker_dashboard.py")) -and -not ($Purge -and (Test-Path $InstallDir))) {
        Write-Host "No installation found at $InstallDir - nothing to do."
        exit 0
    }
    # 1. Boot task (if present). Unregistering needs elevation; wrap every
    #    scheduler call - on PS 5.1 their stderr becomes terminating under
    #    ErrorActionPreference=Stop even when the operation half-succeeds.
    $task = Get-ScheduledTask -TaskName "NetWorkerDashboard" -ErrorAction SilentlyContinue
    if ($task) {
        $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
        if (-not $isAdmin) {
            Write-Host ""
            Write-Host "ERROR: the 'NetWorkerDashboard' boot task exists; removing it requires an elevated prompt. Re-run as Administrator." -ForegroundColor Red
            exit 1
        }
        try { Stop-ScheduledTask -TaskName "NetWorkerDashboard" -ErrorAction Stop } catch { }
        try { Unregister-ScheduledTask -TaskName "NetWorkerDashboard" -Confirm:$false -ErrorAction Stop; Write-Host "Task    : removed" } catch { Write-Host "Task    : removal failed ($($_.Exception.Message))" }
    } else {
        Write-Host "Task    : none registered"
    }
    # 2. Stop any running dashboard instance (matched by command line, so an
    #    instance started by hand is caught too).
    $killed = 0
    Get-CimInstance Win32_Process -Filter "Name='python.exe' or Name='py.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match "networker_dashboard\.py" } |
        ForEach-Object {
            try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; $killed++ } catch { }
        }
    Write-Host "Process : $killed stopped"
    Start-Sleep -Seconds 1
    # 3. Remove files. Default keeps runtime state (data\ holds keys, snapshots,
    #    saved profiles, automations); -Purge removes everything.
    if ($Purge) {
        Remove-Item -LiteralPath $InstallDir -Recurse -Force
        Write-Host "Files   : removed $InstallDir (INCLUDING data\, .certs\, logs\ - purged)"
    } else {
        foreach ($item in @("networker_dashboard.py", "nwdash", "README.md", "pyproject.toml", "__pycache__")) {
            $p = Join-Path $InstallDir $item
            if (Test-Path $p) { Remove-Item -LiteralPath $p -Recurse -Force }
        }
        Write-Host "Files   : application removed; kept data\, .certs\, logs\ in $InstallDir"
        Write-Host "          (re-run with -Uninstall -Purge to delete those too)"
    }
    Write-Host "Uninstall complete."
    exit 0
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
function Get-BundleVersion([string]$name) {
    if ($name -match "networker-dashboard-(\d+\.\d+\.\d+)-bundle\.zip") { return [version]$Matches[1] }
    return $null
}
function Select-NewestBundle {
    Get-ChildItem -Path $ScriptDir -Filter "networker-dashboard-*-bundle.zip" |
        Where-Object { Get-BundleVersion $_.Name } |
        Sort-Object { Get-BundleVersion $_.Name } -Descending | Select-Object -First 1
}
$bundle = Select-NewestBundle
$localVer = if ($bundle) { Get-BundleVersion $bundle.Name } else { $null }

# --- 1b. Detect the latest published release and self-update the bundle -------
# Fail-soft by design: air-gapped hosts, missing credentials, or GitHub being
# unreachable all fall back to whatever bundle sits next to this script.
# Skip entirely with -NoUpdate.
if (-not $NoUpdate) {
    $repo = "ssaksa80/networker-dashboard"
    $latestTag = $null
    $gh = Get-Command gh -ErrorAction SilentlyContinue
    if ($gh) {
        try {
            $json = & gh release view --repo $repo --json tagName,assets 2>$null | ConvertFrom-Json
            if ($json -and $json.tagName) { $latestTag = $json.tagName }
        } catch { }
    }
    if (-not $latestTag -and $env:GITHUB_TOKEN) {
        try {
            $rel = Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/releases/latest" `
                -Headers @{ Authorization = "Bearer $($env:GITHUB_TOKEN)"; Accept = "application/vnd.github+json" } -TimeoutSec 15
            if ($rel.tag_name) { $latestTag = $rel.tag_name }
        } catch { }
    }
    if ($latestTag -and $latestTag -match "^v?(\d+\.\d+\.\d+)$") {
        $latestVer = [version]$Matches[1]
        $localText = if ($localVer) { "v$localVer" } else { "none" }
        Write-Host "Version : latest published $latestTag, local bundle $localText"
        if (-not $localVer -or $latestVer -gt $localVer) {
            Write-Host "Update  : downloading networker-dashboard-$latestVer-bundle.zip ..."
            $ok = $false
            if ($gh) {
                try {
                    & gh release download $latestTag --repo $repo --pattern "networker-dashboard-*-bundle.zip" --dir $ScriptDir --clobber 2>$null
                    $ok = ($LASTEXITCODE -eq 0)
                } catch { }
            }
            if (-not $ok -and $env:GITHUB_TOKEN) {
                try {
                    $rel = Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/releases/tags/$latestTag" `
                        -Headers @{ Authorization = "Bearer $($env:GITHUB_TOKEN)"; Accept = "application/vnd.github+json" } -TimeoutSec 15
                    $asset = $rel.assets | Where-Object { $_.name -like "networker-dashboard-*-bundle.zip" } | Select-Object -First 1
                    if ($asset) {
                        Invoke-WebRequest -Uri "https://api.github.com/repos/$repo/releases/assets/$($asset.id)" `
                            -Headers @{ Authorization = "Bearer $($env:GITHUB_TOKEN)"; Accept = "application/octet-stream" } `
                            -OutFile (Join-Path $ScriptDir $asset.name) -TimeoutSec 300
                        $ok = $true
                    }
                } catch { }
            }
            if ($ok) {
                $bundle = Select-NewestBundle
                $localVer = if ($bundle) { Get-BundleVersion $bundle.Name } else { $null }
                if ($localVer -ne $latestVer) { Write-Host "Update  : download did not produce v$latestVer; continuing with local bundle" }
            } else {
                Write-Host "Update  : download unavailable (no gh auth / GITHUB_TOKEN); continuing with local bundle"
            }
        }
    } else {
        Write-Host "Version : latest-release check unavailable (offline or no gh/GITHUB_TOKEN); using local bundle"
    }
}

if (-not $bundle) {
    Fail "No networker-dashboard-*-bundle.zip found next to this script and no release could be downloaded. Download the bundle from the GitHub release and place it in the same folder."
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
