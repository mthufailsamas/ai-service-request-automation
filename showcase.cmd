@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File ".\showcase\run.ps1"
exit /b %ERRORLEVEL%
