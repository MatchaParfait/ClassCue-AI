param([switch]$WebUI, [switch]$Desktop, [switch]$PrepareOnly)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RuntimeRoot = Join-Path $ProjectRoot ".runtime"
$UvBin = Join-Path $RuntimeRoot "bin"
$UvExe = Join-Path $UvBin "uv.exe"
$RuntimeVenv = Join-Path $RuntimeRoot "venv"
$RuntimePython = Join-Path $RuntimeVenv "Scripts\python.exe"

New-Item -ItemType Directory -Force -Path $RuntimeRoot, $UvBin | Out-Null

if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot "uv.lock"))) {
    throw "uv.lock is missing. Download a complete project release and try again."
}

$env:UV_INSTALL_DIR = $UvBin
$env:UV_NO_MODIFY_PATH = "1"
$env:UV_PROJECT_ENVIRONMENT = $RuntimeVenv
$env:UV_PYTHON_INSTALL_DIR = Join-Path $RuntimeRoot "python"
$env:UV_CACHE_DIR = Join-Path $RuntimeRoot "cache"
$env:PYTHONUTF8 = "1"

if (-not (Test-Path -LiteralPath $UvExe)) {
    Write-Host "First launch: downloading the portable uv runtime..." -ForegroundColor Cyan
    try {
        $Installer = Invoke-RestMethod -Uri "https://astral.sh/uv/install.ps1" -TimeoutSec 60
        Invoke-Expression $Installer
    }
    catch {
        throw "Could not download uv. Check the network and launch again. Error: $($_.Exception.Message)"
    }
}

if (-not (Test-Path -LiteralPath $UvExe)) {
    throw "uv finished downloading, but the executable was not found at $UvExe"
}

Write-Host "Preparing the private Python runtime and locked dependencies..." -ForegroundColor Cyan
if ($Desktop) {
    & $UvExe sync --project $ProjectRoot --frozen --no-dev --extra desktop --managed-python --python 3.12
} else {
    & $UvExe sync --project $ProjectRoot --frozen --no-dev --managed-python --python 3.12
}
if ($LASTEXITCODE -ne 0) {
    throw "Python setup failed. Check access to the Python and PyPI download sources."
}

if (-not (Test-Path -LiteralPath $RuntimePython)) {
    throw "Setup completed, but the private Python executable is missing: $RuntimePython"
}

if ($WebUI -or $Desktop) {
    Write-Host "Checking the bundled GTCRN playback model..." -ForegroundColor Cyan
    & $RuntimePython (Join-Path $PSScriptRoot "prepare_gtcrn.py")
    if ($LASTEXITCODE -ne 0) {
        throw "Bundled GTCRN resources are missing or damaged. Download a complete project copy and retry."
    }
}

if ($PrepareOnly) { exit 0 }
Write-Host "Starting Classroom Video Study..." -ForegroundColor Cyan
$Launcher = if ($WebUI) { "start_frontend.py" } else { "start_app.py" }
& $RuntimePython (Join-Path $PSScriptRoot $Launcher)
if ($LASTEXITCODE -ne 0) {
    throw "The app failed to start. Review the launcher message and service logs."
}
