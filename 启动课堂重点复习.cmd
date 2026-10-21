@echo off
setlocal
cd /d "%~dp0"

wscript.exe "%~dp0scripts\desktop_launcher.vbs"

endlocal
