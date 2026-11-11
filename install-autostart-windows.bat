@echo off
rem Run ONCE as Administrator (right-click > Run as administrator) after start-windows.bat has set Scout up.
cd /d "%~dp0"
if not exist .venv ( echo Run start-windows.bat first to set Scout up. & pause & exit /b 1 )
if not exist server-config.bat (
  set /p PW=Choose a password for opening the dashboard from other devices: 
  call echo set SCOUT_PASSWORD=%%PW%%> server-config.bat
)
schtasks /create /tn "Scout" /tr "\"%~dp0run-server-windows.bat\"" /sc onstart /ru SYSTEM /rl HIGHEST /f
netsh advfirewall firewall add rule name="Scout 8000" dir=in action=allow protocol=TCP localport=8000 >nul
schtasks /run /tn "Scout"
echo.
echo Done. Scout now starts by itself whenever the server starts, even when you're not logged in.
echo Open it from any device at  http://YOUR-SERVER-IP:8000
pause
