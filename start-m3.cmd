@echo off
if /i "%~1"=="--check" (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-m3.ps1" -Check
  exit /b %errorlevel%
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-m3.ps1" %*
exit /b %errorlevel%
