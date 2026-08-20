@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\check_stage_5_database.ps1"
if errorlevel 1 (
  echo.
  echo Stage 5 check did not complete. Return the message above to Codex.
  exit /b 1
)
endlocal
