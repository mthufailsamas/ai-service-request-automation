@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\check_ai_analysis_ollama_smoke.ps1"
if errorlevel 1 (
  echo.
  echo Ollama smoke check did not complete. Return the short output above to Codex.
  exit /b 1
)
endlocal
