@echo off
setlocal
cd /d "%~dp0"
if "%~1"=="" (
  echo Usage: check.cmd ^<recovery-operations^|end-to-end^|locked-evaluation^>
  exit /b 2
)
powershell -NoProfile -ExecutionPolicy Bypass -File ".\checks\run.ps1" -CheckName "%~1"
if errorlevel 1 exit /b 1
endlocal
