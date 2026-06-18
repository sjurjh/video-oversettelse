@echo off
setlocal
cd /d "%~dp0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_ffmpeg.ps1"
pause
exit /b %errorlevel%
