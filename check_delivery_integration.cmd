@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\check_delivery_integration.ps1"
if errorlevel 1 (
  echo.
  echo Primary delivery integration check did not complete. Return the message above to Codex.
  exit /b 1
)
endlocal
