# Build the self-contained offline deployment bundle for the NetWorker
# Dashboard (DEDB-style): app + embedded CPython runtime + nssm.exe + the
# installer itself, so the target host needs NOTHING preinstalled.
#
# Produces dist\nwdash-bundle-<version>-win-x64.zip:
#   app\                  launcher + nwdash package (explicit allow-list below)
#   runtime\python\       python.org embeddable CPython (x64), SHA-pinned,
#                         with python312._pth adjusted for the install layout
#                         and 'cryptography' preinstalled in Lib\site-packages
#   runtime\nssm.exe      SHA-pinned service manager
#   deploy\install.ps1    the installer (runs from inside the unpacked bundle)
#   deploy\lib\common.ps1 shared functions
#   VERSION               plain-text version
#
# Usage:  pwsh -File deploy\build-bundle.ps1
#    or:  powershell -ExecutionPolicy Bypass -File deploy\build-bundle.ps1
#   -SkipRuntimeFetch  never touch the network; requires a seeded deploy\.cache
#                      (air-gapped build hosts)
#   -NoCrypto          skip the optional cryptography wheel (DPAPI/AES paths in
#                      nwdash\secrets.py degrade gracefully without it)
#
# Downloads are cached in deploy\.cache\ so rebuilds are offline-safe.
[CmdletBinding()] param(
  [switch]$SkipRuntimeFetch,
  [switch]$NoCrypto,
  [string]$Out
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
. "$Here\lib\common.ps1"

$Repo = Split-Path -Parent $Here
if (-not $Out) { $Out = Join-Path $Repo 'dist' }
$Cache = Join-Path $Here '.cache'

# ---- pinned runtime artifacts ----
# Embeddable CPython 3.12 x64 from python.org. SHA256 computed from the first
# download on 2026-07-19 and pinned here; a later download that hashes
# differently ABORTS the build (supply-chain guard).
$PyUrl = 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip'
$PySha = '4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3'
$PyZipName = Split-Path $PyUrl -Leaf
# nssm 2.24 win64. The cached exe was copied from the DEDB project's verified
# deploy cache (its zip pin: https://nssm.cc/release/nssm-2.24.zip, sha256
# 727d1e42275c605e0f04aba98095c38a8e1e46def453cdffce42869428aa6743); this pin
# is the SHA256 of the win64 nssm.exe itself, computed 2026-07-19.
$NssmUrl = 'https://nssm.cc/release/nssm-2.24.zip'
$NssmZipSha = '727d1e42275c605e0f04aba98095c38a8e1e46def453cdffce42869428aa6743'
$NssmExeSha = 'f689ee9af94b00e9e3f0bb072b34caaf207f32dcb4f5782fc9ca351df9a06c97'

# ---- version from the package ----
$initFile = Join-Path $Repo 'nwdash\config.py'
$versionLine = Select-String -Path $initFile -Pattern '^APP_VERSION\s*=\s*"([^"]+)"' | Select-Object -First 1
if (-not $versionLine) { Stop-Nwdash 'could not read APP_VERSION from nwdash\config.py' }
$Ver = $versionLine.Matches[0].Groups[1].Value
$Name = "nwdash-bundle-$Ver-win-x64"
Write-NwdashLog "building $Name"

# ---- allow-list: every app file that ships. Anything not listed does NOT
# ship. (An explicit list fails loudly when a new module is forgotten, instead
# of silently shipping an incomplete bundle.) ----
$shipFiles = @(
  'networker_dashboard.py',
  'README.md',
  'pyproject.toml',
  'nwdash\__init__.py',
  'nwdash\auth.py',
  'nwdash\certs.py',
  'nwdash\config.py',
  'nwdash\display.py',
  'nwdash\emailer.py',
  'nwdash\main.py',
  'nwdash\models.py',
  'nwdash\nwui.py',
  'nwdash\profiles.py',
  'nwdash\report_api.py',
  'nwdash\report_cred.py',
  'nwdash\report_groups.py',
  'nwdash\report_groups_api.py',
  'nwdash\report_jobs.py',
  'nwdash\report_notify.py',
  'nwdash\report_render.py',
  'nwdash\report_window.py',
  'nwdash\reports.py',
  'nwdash\restapi.py',
  'nwdash\secrets.py',
  'nwdash\server.py',
  'nwdash\sessions.py',
  'nwdash\snapshots.py',
  'nwdash\ui.py',
  'nwdash\wmi_health.py',
  'nwdash\assets\dashboard.html',
  'nwdash\assets\app.css',
  'nwdash\assets\app.js',
  'nwdash\assets\login.html',
  'nwdash\assets\view.html',
  'nwdash\assets\tv.html',
  'nwdash\assets\tv.css',
  'nwdash\assets\tv.js'
)

# Fail fast if the allow-list is stale in either direction.
foreach ($f in $shipFiles) {
  if (-not (Test-Path (Join-Path $Repo $f))) { Stop-Nwdash "allow-listed file missing from repo: $f" }
}
$repoPy = Get-ChildItem -Path (Join-Path $Repo 'nwdash') -Recurse -File |
  Where-Object { $_.Extension -in '.py', '.html', '.css', '.js' } |
  ForEach-Object { $_.FullName.Substring($Repo.Length + 1) }
foreach ($f in $repoPy) {
  if ($shipFiles -notcontains $f) { Stop-Nwdash "repo file not in the bundle allow-list (add it or delete it): $f" }
}

# ---- stage ----
$Stage = Join-Path $env:TEMP ("nwdash-stage-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path "$Stage\app", "$Stage\runtime", "$Stage\deploy\lib", $Cache | Out-Null
try {
  Write-NwdashLog 'staging app (allow-list)'
  foreach ($f in $shipFiles) {
    $dest = Join-Path "$Stage\app" $f
    $destDir = Split-Path -Parent $dest
    if (-not (Test-Path $destDir)) { New-Item -ItemType Directory -Force -Path $destDir | Out-Null }
    Copy-Item -Path (Join-Path $Repo $f) -Destination $dest
  }

  # ---- embedded CPython ----
  $pyZip = Join-Path $Cache $PyZipName
  if (-not (Test-Path $pyZip)) {
    if ($SkipRuntimeFetch) { Stop-Nwdash "-SkipRuntimeFetch was given but $PyZipName is not in deploy\.cache - seed the cache on a connected host first" }
    Write-NwdashLog "fetch python: $PyUrl"
    Invoke-Download $PyUrl $pyZip
  } else { Write-NwdashLog "using cached $PyZipName" }
  Test-Sha256 $pyZip $PySha
  Write-NwdashLog 'extracting embedded python'
  Expand-Archive -Path $pyZip -DestinationPath "$Stage\runtime\python" -Force

  # The embeddable distribution restricts sys.path to EXACTLY the entries in
  # python312._pth (no script dir, no PYTHONPATH). Entries are relative to the
  # python dir (InstallDir\runtime\python), so:
  #   ..\..              -> InstallDir (where networker_dashboard.py + nwdash\
  #                         live after install; the app is stdlib-only)
  #   Lib\site-packages  -> the optional cryptography wheel installed below
  $pth = Join-Path "$Stage\runtime\python" 'python312._pth'
  if (-not (Test-Path $pth)) { Stop-Nwdash 'python312._pth not found in the embeddable zip - layout changed, update build-bundle.ps1' }
  @('python312.zip', '.', 'Lib\site-packages', '..\..') | Set-Content -Path $pth -Encoding ascii

  # ---- optional cryptography wheel (build-time pip, target install) ----
  # Installed with the HOST python into the EMBEDDED runtime's site-packages
  # using binary-only cp312/win_amd64 wheels, so the target needs no pip.
  # Wheels are cached in deploy\.cache\wheels for air-gapped rebuilds.
  if ($NoCrypto) {
    Write-NwdashWarn 'NoCrypto: bundle ships WITHOUT cryptography (encrypted-at-rest paths degrade gracefully)'
  } else {
    $wheelDir = Join-Path $Cache 'wheels'
    $pipPlat = @('--platform', 'win_amd64', '--python-version', '3.12', '--implementation', 'cp', '--only-binary=:all:')
    $haveWheels = (Test-Path $wheelDir) -and @(Get-ChildItem $wheelDir -Filter 'cryptography-*.whl' -ErrorAction SilentlyContinue).Count
    if (-not $haveWheels) {
      if ($SkipRuntimeFetch) { Stop-Nwdash '-SkipRuntimeFetch was given but deploy\.cache\wheels has no cryptography wheel - seed the cache on a connected host, or pass -NoCrypto' }
      New-Item -ItemType Directory -Force $wheelDir | Out-Null
      Write-NwdashLog 'pip download cryptography (cp312/win_amd64) into deploy\.cache\wheels'
      & python -m pip download cryptography @pipPlat -d $wheelDir -q
      if ($LASTEXITCODE) { Stop-Nwdash "pip download cryptography failed (exit $LASTEXITCODE) - is the host python + pip available? Use -NoCrypto to ship without it." }
    }
    Write-NwdashLog 'installing cryptography into the embedded runtime (pip --target, offline from cache)'
    & python -m pip install cryptography --no-index --find-links $wheelDir @pipPlat --target "$Stage\runtime\python\Lib\site-packages" -q
    if ($LASTEXITCODE) { Stop-Nwdash "pip install --target cryptography failed (exit $LASTEXITCODE) - use -NoCrypto to ship without it" }
  }

  # Prove the staged runtime actually works BEFORE shipping it: stdlib modules
  # the app needs, plus cryptography when included ("it didn't crash" is not
  # "it worked" - import under the real embedded interpreter).
  Write-NwdashLog 'validating the embedded runtime'
  $probe = 'import ssl, sqlite3, ctypes, json, socket; print("stdlib-ok")'
  if (-not $NoCrypto) { $probe = 'import ssl, sqlite3, ctypes, json, socket; import cryptography; from cryptography.fernet import Fernet; print("stdlib-ok crypto-ok " + cryptography.__version__)' }
  $proof = & "$Stage\runtime\python\python.exe" -c $probe
  if ($LASTEXITCODE -or -not "$proof".StartsWith('stdlib-ok')) { Stop-Nwdash "embedded runtime validation failed: $proof" }
  Write-NwdashLog "embedded runtime OK: $proof"

  # ---- nssm ----
  $nssmCache = Join-Path $Cache 'nssm.exe'
  if (-not (Test-Path $nssmCache)) {
    if ($SkipRuntimeFetch) { Stop-Nwdash "-SkipRuntimeFetch was given but nssm.exe is not in deploy\.cache" }
    Write-NwdashLog "fetch nssm: $NssmUrl"
    $nssmZip = Join-Path $Cache 'nssm.zip'
    Invoke-Download $NssmUrl $nssmZip
    Test-Sha256 $nssmZip $NssmZipSha
    $nssmTmp = Join-Path $Cache 'nssm-tmp'
    Expand-Archive $nssmZip $nssmTmp -Force
    Copy-Item (Get-ChildItem $nssmTmp -Recurse -Filter nssm.exe | Where-Object FullName -like '*win64*' | Select-Object -First 1).FullName $nssmCache
    Remove-Item -Recurse -Force $nssmTmp, $nssmZip
  } else { Write-NwdashLog 'using cached nssm.exe (deploy\.cache\nssm.exe)' }
  Test-Sha256 $nssmCache $NssmExeSha
  Copy-Item $nssmCache "$Stage\runtime\nssm.exe"

  # ---- deploy scripts live INSIDE the bundle (DEDB/FMD stale-launcher
  # lesson: logic shipped outside the zip goes stale on bundle-only updates) --
  Copy-Item "$Here\install.ps1" "$Stage\deploy\install.ps1"
  Copy-Item "$Here\lib\common.ps1" "$Stage\deploy\lib\common.ps1"
  $Ver | Out-File -Encoding ascii "$Stage\VERSION"

  # ---- zip (forward-slash entry names: both Compress-Archive and .NET
  # Framework's CreateFromDirectory emit backslash entries under Windows
  # PowerShell 5.1, which non-Windows unzip tools reject) ----
  New-Item -ItemType Directory -Force -Path $Out | Out-Null
  $zipPath = Join-Path $Out "$Name.zip"
  if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
  Write-NwdashLog 'packing archive'
  Add-Type -AssemblyName System.IO.Compression
  Add-Type -AssemblyName System.IO.Compression.FileSystem
  $archive = [System.IO.Compression.ZipFile]::Open($zipPath, [System.IO.Compression.ZipArchiveMode]::Create)
  $count = 0
  try {
    $stageFull = (Get-Item $Stage).FullName
    Get-ChildItem -Path $Stage -Recurse -File | Sort-Object FullName | ForEach-Object {
      $entryName = $_.FullName.Substring($stageFull.Length + 1) -replace '\\', '/'
      [void][System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($archive, $_.FullName, $entryName)
      $count++
    }
  } finally {
    $archive.Dispose()
  }
  $zipItem = Get-Item $zipPath
  Write-NwdashLog ("done -> dist\{0}.zip ({1:N0} bytes, {2} files)" -f $Name, $zipItem.Length, $count)
} finally {
  Remove-Item -Recurse -Force $Stage -ErrorAction SilentlyContinue
}
