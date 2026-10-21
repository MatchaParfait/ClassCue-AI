@echo off
setlocal
cd /d "%~dp0.."

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0webui.ps1" -Action start

if errorlevel 1 (
  echo.
  echo Frontend launch failed. Run "cd frontend && npm install" and try again.
  pause
  exit /b 1
)

endlocal
