@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tracewaltz.ps1" %*
exit /b %errorlevel%
