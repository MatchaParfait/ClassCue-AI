@echo off
setlocal
cd /d "%~dp0.."

wscript.exe "%~dp0desktop_launcher.vbs" exit
endlocal
