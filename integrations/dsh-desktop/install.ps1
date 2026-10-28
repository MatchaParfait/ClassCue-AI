param(
    [Parameter(Mandatory = $true)][string]$DesktopExecutable,
    [Parameter(Mandatory = $true)][string]$HarnessHome
)

$ErrorActionPreference = "Stop"
$bridgeExecutable = (Resolve-Path -LiteralPath $DesktopExecutable).Path
$bridgeHome = [IO.Path]::GetFullPath($HarnessHome)
$bridgePackage = $PSScriptRoot
$bridgeCli = Join-Path (Split-Path $bridgeExecutable) "resources\runtime\cli\bin\dsh.cmd"
if (-not (Test-Path -LiteralPath $bridgeCli -PathType Leaf)) {
    throw "Missing desktop bundled CLI."
}
$bridgeVersion = (Get-Item -LiteralPath $bridgeExecutable).VersionInfo.ProductVersion
if ($bridgeVersion -ne "0.2.1.0") {
    throw "This bridge requires desktop 0.2.1-rc.3 (ProductVersion 0.2.1.0)."
}
$bridgeLock = Join-Path $bridgeHome "profiles\desktop\lock"
$bridgeProfile = Join-Path $bridgeHome "profiles\desktop"
$bridgeRunning = Get-CimInstance Win32_Process | Where-Object {
    $_.ExecutablePath -eq $bridgeExecutable -and $_.CommandLine -and
    $_.CommandLine.IndexOf($bridgeProfile, [StringComparison]::OrdinalIgnoreCase) -ge 0
}
if ($bridgeRunning) {
    throw "The selected desktop profile is running. Close it before installing; no process was stopped."
}
if (Test-Path -LiteralPath $bridgeLock -PathType Leaf) {
    $bridgeOwner = Get-Content -LiteralPath $bridgeLock -Raw
    $bridgeOwnerId = 0
    if (-not [int]::TryParse($bridgeOwner.Trim(), [ref]$bridgeOwnerId) -or
        (Get-Process -Id $bridgeOwnerId -ErrorAction SilentlyContinue)) {
        throw "The selected desktop profile may be running. Close that profile before installing; no process was stopped."
    }
}
$bridgePreviousHome = $env:DSH_HOME
try {
    $env:DSH_HOME = $bridgeHome
    & $bridgeCli plugin --profile desktop add --ignore-scripts --config.auto-install-peers=false --config.strict-peer-dependencies=false "file:$bridgePackage"
    if ($LASTEXITCODE -ne 0) { throw "Desktop plugin installation failed." }
    Write-Host "Installed. Start the selected desktop profile and connect ClassCue to classcue-bridge\connection.json under the selected Harness home."
} finally {
    if ($null -eq $bridgePreviousHome) { Remove-Item Env:DSH_HOME -ErrorAction SilentlyContinue }
    else { $env:DSH_HOME = $bridgePreviousHome }
}
