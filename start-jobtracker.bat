@echo off
rem One-click launcher: starts the tracker and opens the board in your browser.
rem Optional: set JOBTRACKER_DATA to use a different data folder.
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 app\jobtracker.py --open %*
) else (
  where python >nul 2>nul
  if errorlevel 1 (
    echo Python 3 was not found. Install it from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^).
    pause
    exit /b 1
  )
  python app\jobtracker.py --open %*
)
if errorlevel 1 pause
