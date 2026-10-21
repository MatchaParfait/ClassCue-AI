$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location -LiteralPath $ProjectRoot
$env:PYTHONUTF8 = "1"
$Python = @(
    (Join-Path $ProjectRoot ".venv\Scripts\python.exe"),
    (Join-Path $ProjectRoot ".runtime\venv\Scripts\python.exe")
) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
$Ready = $false
if ($Python) {
    & $Python -c "import importlib.util,sys; sys.exit(0 if all(importlib.util.find_spec(name) for name in ('webview','pystray','PIL','cryptography')) else 1)"
    $Ready = $LASTEXITCODE -eq 0
}
if (-not $Ready) {
    & (Join-Path $PSScriptRoot "bootstrap.ps1") -Desktop -PrepareOnly
    if ($LASTEXITCODE -ne 0) { throw "Desktop Python setup failed." }
    $Python = Join-Path $ProjectRoot ".runtime\venv\Scripts\python.exe"
}
$Journal = Join-Path $ProjectRoot ".runtime\updates\transaction.json"
$Recovery = $null
if (Test-Path -LiteralPath $Journal) {
    $Transaction = Get-Content -LiteralPath $Journal -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($Transaction.status -eq "replacing") {
        if ($Transaction.id -notmatch '^[a-f0-9]{32}$') { throw "Invalid update recovery identity." }
        $Recovery = Join-Path $ProjectRoot ".runtime\updates\helper\$($Transaction.id)\worker.py"
        if (-not (Test-Path -LiteralPath $Recovery)) { throw "The trusted recovery helper is missing. See docs/releases.md." }
    }
}
if ($Recovery) {
    & $Python $Recovery --root $ProjectRoot --recover
} else {
    & $Python -c "from app_update_install import recover_at_launch; from pathlib import Path; recover_at_launch(Path.cwd())"
}
if ($LASTEXITCODE -ne 0) { throw "Update recovery could not finish. See .runtime/updates/backups and docs/releases.md." }
Write-Host "Checking the bundled GTCRN playback model..." -ForegroundColor Cyan
& $Python (Join-Path $PSScriptRoot "prepare_gtcrn.py")
if ($LASTEXITCODE -ne 0) {
    throw "Bundled GTCRN resources are missing or damaged. Download a complete project copy and retry."
}
$Frontend = Join-Path $ProjectRoot "frontend"
$Index = Join-Path $Frontend "dist\index.html"
if (Test-Path -LiteralPath (Join-Path $ProjectRoot ".release-install.json")) {
    if (-not (Test-Path -LiteralPath $Index)) { throw "The release is incomplete: frontend/dist/index.html is missing." }
} else {
$Source = @(
    Get-ChildItem -LiteralPath (Join-Path $Frontend "src"), (Join-Path $Frontend "public") -File -Recurse
    Get-Item -LiteralPath (Join-Path $Frontend "package-lock.json"), (Join-Path $Frontend "vite.config.ts"), (Join-Path $Frontend "index.html")
    Get-ChildItem -LiteralPath (Join-Path $ProjectRoot "experiments\subtitle-cyberspace\src") -File
)
$Latest = ($Source | Measure-Object -Property LastWriteTimeUtc -Maximum).Maximum
if (-not (Test-Path -LiteralPath $Index) -or (Get-Item -LiteralPath $Index).LastWriteTimeUtc -lt $Latest) {
    if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) {
        throw "The first frontend build requires Node.js. Install Node.js and retry."
    }
    Push-Location -LiteralPath $Frontend
    try {
        $Marker = Join-Path $ProjectRoot ".runtime\desktop_build_lock.txt"
        $Hash = (Get-FileHash -LiteralPath "package-lock.json" -Algorithm SHA256).Hash
        if (-not (Test-Path -LiteralPath "node_modules") -or
            ((Test-Path -LiteralPath $Marker) -and (Get-Content -LiteralPath $Marker -Raw).Trim() -ne $Hash)) {
            & npm.cmd ci
            if ($LASTEXITCODE -ne 0) { throw "Frontend dependencies could not be installed." }
        }
        & npm.cmd run build
        if ($LASTEXITCODE -ne 0) { throw "Frontend build failed." }
        Set-Content -LiteralPath $Marker -Value $Hash -Encoding ASCII
    } finally { Pop-Location }
}
}
$Pythonw = Join-Path (Split-Path $Python -Parent) "pythonw.exe"
if (-not (Test-Path -LiteralPath $Pythonw)) { throw "The windowed Python executable is missing." }
@{ pythonw = $Pythonw } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $ProjectRoot ".runtime\desktop_ready.json") -Encoding UTF8
