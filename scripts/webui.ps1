param(
    [ValidateSet("start", "stop", "restart")]
    [string]$Action = "start"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$env:PYTHONUTF8 = "1"

$Python = @(
    (Join-Path $ProjectRoot ".venv\Scripts\python.exe"),
    (Join-Path $ProjectRoot ".runtime\venv\Scripts\python.exe")
) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1

if (-not $Python) {
    if ($Action -eq "stop") {
        if (Test-Path -LiteralPath (Join-Path $ProjectRoot ".runtime\frontend_state.json")) {
            throw "The saved app runtime is missing. Restore its Python environment before stopping."
        }
        Write-Host "Frontend and API are not running."
        exit 0
    }
    & (Join-Path $PSScriptRoot "bootstrap.ps1") -WebUI
    exit $LASTEXITCODE
}

& $Python (Join-Path $PSScriptRoot "start_frontend.py") $Action
exit $LASTEXITCODE
