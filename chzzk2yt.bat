@echo off
rem Launch UI without console window
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo Not installed yet. Starting setup via menu.bat ...
  call "%~dp0menu.bat"
  exit /b
)
start "" ".venv\Scripts\pythonw.exe" -m chzzk2yt.gui
