Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Hasher = [Security.Cryptography.SHA256]::Create()
$Hash = [BitConverter]::ToString($Hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($ProjectRoot.ToLowerInvariant()))).Replace("-", "")
$Hasher.Dispose()
$Mutex = New-Object Threading.Mutex($false, "Local\ClassroomBootstrap-$($Hash.Substring(0,12))")
try { $OwnsBootstrap = $Mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $OwnsBootstrap = $true }
if (-not $OwnsBootstrap) { $Mutex.Dispose(); exit 0 }
$Runtime = Join-Path $ProjectRoot ".runtime"
New-Item -ItemType Directory -Force -Path $Runtime | Out-Null

$Scene = $null
$Artwork = $null
$script:ArtworkSurface = $null
$script:ArtworkSurfaceError = $false
$script:StartupFailed = $false
try {
    $ArtRoot = Join-Path $ProjectRoot "assets\startup"
    $Manifest = Get-Content -LiteralPath (Join-Path $ArtRoot "scenes.json") -Raw -Encoding UTF8 | ConvertFrom-Json
    $Candidates = @($Manifest.scenes | Where-Object {
        $_.id -cmatch '^[a-z0-9][a-z0-9-]{0,63}$' -and
        $_.file -cmatch '^[a-z0-9][a-z0-9-]{0,63}\.jpg$' -and
        $_.background -match '^#[0-9a-fA-F]{6}$' -and
        (Test-Path -LiteralPath (Join-Path $ArtRoot $_.file))
    })
    $Previous = $null
    try { $Previous = Get-Content -LiteralPath (Join-Path $Runtime "startup-scene.json") -Raw -Encoding UTF8 | ConvertFrom-Json } catch {}
    while ($Candidates.Count -gt 0 -and -not $Artwork) {
        $Alternatives = @($Candidates | Where-Object { $_.id -ne $Previous.id })
        $Pool = if ($Alternatives.Count -gt 0) { $Alternatives } else { $Candidates }
        $Candidate = $Pool | Get-Random
        $Candidates = @($Candidates | Where-Object { $_.id -ne $Candidate.id })
        try {
            $ImagePath = Join-Path $ArtRoot $Candidate.file
            if ((Get-Item -LiteralPath $ImagePath).Length -gt 1048576) { continue }
            $SourceArtwork = [Drawing.Image]::FromFile($ImagePath)
            try { $Artwork = New-Object Drawing.Bitmap($SourceArtwork) } finally { $SourceArtwork.Dispose() }
            $Scene = $Candidate
            $env:CLASSROOM_STARTUP_SCENE = $Scene.id
        } catch {}
    }
} catch {}

$Form = New-Object Windows.Forms.Form
$Form.Text = "课堂重点复习"
$Form.Size = New-Object Drawing.Size(1280, 850)
$Form.MinimumSize = New-Object Drawing.Size(800, 600)
$Form.StartPosition = "CenterScreen"
$Form.BackColor = [Drawing.ColorTranslator]::FromHtml("#eef6f8")
$Form.Icon = New-Object Drawing.Icon((Join-Path $ProjectRoot "assets\desktop.ico"))
if ($Artwork) {
    $Form.BackgroundImageLayout = "None"
    $Form.BackColor = [Drawing.ColorTranslator]::FromHtml($Scene.background)
}
$Brand = New-Object Windows.Forms.Label
$Brand.AutoSize = $true
$Brand.BackColor = [Drawing.Color]::Transparent
$Brand.ForeColor = [Drawing.ColorTranslator]::FromHtml("#192b24")
$Brand.Font = New-Object Drawing.Font("Segoe UI", 20, [Drawing.FontStyle]::Bold)
$Brand.Text = "ClassCue AI"
$Form.Controls.Add($Brand)
$Subtitle = New-Object Windows.Forms.Label
$Subtitle.AutoSize = $true
$Subtitle.BackColor = [Drawing.Color]::Transparent
$Subtitle.ForeColor = [Drawing.ColorTranslator]::FromHtml("#52655a")
$Subtitle.Font = New-Object Drawing.Font("Microsoft YaHei UI", 10)
$Subtitle.Text = "课堂重点复习"
$Form.Controls.Add($Subtitle)
$Caption = New-Object Windows.Forms.Label
$Caption.BackColor = [Drawing.Color]::Transparent
$Caption.ForeColor = $Brand.ForeColor
$Caption.Font = New-Object Drawing.Font("Microsoft YaHei UI", 15, [Drawing.FontStyle]::Bold)
$Caption.Text = if ($Scene) { $Scene.caption } else { "给今天的学习，留一点好心情。" }
$Form.Controls.Add($Caption)
$Label = New-Object Windows.Forms.Label
$Label.BackColor = [Drawing.Color]::Transparent
$Label.ForeColor = $Subtitle.ForeColor
$Label.Font = New-Object Drawing.Font("Microsoft YaHei UI", 10)
$Label.Text = "正在准备桌面环境…"
$Form.Controls.Add($Label)
$Retry = New-Object Windows.Forms.Button
$Retry.Text = "重试启动"
$Retry.Size = New-Object Drawing.Size(100, 38)
$Retry.Visible = $false
$Form.Controls.Add($Retry)
$Logs = New-Object Windows.Forms.Button
$Logs.Text = "查看日志"
$Logs.Size = New-Object Drawing.Size(100, 38)
$Logs.Visible = $false
$Form.Controls.Add($Logs)
$Logs.Add_Click({ Start-Process notepad.exe -ArgumentList "`"$(Join-Path $Runtime 'desktop-prepare-error.log')`"" })

function Update-Layout {
    $Edge = if ($Form.ClientSize.Width -le 760) { 24 } else { 32 }
    $Width = [int][Math]::Min(960, $Form.ClientSize.Width - 2 * $Edge)
    $CaptionSize = [Windows.Forms.TextRenderer]::MeasureText($Caption.Text, $Caption.Font,
        (New-Object Drawing.Size($Width, 0)), [Windows.Forms.TextFormatFlags]::WordBreak)
    $StatusSize = [Windows.Forms.TextRenderer]::MeasureText($Label.Text, $Label.Font,
        (New-Object Drawing.Size($Width, 0)), [Windows.Forms.TextFormatFlags]::WordBreak)
    $StatusHeight = [int][Math]::Max(24, [Math]::Min(84, $StatusSize.Height + 4))
    $ActionHeight = if ($script:StartupFailed) { 52 } else { 0 }
    $Top = $Form.ClientSize.Height - 24 - $ActionHeight - $StatusHeight - 8 - $CaptionSize.Height
    $StatusTop = $Top + $CaptionSize.Height + 8
    $Brand.Location = New-Object Drawing.Point($Edge, 20)
    $Subtitle.Location = New-Object Drawing.Point(($Edge + $Brand.PreferredWidth + 14), 30)
    $Caption.Location = New-Object Drawing.Point($Edge, $Top)
    $Caption.Size = New-Object Drawing.Size($Width, $CaptionSize.Height)
    $Label.Location = New-Object Drawing.Point($Edge, $StatusTop)
    $Label.Size = New-Object Drawing.Size($Width, $StatusHeight)
    $Retry.Location = New-Object Drawing.Point($Edge, ($StatusTop + $StatusHeight + 14))
    $Logs.Location = New-Object Drawing.Point(($Edge + 110), ($StatusTop + $StatusHeight + 14))
    $SurfaceWidth = $Form.ClientSize.Width
    $SurfaceHeight = $Form.ClientSize.Height
    if ($Artwork -and $SurfaceWidth -gt 0 -and $SurfaceHeight -gt 0 -and
        (-not $script:ArtworkSurface -or $script:ArtworkSurface.Width -ne $SurfaceWidth -or
            $script:ArtworkSurface.Height -ne $SurfaceHeight -or
            $script:ArtworkSurfaceError -ne $script:StartupFailed)) {
        # Match the WebView's centered object-fit: cover without distorting the image.
        $Surface = New-Object Drawing.Bitmap($SurfaceWidth, $SurfaceHeight)
        $Canvas = [Drawing.Graphics]::FromImage($Surface)
        try {
            $Canvas.InterpolationMode = [Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
            $Scale = [Math]::Max($SurfaceWidth / $Artwork.Width, $SurfaceHeight / $Artwork.Height)
            $DrawWidth = [int][Math]::Ceiling($Artwork.Width * $Scale)
            $DrawHeight = [int][Math]::Ceiling($Artwork.Height * $Scale)
            $Canvas.DrawImage($Artwork, [int](($SurfaceWidth - $DrawWidth) / 2),
                [int](($SurfaceHeight - $DrawHeight) / 2), $DrawWidth, $DrawHeight)
            $HeaderArea = New-Object Drawing.Rectangle(0, 0, $SurfaceWidth, 120)
            $FooterHeight = if ($script:StartupFailed) { 240 } else { 160 }
            $FooterArea = New-Object Drawing.Rectangle(0, ($SurfaceHeight - $FooterHeight), $SurfaceWidth, $FooterHeight)
            $HeaderBrush = New-Object Drawing.Drawing2D.LinearGradientBrush($HeaderArea,
                [Drawing.Color]::FromArgb(240, 255, 255, 255), [Drawing.Color]::FromArgb(0, 255, 255, 255),
                [Drawing.Drawing2D.LinearGradientMode]::Vertical)
            $FooterBrush = New-Object Drawing.Drawing2D.LinearGradientBrush($FooterArea,
                [Drawing.Color]::FromArgb(0, 255, 255, 255), [Drawing.Color]::FromArgb(245, 255, 255, 255),
                [Drawing.Drawing2D.LinearGradientMode]::Vertical)
            try {
                $Canvas.FillRectangle($HeaderBrush, $HeaderArea)
                $Canvas.FillRectangle($FooterBrush, $FooterArea)
            } finally { $HeaderBrush.Dispose(); $FooterBrush.Dispose() }
        } finally { $Canvas.Dispose() }
        $PreviousSurface = $script:ArtworkSurface
        $script:ArtworkSurface = $Surface
        $script:ArtworkSurfaceError = $script:StartupFailed
        $Form.BackgroundImage = $Surface
        if ($PreviousSurface) { $PreviousSurface.Dispose() }
    }
}
$Form.Add_Resize({ Update-Layout })
Update-Layout
$Timer = New-Object Windows.Forms.Timer
$Timer.Interval = 250
$script:Preparation = $null
$script:Desktop = $null
$script:Launching = $false

function Show-Failure([string]$Message) {
    $Timer.Stop()
    $script:Launching = $false
    $script:StartupFailed = $true
    $Label.Text = $Message
    $Label.ForeColor = [Drawing.ColorTranslator]::FromHtml("#9f3636")
    $Retry.Visible = $true
    $Logs.Visible = $true
    Update-Layout
}

function Begin-Preparation {
    $script:StartupFailed = $false
    $Retry.Visible = $false
    $Logs.Visible = $false
    $Label.ForeColor = $Subtitle.ForeColor
    $Label.Text = "正在检查桌面环境和页面…"
    Update-Layout
    try {
        $script:Preparation = Start-Process powershell.exe -WindowStyle Hidden -PassThru -ArgumentList @(
            "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", "`"$(Join-Path $PSScriptRoot 'prepare_desktop.ps1')`""
        ) -RedirectStandardOutput (Join-Path $Runtime "desktop-prepare.log") -RedirectStandardError (Join-Path $Runtime "desktop-prepare-error.log")
        # Open the handle before a fast preparation process can exit.
        $null = $script:Preparation.Handle
        $Timer.Start()
    } catch {
        Show-Failure "桌面准备未能开始，请查看日志后重试。"
    }
}

function Test-DesktopWindow {
    $Response = $null
    $Reader = $null
    try {
        $State = Get-Content -LiteralPath (Join-Path $Runtime "desktop_instance.json") -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($State.service -ne "classroom-desktop" -or $State.port -lt 1 -or $State.port -gt 65535) { return $false }
        $Request = [Net.HttpWebRequest]::Create("http://127.0.0.1:$($State.port)/command")
        $Request.Proxy = $null
        $Request.Timeout = 500
        $Request.ReadWriteTimeout = 500
        $Request.Method = "POST"
        $Request.ContentType = "application/json"
        $Request.Headers["Authorization"] = "Bearer $($State.token)"
        $Payload = [Text.Encoding]::UTF8.GetBytes('{"action":"status"}')
        $Request.ContentLength = $Payload.Length
        $Stream = $Request.GetRequestStream()
        try { $Stream.Write($Payload, 0, $Payload.Length) } finally { $Stream.Dispose() }
        $Response = $Request.GetResponse()
        $Reader = New-Object IO.StreamReader($Response.GetResponseStream())
        $Status = $Reader.ReadToEnd() | ConvertFrom-Json
        return [bool]$Status.window_ready
    } catch {
        return $false
    } finally {
        if ($Reader) { $Reader.Dispose() }
        if ($Response) { $Response.Dispose() }
    }
}

$Retry.Add_Click({ Begin-Preparation })
$Form.Add_Shown({ Begin-Preparation })
$Form.Add_FormClosing({
    param($Sender, $EventArgs)
    if ($script:Preparation -and -not $script:Preparation.HasExited) {
        $EventArgs.Cancel = $true
        $Label.Text = "正在准备应用，请等待本次准备结束后再关闭。"
    }
})
$Timer.Add_Tick({
    if ($script:Launching) {
        if (Test-DesktopWindow) {
            $Timer.Stop()
            $Form.Close()
        } elseif ($script:Desktop.HasExited -or [DateTime]::UtcNow -gt $script:WindowDeadline) {
            Show-Failure "桌面窗口未能打开，请查看日志后重试。"
        }
        return
    }
    if (-not $script:Preparation.HasExited) { return }
    $script:Preparation.WaitForExit()
    if ($script:Preparation.ExitCode -ne 0) {
        Show-Failure "桌面准备未完成，请检查网络与日志后重试。`n源码版首次构建页面需要 Node.js。"
        return
    }
    try {
        $Config = Get-Content -LiteralPath (Join-Path $Runtime "desktop_ready.json") -Raw -Encoding UTF8 | ConvertFrom-Json
        $script:Desktop = Start-Process -FilePath $Config.host -WorkingDirectory $ProjectRoot -PassThru -ArgumentList "--desktop"
        $null = $script:Desktop.Handle
        $script:Launching = $true
        $script:WindowDeadline = [DateTime]::UtcNow.AddSeconds(60)
        $Label.Text = "正在打开视频库…"
    } catch {
        Show-Failure "桌面窗口未能启动，请查看日志后重试。"
    }
})
try {
    [Windows.Forms.Application]::Run($Form)
} finally {
    $Timer.Dispose()
    $Form.BackgroundImage = $null
    $Form.Dispose()
    if ($script:ArtworkSurface) { $script:ArtworkSurface.Dispose() }
    if ($Artwork) { $Artwork.Dispose() }
    $Mutex.ReleaseMutex()
    $Mutex.Dispose()
}
