@echo off
setlocal
cd /d "%~dp0"

if /i "%~1"=="start" goto start
if /i "%~1"=="ollama" goto ollama
if /i "%~1"=="stop" goto stop

echo Usage: demo.cmd ^<start^|ollama^|stop^>
echo.
echo   start   Start the local guided portal with fictional learning cases.
echo   ollama  Analyze 1 fictional request with the installed local model.
echo   stop    Remove the disposable demo services and temporary secrets.
exit /b 2

:start
powershell -NoProfile -ExecutionPolicy Bypass -File ".\demo\start.ps1"
exit /b %ERRORLEVEL%

:ollama
powershell -NoProfile -ExecutionPolicy Bypass -File ".\demo\ollama.ps1"
exit /b %ERRORLEVEL%

:stop
powershell -NoProfile -ExecutionPolicy Bypass -File ".\demo\stop.ps1"
exit /b %ERRORLEVEL%
