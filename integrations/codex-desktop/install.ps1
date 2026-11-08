#requires -Version 7.0
param(
    [Parameter(Mandatory = $true)][string]$WorkspaceRoot,
    [string]$Destination,
    [string]$MarketplaceRoot = $env:USERPROFILE,
    [string]$NodeExecutable,
    [string]$NpmExecutable,
    [string]$CodexHome = $(if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE ".codex" })
)

$ErrorActionPreference = "Stop"
if (-not $Destination) {
    $pluginVersion = (Get-Content -LiteralPath (Join-Path $PSScriptRoot ".codex-plugin\plugin.json") -Raw |
        ConvertFrom-Json).version
    $Destination = Join-Path $CodexHome "plugins\classcue-$pluginVersion"
}
$workspacePath = (Resolve-Path -LiteralPath $WorkspaceRoot).Path
if (-not (Test-Path -LiteralPath $workspacePath -PathType Container)) { throw "Workspace must be a directory." }
$pluginDestination = [IO.Path]::GetFullPath($Destination)
$existingLockPath = Join-Path $pluginDestination "package-lock.json"
$existingLockHash = if (Test-Path -LiteralPath $existingLockPath) {
    (Get-FileHash -LiteralPath $existingLockPath -Algorithm SHA256).Hash
} else { "" }
$marketplaceRoot = [IO.Path]::GetFullPath($MarketplaceRoot)
$relativePlugin = [IO.Path]::GetRelativePath($marketplaceRoot, $pluginDestination).Replace("\", "/")
if ($relativePlugin.StartsWith("../") -or [IO.Path]::IsPathRooted($relativePlugin)) {
    throw "Plugin destination must be inside the current user directory."
}
if (-not $NodeExecutable) {
    $NodeExecutable = if ($env:CODEX_MCP_NODE_PATH) { $env:CODEX_MCP_NODE_PATH } else { (Get-Command node -ErrorAction Stop).Source }
}
if (-not $NpmExecutable) { $NpmExecutable = (Get-Command npm.cmd -ErrorAction Stop).Source }
$nodePath = (Resolve-Path -LiteralPath $NodeExecutable).Path
& $nodePath -e 'if (Number(process.versions.node.split(".")[0]) < 24) process.exit(1); require("node:sqlite");'
if ($LASTEXITCODE -ne 0) { throw "ClassCue requires Node.js 24 or newer with node:sqlite." }
$manifest = Join-Path $pluginDestination ".codex-plugin\plugin.json"
$portableManifest = Join-Path $pluginDestination "plugin.json"
if (Test-Path -LiteralPath $pluginDestination) {
    $existingManifests = @(@($manifest, $portableManifest) | Where-Object { Test-Path -LiteralPath $_ })
    if ($existingManifests.Count -eq 0 -or
        @($existingManifests | Where-Object {
            (Get-Content -LiteralPath $_ -Raw | ConvertFrom-Json).name -ne "classcue"
        }).Count -gt 0) {
        throw "Destination already exists and is not the ClassCue plugin."
    }
    # Older dual-manifest packages hide the MCP service in the current desktop host.
    if (Test-Path -LiteralPath $portableManifest) {
        Remove-Item -LiteralPath $portableManifest
        $portableMcp = Join-Path $pluginDestination "mcp.json"
        if (Test-Path -LiteralPath $portableMcp) { Remove-Item -LiteralPath $portableMcp }
    }
}
New-Item -ItemType Directory -Path $pluginDestination -Force | Out-Null
$sourcePaths = @("package.json", "package-lock.json", "server.mjs", "context.mjs",
    "connection.mjs", "drafts.mjs", "draft-controller.mjs", "app.mjs", "resource.mjs", "view.html", "icon.png", ".codex-plugin", "skills")
foreach ($name in $sourcePaths) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination $pluginDestination -Recurse -Force
}
$connectionDirectory = Join-Path ([IO.Path]::GetFullPath($CodexHome)) "classcue"
New-Item -ItemType Directory -Path $connectionDirectory -Force | Out-Null
$connectionPath = Join-Path $connectionDirectory "connection.json"
$connection = if (Test-Path -LiteralPath $connectionPath) {
    Get-Content -LiteralPath $connectionPath -Raw | ConvertFrom-Json -AsHashtable
} else { @{ format = "classcue-codex-connection"; version = 1; roots = @() } }
if ($connection.format -ne "classcue-codex-connection" -or $connection.version -ne 1) {
    throw "The existing ClassCue connection file has an unsupported format."
}
$connection.roots = @($workspacePath) + @($connection.roots | Where-Object { $_ -ne $workspacePath })
if ($connection.roots.Count -gt 32) { throw "Too many registered ClassCue workspaces." }
$temporaryConnection = "$connectionPath.$([Guid]::NewGuid().ToString('N')).tmp"
try {
    $connection | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $temporaryConnection -Encoding utf8
    Move-Item -LiteralPath $temporaryConnection -Destination $connectionPath -Force
} finally {
    if (Test-Path -LiteralPath $temporaryConnection) { Remove-Item -LiteralPath $temporaryConnection }
}
$mcp = @{
    mcpServers = @{
        classcue = @{
            command = $nodePath
            args = @((Join-Path $pluginDestination "server.mjs"))
            env = @{ CLASSCUE_CODEX_ROOT = $workspacePath; CLASSCUE_CODEX_CONNECTION_FILE = $connectionPath }
        }
    }
}
$mcp | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $pluginDestination ".mcp.json") -Encoding utf8
Push-Location $pluginDestination
try {
    $sourceLockHash = (Get-FileHash -LiteralPath (Join-Path $PSScriptRoot "package-lock.json") -Algorithm SHA256).Hash
    $dependenciesReady = $false
    if ($existingLockHash -eq $sourceLockHash) {
        & $nodePath --input-type=module -e 'import("./resource.mjs").then(m => m.discussionResource()).catch(() => process.exit(1))'
        $dependenciesReady = $LASTEXITCODE -eq 0
    }
    if (-not $dependenciesReady) {
        & $NpmExecutable ci --omit=dev --ignore-scripts --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { throw "Could not install the bundled MCP dependencies." }
    }
} finally { Pop-Location }
$marketplaceDirectory = Join-Path $marketplaceRoot ".agents\plugins"
New-Item -ItemType Directory -Path $marketplaceDirectory -Force | Out-Null
$catalogPath = Join-Path $marketplaceDirectory "marketplace.json"
$catalog = if (Test-Path -LiteralPath $catalogPath) {
    Get-Content -LiteralPath $catalogPath -Raw | ConvertFrom-Json -AsHashtable
} else { @{ name = "personal"; plugins = @() } }
$others = @($catalog.plugins | Where-Object { $_.name -ne "classcue" })
$catalog.plugins = $others + @(@{
    name = "classcue"
    source = @{ source = "local"; path = "./$relativePlugin" }
    policy = @{ installation = "AVAILABLE"; authentication = "ON_INSTALL" }
    category = "Productivity"
})
$catalog | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $catalogPath -Encoding utf8
Write-Host "ClassCue plugin prepared. Restart Codex, open Plugins, select the personal source and install ClassCue."
