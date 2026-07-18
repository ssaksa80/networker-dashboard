# Build an offline deployment bundle for the NetWorker Dashboard.
#
# Produces dist\networker-dashboard-<version>-bundle.zip containing only the
# runtime files (explicit allow-list below). Runtime state (data\, logs\,
# .certs\) is created by the app on first start and is never bundled.
#
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\build-bundle.ps1
#    or:  pwsh -File scripts\build-bundle.ps1
# Verified under both Windows PowerShell 5.1 and PowerShell 7. The target
# machine needs Python 3.11+; the optional 'cryptography' package enables
# encrypted credential storage but is not required.

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

# --- Version from the package ---------------------------------------------
$initFile = Join-Path $repoRoot "nwdash\config.py"
$versionLine = Select-String -Path $initFile -Pattern '^APP_VERSION\s*=\s*"([^"]+)"' | Select-Object -First 1
if (-not $versionLine) {
    throw "Could not read APP_VERSION from nwdash\config.py"
}
$version = $versionLine.Matches[0].Groups[1].Value
Write-Host "Bundling version $version"

# --- Allow-list: every file that ships. Anything not listed does NOT ship. --
# (An explicit list fails loudly when a new module is forgotten, instead of
# silently shipping an incomplete bundle.)
$shipDirs = @(
    "nwdash",
    "nwdash\assets"
)
$shipFiles = @(
    "networker_dashboard.py",
    "README.md",
    "pyproject.toml",
    "nwdash\__init__.py",
    "nwdash\auth.py",
    "nwdash\certs.py",
    "nwdash\config.py",
    "nwdash\emailer.py",
    "nwdash\main.py",
    "nwdash\models.py",
    "nwdash\nwui.py",
    "nwdash\profiles.py",
    "nwdash\reports.py",
    "nwdash\restapi.py",
    "nwdash\secrets.py",
    "nwdash\server.py",
    "nwdash\sessions.py",
    "nwdash\snapshots.py",
    "nwdash\ui.py",
    "nwdash\wmi_health.py",
    "nwdash\assets\dashboard.html",
    "nwdash\assets\app.css",
    "nwdash\assets\app.js",
    "nwdash\assets\login.html",
    "nwdash\assets\view.html",
    "nwdash\assets\tv.html",
    "nwdash\assets\tv.css",
    "nwdash\assets\tv.js"
)

# Fail fast if the allow-list is stale in either direction.
foreach ($f in $shipFiles) {
    if (-not (Test-Path (Join-Path $repoRoot $f))) {
        throw "Allow-listed file missing from repo: $f"
    }
}
$repoPy = Get-ChildItem -Path (Join-Path $repoRoot "nwdash") -Recurse -File |
    Where-Object { $_.Extension -in ".py", ".html", ".css", ".js" } |
    ForEach-Object { $_.FullName.Substring($repoRoot.Length + 1) }
foreach ($f in $repoPy) {
    if ($shipFiles -notcontains $f) {
        throw "Repo file not in the bundle allow-list (add it or delete it): $f"
    }
}

# --- Stage and zip ----------------------------------------------------------
$distDir = Join-Path $repoRoot "dist"
$stage = Join-Path $env:TEMP ("nwdash-bundle-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $distDir | Out-Null
New-Item -ItemType Directory -Force -Path $stage | Out-Null
try {
    foreach ($d in $shipDirs) {
        New-Item -ItemType Directory -Force -Path (Join-Path $stage $d) | Out-Null
    }
    foreach ($f in $shipFiles) {
        Copy-Item -Path (Join-Path $repoRoot $f) -Destination (Join-Path $stage $f)
    }

    $zipName = "networker-dashboard-$version-bundle.zip"
    $zipPath = Join-Path $distDir $zipName
    if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
    # Write entries by hand with forward-slash names: both Compress-Archive
    # and .NET Framework's ZipFile::CreateFromDirectory emit backslash entry
    # names under Windows PowerShell 5.1, which non-Windows unzip tools reject.
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::Open($zipPath, [System.IO.Compression.ZipArchiveMode]::Create)
    try {
        foreach ($f in $shipFiles) {
            $entryName = $f -replace "\\", "/"
            [void][System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
                $archive, (Join-Path $stage $f), $entryName)
        }
    }
    finally {
        $archive.Dispose()
    }

    $zipItem = Get-Item $zipPath
    Write-Host ("Bundle written: dist\{0} ({1:N0} bytes, {2} files)" -f $zipName, $zipItem.Length, $shipFiles.Count)
}
finally {
    Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
}
