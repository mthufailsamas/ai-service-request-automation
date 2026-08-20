@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\check_human_resume_integration.ps1"
if errorlevel 1 (
  echo.
  echo Human-resume check did not complete. Return the short output above to Codex.
  exit /b 1
)
endlocal
