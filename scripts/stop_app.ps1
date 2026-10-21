$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$StatePath = Join-Path $ProjectRoot ".runtime\app_state.json"

if (-not (Test-Path -LiteralPath $StatePath)) {
    Write-Host "Classroom Video Study is not running."
    exit 0
}

try {
    $State = Get-Content -LiteralPath $StatePath -Raw -Encoding UTF8 | ConvertFrom-Json
    $ProcessId = [int]$State.pid
    $Process = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
    $ExpectedApp = Join-Path $ProjectRoot "video_app.py"
    if ($null -ne $Process -and $null -ne $Process.CommandLine -and $Process.CommandLine.Contains("streamlit") -and $Process.CommandLine.Contains($ExpectedApp)) {
        & taskkill.exe /PID $ProcessId /T /F | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Could not stop the Streamlit process tree (PID $ProcessId)."
        }
        Write-Host "Classroom Video Study has stopped."
    }
    else {
        Write-Host "The saved app process was not found. The stale state was removed."
    }
}
finally {
    Remove-Item -LiteralPath $StatePath -Force -ErrorAction SilentlyContinue
}
