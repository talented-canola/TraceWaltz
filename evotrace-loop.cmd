@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0evotrace-loop.ps1" %*
exit /b %errorlevel%
