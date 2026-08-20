@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\check_service_desk_sandbox.ps1"
if errorlevel 1 (
  echo.
  echo Service Desk Sandbox check did not complete. Return the message above to Codex.
  exit /b 1
)
endlocal
