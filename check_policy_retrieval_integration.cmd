@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\check_policy_retrieval_integration.ps1" %*
if errorlevel 1 (
  echo.
  echo Policy-retrieval check did not complete. Return the short output above to Codex.
  exit /b 1
)
endlocal
