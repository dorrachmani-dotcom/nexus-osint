@echo off
rem ============================================================
rem  Nexus-OSINT - Windows installer (run once)
rem  Double-click this. It sets up everything (Python env +
rem  dependencies + icon) and puts a "Nexus OSINT" icon on your
rem  Desktop. From then on, just use that icon to open the app.
rem ============================================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
echo.
pause
