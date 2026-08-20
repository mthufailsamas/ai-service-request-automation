@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\check_ai_analysis_database.ps1"
if errorlevel 1 (
  echo.
  echo AI-analysis database check did not complete. Return the short output above to Codex.
  exit /b 1
)
endlocal
