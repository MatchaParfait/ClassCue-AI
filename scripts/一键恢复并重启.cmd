@echo off
setlocal
cd /d "%~dp0.."
set "PYTHONUTF8=1"
set "PYTHON="
if exist ".venv\Scripts\python.exe" set "PYTHON=.venv\Scripts\python.exe"
if not defined PYTHON if exist ".runtime\venv\Scripts\python.exe" set "PYTHON=.runtime\venv\Scripts\python.exe"
if not defined PYTHON (
    echo Python is not ready. Run the main application launcher first.
    pause
    exit /b 1
)
"%PYTHON%" "%~dp0desktop_recover.py"
if errorlevel 1 (
    echo Recovery stopped. No unverified processes were terminated.
    pause
    exit /b 1
)
endlocal
