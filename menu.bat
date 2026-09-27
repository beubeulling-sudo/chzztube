@echo off
rem chzzk2yt number menu. First run installs automatically.
cd /d "%~dp0"

:start
if not exist ".venv\Scripts\python.exe" goto setup
if not exist "vendor\chzzk-vod-downloader-v2\scripts\headless_download.py" goto setup

".venv\Scripts\python.exe" -m chzzk2yt.menu
if "%errorlevel%"=="10" goto setup
exit /b 0

:setup
echo.
echo [setup] install / update ...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
if errorlevel 1 (
  echo [setup] failed. Check the messages above.
  pause
  exit /b 1
)
pause
goto start
