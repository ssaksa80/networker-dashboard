@echo off
REM ===========================================================================
REM  NetWorker Dashboard - setup bootstrap (thin launcher).
REM
REM  Put this file NEXT TO a nwdash-bundle-<version>-win-x64.zip and
REM  double-click it. It finds the newest bundle beside it (checking GitHub for
REM  a newer published release first), unpacks the bundle to a temporary
REM  staging folder, and hands off to the installer that ships INSIDE the
REM  bundle (deploy\install.ps1). All install/upgrade/service logic lives in
REM  the bundle, so this launcher never goes stale (DEDB/FMD lesson).
REM
REM  Usage (arguments are forwarded to deploy\install.ps1 unchanged):
REM    Setup-NWDash.cmd                          fresh install / migrate the
REM                                              legacy scheduled-task install
REM    Setup-NWDash.cmd -InstallDir D:\some\dir  custom install directory
REM    Setup-NWDash.cmd -Check                   dry-run: print the plan only
REM    Setup-NWDash.cmd -Upgrade                 upgrade (config + data kept)
REM    Setup-NWDash.cmd -Rollback                restore the newest backup
REM    Setup-NWDash.cmd -Uninstall [-Purge]      remove service + files
REM    Setup-NWDash.cmd -Port 9443 -BindHost 10.0.0.5 -AuthPassword ...
REM    Setup-NWDash.cmd -NoUpdate ...            skip the latest-release check
REM                                              (air-gapped hosts; handled by
REM                                              this bootstrap, not forwarded)
REM
REM  Version detection: checks GitHub for the newest published release (via an
REM  authenticated gh CLI, else a GITHUB_TOKEN env var) and downloads a newer
REM  nwdash-bundle-*-win-x64.zip automatically. Offline or credential-less
REM  hosts fall back to the local bundle - the check never blocks an install.
REM
REM  The installer registers a REAL Windows service 'NetWorkerDashboard'
REM  (services.msc) via the bundled nssm.exe, running the bundled embedded
REM  Python - the target host needs NOTHING preinstalled.
REM ===========================================================================
powershell -NoProfile -ExecutionPolicy Bypass -Command "$m='#PS'+'BEGIN';$sc=[IO.File]::ReadAllText('%~f0');$ps=$sc.Substring($sc.IndexOf($m)+$m.Length);& ([scriptblock]::Create($ps)) '%~dp0' '%~f0' %*"
exit /b %ERRORLEVEL%

REM Everything below runs as PowerShell, not cmd.
#PSBEGIN
param(
    [string]$ScriptDir,
    [string]$ScriptPath,
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Rest
)
$ErrorActionPreference = "Stop"

function Fail([string]$msg) {
    Write-Host ""
    Write-Host "ERROR: $msg" -ForegroundColor Red
    exit 1
}

# -NoUpdate is the bootstrap's own switch (skip the release check); everything
# else is forwarded to the bundle's deploy\install.ps1 verbatim.
$NoUpdate = $false
$fwd = @()
foreach ($a in @($Rest)) {
    if ($a -ieq "-NoUpdate") { $NoUpdate = $true } else { $fwd += $a }
}

Write-Host "=== NetWorker Dashboard setup (bootstrap) ==="

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
    if ($name -match "nwdash-bundle-(\d+\.\d+\.\d+)-win-x64\.zip") { return [version]$Matches[1] }
    return $null
}
function Select-NewestBundle {
    Get-ChildItem -Path $ScriptDir -Filter "nwdash-bundle-*-win-x64.zip" |
        Where-Object { Get-BundleVersion $_.Name } |
        Sort-Object { Get-BundleVersion $_.Name } -Descending | Select-Object -First 1
}
$bundle = Select-NewestBundle
$localVer = if ($bundle) { Get-BundleVersion $bundle.Name } else { $null }

# --- 1b. Latest published release check + self-update of the bundle ----------
# Fail-soft by design: air-gapped hosts, missing credentials, or GitHub being
# unreachable all fall back to whatever bundle sits next to this script.
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
            Write-Host "Update  : downloading nwdash-bundle-$latestVer-win-x64.zip ..."
            $ok = $false
            if ($gh) {
                try {
                    & gh release download $latestTag --repo $repo --pattern "nwdash-bundle-*" --dir $ScriptDir --clobber 2>$null
                    $ok = ($LASTEXITCODE -eq 0)
                } catch { }
            }
            if (-not $ok -and $env:GITHUB_TOKEN) {
                try {
                    $rel = Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/releases/tags/$latestTag" `
                        -Headers @{ Authorization = "Bearer $($env:GITHUB_TOKEN)"; Accept = "application/vnd.github+json" } -TimeoutSec 15
                    $asset = $rel.assets | Where-Object { $_.name -like "nwdash-bundle-*-win-x64.zip" } | Select-Object -First 1
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
    Fail "No nwdash-bundle-*-win-x64.zip found next to this script and no release could be downloaded. Download the bundle from the GitHub release and place it in the same folder."
}
Write-Host "Bundle  : $($bundle.Name)"

# --- 2. Unpack to a temp staging dir ------------------------------------------
$staging = Join-Path $env:TEMP ("nwdash-setup-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $staging | Out-Null
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive = [System.IO.Compression.ZipFile]::OpenRead($bundle.FullName)
try {
    $entries = @($archive.Entries | Where-Object { $_.Name })  # skip directory entries
    $total = $entries.Count
    $done = 0
    Show-Bar "Unpacking" 0
    foreach ($entry in $entries) {
        $rel = $entry.FullName -replace "/", "\"
        $dest = Join-Path $staging $rel
        $destDir = Split-Path -Parent $dest
        if (-not (Test-Path $destDir)) { New-Item -ItemType Directory -Force -Path $destDir | Out-Null }
        [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $dest, $true)
        $done++
        Show-Bar "Unpacking" ([int](100 * $done / $total))
    }
}
finally {
    $archive.Dispose()
}

# --- 3. Hand off to the installer that ships inside the bundle ----------------
$installer = Join-Path $staging "deploy\install.ps1"
if (-not (Test-Path $installer)) {
    Remove-Item -Recurse -Force $staging -ErrorAction SilentlyContinue
    Fail "$($bundle.Name) does not contain deploy\install.ps1 - it is an old-layout bundle. Download the current nwdash-bundle-<version>-win-x64.zip from the latest GitHub release."
}
Write-Host "Handoff : $installer $($fwd -join ' ')"
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installer @fwd
$code = $LASTEXITCODE
Remove-Item -Recurse -Force $staging -ErrorAction SilentlyContinue
exit $code
