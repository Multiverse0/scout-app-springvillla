@echo off
title Scout
cd /d "%~dp0"

rem ---- 1. Find a real Python (not the Microsoft Store placeholder) ----
set "PY="
py -3 --version >nul 2>nul && set "PY=py -3"
if not defined PY python --version >nul 2>nul && set "PY=python"
if not defined PY goto nopython

rem ---- 2. Remove a half-finished setup from an earlier failed run ----
if exist .venv if not exist .venv\Scripts\python.exe rmdir /s /q .venv

rem ---- 3. First-run setup ----
if exist .venv\Scripts\python.exe goto node
echo.
echo First run: setting up Scout. This takes 3-5 minutes...
%PY% -m venv .venv
if errorlevel 1 goto setupfail
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt
if errorlevel 1 goto setupfail
.venv\Scripts\python -m playwright install chromium
if errorlevel 1 goto setupfail

:node
where node >nul 2>nul
if errorlevel 1 goto nonode
if exist wa-service\node_modules goto run
echo Installing WhatsApp service...
pushd wa-service
call npm install --no-audit --no-fund
popd
goto run

:nonode
echo.
echo NOTE: Node.js is not installed, so WhatsApp auto-send is off.
echo       Install the LTS version from https://nodejs.org then run this file again.
echo.

:run
echo.
echo Starting Scout... keep this window open. Close it to stop.
.venv\Scripts\python app.py
pause
exit /b

:nopython
echo.
echo ============================================================
echo  Python is not installed on this computer yet.
echo.
echo  1. A download page will open now. Download Python 3.12 for Windows (64-bit).
echo  2. In the installer, TICK "Add python.exe to PATH" at the bottom,
echo     then click "Install Now".
echo  3. Close this window, then double-click start-windows.bat again.
echo ============================================================
start "" "https://www.python.org/downloads/windows/"
pause
exit /b 1

:setupfail
echo.
echo Setup did not finish. Check the internet connection, then run this file again.
if exist .venv rmdir /s /q .venv
pause
exit /b 1
