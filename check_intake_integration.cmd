@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\check_intake_integration.ps1"
if errorlevel 1 (
  echo.
  echo Primary intake check did not complete. Return the message above to Codex.
  exit /b 1
)
endlocal
