@echo off
rem Runs Scout headless on a Windows server (used by install-autostart-windows.bat). Set your password in server-config.bat
cd /d "%~dp0"
if exist server-config.bat call server-config.bat
if "%SCOUT_PASSWORD%"=="" ( set HOST=127.0.0.1 ) else ( set HOST=0.0.0.0 )
.venv\Scripts\python app.py --no-browser >> scout.log 2>&1
