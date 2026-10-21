Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Hasher = [System.Security.Cryptography.SHA256]::Create()
$Hash = [BitConverter]::ToString($Hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($ProjectRoot.ToLowerInvariant()))).Replace("-", "")
$Hasher.Dispose()
$Mutex = New-Object Threading.Mutex($false, "Local\ClassroomBootstrap-$($Hash.Substring(0,12))")
try { $OwnsBootstrap = $Mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $OwnsBootstrap = $true }
if (-not $OwnsBootstrap) { $Mutex.Dispose(); exit 0 }
$Runtime = Join-Path $ProjectRoot ".runtime"
New-Item -ItemType Directory -Force -Path $Runtime | Out-Null
$Form = New-Object System.Windows.Forms.Form
$Form.Text = "课堂重点复习"
$Form.Size = New-Object System.Drawing.Size(500, 285)
$Form.StartPosition = "CenterScreen"
$Form.FormBorderStyle = "FixedDialog"
$Form.MaximizeBox = $false
$Form.BackColor = [Drawing.ColorTranslator]::FromHtml("#F6F4E7")
$Form.Icon = New-Object Drawing.Icon((Join-Path $ProjectRoot "assets\desktop.ico"))
$Label = New-Object System.Windows.Forms.Label
$Label.Location = New-Object Drawing.Point(28, 28)
$Label.Size = New-Object Drawing.Size(430, 108)
$Label.Font = New-Object Drawing.Font("Microsoft YaHei UI", 11)
$Label.Text = "正在准备桌面环境和页面…`n首次启动需要联网下载依赖，请稍候。"
$Form.Controls.Add($Label)
$Progress = New-Object System.Windows.Forms.ProgressBar
$Progress.Location = New-Object Drawing.Point(28, 146)
$Progress.Size = New-Object Drawing.Size(430, 8)
$Progress.Style = "Marquee"
$Form.Controls.Add($Progress)
$Retry = New-Object System.Windows.Forms.Button
$Retry.Text = "重试"
$Retry.Location = New-Object Drawing.Point(28, 178)
$Retry.Visible = $false
$Form.Controls.Add($Retry)
$Logs = New-Object System.Windows.Forms.Button
$Logs.Text = "查看日志"
$Logs.Location = New-Object Drawing.Point(120, 178)
$Logs.Visible = $false
$Form.Controls.Add($Logs)
$Logs.Add_Click({ Start-Process notepad.exe -ArgumentList "`"$(Join-Path $Runtime 'desktop-prepare-error.log')`"" })
$Timer = New-Object System.Windows.Forms.Timer
$Timer.Interval = 250
$script:Preparation = $null
$script:Launching = $false
function Begin-Preparation {
    $Retry.Visible = $false
    $Logs.Visible = $false
    $Progress.Visible = $true
    $Label.Text = "正在检查桌面环境和页面…`n首次启动可能需要一些时间。"
    $script:Preparation = Start-Process powershell.exe -WindowStyle Hidden -PassThru -ArgumentList @(
        "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", "`"$(Join-Path $PSScriptRoot 'prepare_desktop.ps1')`""
    ) -RedirectStandardOutput (Join-Path $Runtime "desktop-prepare.log") -RedirectStandardError (Join-Path $Runtime "desktop-prepare-error.log")
    # Windows PowerShell can lose ExitCode if the handle is first opened after
    # a fast preparation process has already exited.
    $null = $script:Preparation.Handle
    $Timer.Start()
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
    if (-not $script:Preparation.HasExited) { return }
    $Timer.Stop()
    $script:Preparation.WaitForExit()
    if ($script:Preparation.ExitCode -ne 0) {
        $Progress.Visible = $false
        $Label.Text = "桌面准备未完成。请检查网络与日志后重试。`n源码版首次构建页面需要 Node.js。"
        $Retry.Visible = $true
        $Logs.Visible = $true
        return
    }
    try {
        $Config = Get-Content -LiteralPath (Join-Path $Runtime "desktop_ready.json") -Raw | ConvertFrom-Json
        Start-Process -FilePath $Config.pythonw -WorkingDirectory $ProjectRoot -WindowStyle Hidden -ArgumentList "`"$(Join-Path $PSScriptRoot 'desktop_app.py')`""
        $Form.Close()
    } catch {
        $Progress.Visible = $false
        $Label.Text = "桌面窗口未能启动，请查看日志后重试。"
        $Retry.Visible = $true
        $Logs.Visible = $true
    }
})
[System.Windows.Forms.Application]::Run($Form)
$Timer.Dispose()
$Form.Dispose()
$Mutex.ReleaseMutex()
$Mutex.Dispose()
