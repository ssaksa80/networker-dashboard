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
REM    Setup-NWDash.cmd -Port 9443 -BindHost 10.0.0.5   asks for the dashboard
REM                                              password (-AuthPassword ... for
REM                                              unattended installs)
REM    Setup-NWDash.cmd -Update ...              download a newer published
REM                                              bundle, verified by SHA-256
REM    Setup-NWDash.cmd -NoUpdate ...            skip the latest-release check
REM                                              (air-gapped hosts; both are
REM                                              handled here, not forwarded)
REM
REM  Version detection: checks GitHub for the newest published release (via an
REM  authenticated gh CLI, else a GITHUB_TOKEN env var) and says when it is newer
REM  than the local bundle. Only -Update downloads it, and only a download whose
REM  SHA-256 matches the release's published hash is used. Offline or
REM  credential-less hosts use the local bundle - the check never blocks.
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

# -NoUpdate and -Update are the bootstrap's own switches (step 1b); everything
# else is forwarded to the bundle's deploy\install.ps1, except the passwords,
# which do not go on its command line (step 3).
# Which installer password an argument names, allowing PowerShell's unique
# prefixes (-AuthPass, -ServicePass) and the -Name:value form.
function Get-SecretParam([string]$arg) {
    if ($arg -notmatch "^-([A-Za-z]+)(:|$)") { return $null }
    $n = $Matches[1]
    if ($n.Length -ge 2 -and "AuthPassword".StartsWith($n, [StringComparison]::OrdinalIgnoreCase)) { return "AuthPassword" }
    if ($n.Length -ge 8 -and "ServicePassword".StartsWith($n, [StringComparison]::OrdinalIgnoreCase)) { return "ServicePassword" }
    return $null
}
$NoUpdate = $false
$Update = $false
$fwd = @()
$secrets = [ordered]@{}
$Rest = @($Rest)
for ($i = 0; $i -lt $Rest.Count; $i++) {
    $a = $Rest[$i]
    if ($a -ieq "-NoUpdate") { $NoUpdate = $true; continue }
    if ($a -ieq "-Update") { $Update = $true; continue }
    $secret = Get-SecretParam $a
    if ($secret) {
        # PowerShell hands -Name:value over as "-Name:" and "value".
        if ($a -match "^-[A-Za-z]+:(.+)$") { $secrets[$secret] = $Matches[1] }
        elseif ($i + 1 -lt $Rest.Count) { $i++; $secrets[$secret] = $Rest[$i] }
        continue
    }
    $fwd += $a
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

# --- 1b. Newer published release (review S10) --------------------------------
# What this bootstrap unpacks runs elevated, so a download must be asked for and
# verified. By default it only says that a newer release exists. -Update
# downloads it to a temporary folder and moves it next to this script only when
# its SHA-256 matches both GitHub's digest for the asset and the "Bundle
# SHA-256" line of the release notes. Both come from the same release: this
# catches a corrupted or altered download, not a forged release. Fail-soft:
# offline or credential-less hosts carry on with the local bundle.
function Get-FileSha256([string]$path) {
    $sha = [Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::OpenRead($path)
    try { return ([BitConverter]::ToString($sha.ComputeHash($stream)) -replace "-", "").ToLowerInvariant() }
    finally { $stream.Dispose(); $sha.Dispose() }
}
# Why a downloaded bundle cannot be used, or $null when it verifies.
function Get-DownloadProblem([string]$path, $release, [string]$name) {
    if (-not (Test-Path -LiteralPath $path)) { return "the download did not produce $name" }
    $asset = @($release.Assets | Where-Object { $_.name -eq $name }) | Select-Object -First 1
    $digest = $null
    if ($asset -and "$($asset.digest)" -match '^sha256:([0-9a-fA-F]{64})$') { $digest = $Matches[1].ToLowerInvariant() }
    $noted = $null
    if ("$($release.Body)" -match ('Bundle SHA-256:\**\s*`?([0-9a-fA-F]{64})`?\s*\(`?' + [regex]::Escape($name))) { $noted = $Matches[1].ToLowerInvariant() }
    if (-not $digest) { return "GitHub gives no SHA-256 digest for $name, so it cannot be verified" }
    if (-not $noted) { return "the release notes give no Bundle SHA-256 for $name, so it cannot be verified" }
    if ($digest -ne $noted) { return "GitHub's digest and the release notes disagree about $name" }
    $actual = Get-FileSha256 $path
    if ($actual -ne $digest) { return "$name does not match its published SHA-256 (got $actual, expected $digest)" }
    return $null
}
if (-not $NoUpdate) {
    $repo = "ssaksa80/networker-dashboard"
    $release = $null
    $gh = Get-Command gh -ErrorAction SilentlyContinue
    if ($gh) {
        try {
            $json = & gh release view --repo $repo --json tagName,body,assets 2>$null | ConvertFrom-Json
            if ($json -and $json.tagName) { $release = @{ Tag = $json.tagName; Body = $json.body; Assets = @($json.assets) } }
        } catch { }
    }
    if (-not $release -and $env:GITHUB_TOKEN) {
        try {
            $rel = Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/releases/latest" `
                -Headers @{ Authorization = "Bearer $($env:GITHUB_TOKEN)"; Accept = "application/vnd.github+json" } -TimeoutSec 15
            if ($rel.tag_name) { $release = @{ Tag = $rel.tag_name; Body = $rel.body; Assets = @($rel.assets) } }
        } catch { }
    }
    if ($release -and $release.Tag -match "^v?(\d+\.\d+\.\d+)$") {
        $latestTag = $release.Tag
        $latestVer = [version]$Matches[1]
        $localText = if ($localVer) { "v$localVer" } else { "none" }
        Write-Host "Version : latest published $latestTag, local bundle $localText"
        if (-not $localVer -or $latestVer -gt $localVer) {
            $name = "nwdash-bundle-$latestVer-win-x64.zip"
            if (-not $Update) {
                Write-Host "Update  : $latestTag is available. Re-run with -Update to download and verify it, or put $name next to this script."
            } else {
                Write-Host "Update  : downloading $name ..."
                $download = Join-Path $env:TEMP ("nwdash-update-" + [guid]::NewGuid().ToString("N"))
                New-Item -ItemType Directory -Force -Path $download | Out-Null
                try {
                    $file = Join-Path $download $name
                    if ($gh) {
                        try { & gh release download $latestTag --repo $repo --pattern $name --dir $download --clobber 2>$null } catch { }
                    }
                    if (-not (Test-Path -LiteralPath $file) -and $env:GITHUB_TOKEN) {
                        $asset = @($release.Assets | Where-Object { $_.name -eq $name }) | Select-Object -First 1
                        if ($asset -and $asset.id) {
                            try {
                                Invoke-WebRequest -Uri "https://api.github.com/repos/$repo/releases/assets/$($asset.id)" `
                                    -Headers @{ Authorization = "Bearer $($env:GITHUB_TOKEN)"; Accept = "application/octet-stream" } `
                                    -OutFile $file -TimeoutSec 300
                            } catch { }
                        }
                    }
                    $problem = Get-DownloadProblem $file $release $name
                    if ($problem) {
                        Write-Host "Update  : NOT used - $problem. Continuing with the local bundle."
                    } else {
                        $verified = Get-FileSha256 $file
                        Move-Item -Force -LiteralPath $file -Destination (Join-Path $ScriptDir $name)
                        Write-Host "Update  : $name verified (SHA-256 $verified)"
                        $bundle = Select-NewestBundle
                        $localVer = if ($bundle) { Get-BundleVersion $bundle.Name } else { $null }
                    }
                } finally {
                    Remove-Item -Recurse -Force $download -ErrorAction SilentlyContinue
                }
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
$outside = $null
try {
    $entries = @($archive.Entries | Where-Object { $_.Name })  # skip directory entries
    # Every entry must land inside the staging folder: a "..\" or absolute name
    # would write wherever this run can (zip-slip, review S10). All of them are
    # checked before any is extracted.
    $root = [IO.Path]::GetFullPath($staging).TrimEnd("\") + "\"
    $targets = @()
    foreach ($entry in $entries) {
        $dest = $null
        try { $dest = [IO.Path]::GetFullPath([IO.Path]::Combine($root, ($entry.FullName -replace "/", "\"))) } catch { }
        if (-not $dest -or -not $dest.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) { $outside = $entry.FullName; break }
        $targets += , @($entry, $dest)
    }
    if (-not $outside) {
        $total = $targets.Count
        $done = 0
        Show-Bar "Unpacking" 0
        foreach ($target in $targets) {
            $destDir = Split-Path -Parent $target[1]
            if (-not (Test-Path $destDir)) { New-Item -ItemType Directory -Force -Path $destDir | Out-Null }
            [System.IO.Compression.ZipFileExtensions]::ExtractToFile($target[0], $target[1], $true)
            $done++
            Show-Bar "Unpacking" ([int](100 * $done / $total))
        }
    }
}
finally {
    $archive.Dispose()
}
if ($outside) {
    Remove-Item -Recurse -Force $staging -ErrorAction SilentlyContinue
    Fail "$($bundle.Name) has an entry that would unpack outside its folder ($outside), so nothing was unpacked. Download the bundle again from the GitHub release and check its SHA-256."
}

# --- 3. Hand off to the installer that ships inside the bundle ----------------
$installer = Join-Path $staging "deploy\install.ps1"
if (-not (Test-Path $installer)) {
    Remove-Item -Recurse -Force $staging -ErrorAction SilentlyContinue
    Fail "$($bundle.Name) does not contain deploy\install.ps1 - it is an old-layout bundle. Download the current nwdash-bundle-<version>-win-x64.zip from the latest GitHub release."
}
# The passwords go in the environment, which the installer reads and clears,
# instead of on its command line, where process-creation auditing records them
# (review S9). An older bundle's installer only takes them as arguments.
$setupEnv = @{ AuthPassword = "NWDASH_SETUP_AUTH_PASSWORD"; ServicePassword = "NWDASH_SETUP_SERVICE_PASSWORD" }
$viaEnv = [IO.File]::ReadAllText($installer) -match "NWDASH_SETUP_AUTH_PASSWORD"
$installerArgs = @($fwd)
$shown = @($fwd)
foreach ($name in @($secrets.Keys)) {
    $shown += "-$name", "********"
    if ($viaEnv) { [Environment]::SetEnvironmentVariable($setupEnv[$name], $secrets[$name], "Process") }
    else { $installerArgs += "-$name", $secrets[$name] }
}
Write-Host "Handoff : $installer $($shown -join ' ')"
try {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installer @installerArgs
    $code = $LASTEXITCODE
} finally {
    foreach ($var in $setupEnv.Values) { [Environment]::SetEnvironmentVariable($var, $null, "Process") }
}
Remove-Item -Recurse -Force $staging -ErrorAction SilentlyContinue
exit $code
