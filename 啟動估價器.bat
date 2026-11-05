@echo off
rem DCF Stock Price Estimator launcher: prefer `py` (has tkinter + matplotlib)
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py gui_app.py
) else (
  python gui_app.py
)
if %errorlevel% neq 0 pause
