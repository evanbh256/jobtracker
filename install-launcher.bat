@echo off
rem One-time: lets the Brave extension start (and stop) the tracker app.
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (py -3 native\install.py %*) else (python native\install.py %*)
pause
