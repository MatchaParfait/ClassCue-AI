param(
    [Parameter(Mandatory=$true)][string]$ProjectRoot,
    [Parameter(Mandatory=$true)][string]$AppName,
    [string]$Hotkey = ""
)
$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$Script = Join-Path $ProjectRoot "scripts\desktop_launcher.vbs"
$Icon = Join-Path $ProjectRoot "assets\desktop.ico"
if (-not (Test-Path -LiteralPath $Script) -or -not (Test-Path -LiteralPath $Icon)) {
    throw "Desktop launcher or icon is missing."
}
if ($Hotkey -and $Hotkey -notmatch '^CTRL\+(ALT|SHIFT)\+[A-Z0-9]$') {
    throw "Unsupported keyboard shortcut."
}
$Shell = New-Object -ComObject WScript.Shell
$Target = Join-Path $env:WINDIR "System32\wscript.exe"
$Menu = Join-Path $Shell.SpecialFolders.Item("Programs") $AppName
New-Item -ItemType Directory -Force -Path $Menu | Out-Null
$Destinations = @(
    (Join-Path $Shell.SpecialFolders.Item("Desktop") "$AppName.lnk"),
    (Join-Path $Menu "$AppName.lnk")
)
foreach ($Destination in $Destinations) {
    $Shortcut = $Shell.CreateShortcut($Destination)
    if ((Test-Path -LiteralPath $Destination) -and
        ($Shortcut.TargetPath -ne $Target -or $Shortcut.Arguments -ne "`"$Script`"")) {
        throw "An unrelated shortcut already uses this name."
    }
    $Shortcut.TargetPath = $Target
    $Shortcut.Arguments = "`"$Script`""
    $Shortcut.WorkingDirectory = $ProjectRoot
    $Shortcut.IconLocation = "$Icon,0"
    $Shortcut.Description = $AppName
    # One registration avoids duplicate hotkey handlers for the same command.
    $Shortcut.Hotkey = if ($Destination -eq $Destinations[0]) { $Hotkey } else { "" }
    $Shortcut.Save()
}
