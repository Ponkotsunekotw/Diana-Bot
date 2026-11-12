@echo off
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
    py -3.12 gui.py 2>nul
    if not errorlevel 1 exit /b 0
)
if exist "%LocalAppData%\Programs\Python\Python312\python.exe" (
    "%LocalAppData%\Programs\Python\Python312\python.exe" gui.py
    if errorlevel 1 pause
    exit /b 0
)
python gui.py
if errorlevel 1 pause
