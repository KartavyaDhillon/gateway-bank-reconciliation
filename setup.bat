@echo off
rem One-time setup: creates a virtual environment and installs requirements.
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 goto nopython
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv .venv
)
echo Installing requirements...
".venv\Scripts\python.exe" -m pip install --upgrade pip -q
".venv\Scripts\python.exe" -m pip install -r requirements.txt -q
if errorlevel 1 goto failed
if not exist ".env" copy ".env.example" ".env" >nul
echo.
echo Setup complete.
if /i not "%~1"=="quiet" pause
exit /b 0

:nopython
echo Python was not found. Install Python 3.10+ from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" during installation.
pause
exit /b 1

:failed
echo Installing requirements failed. Check your internet connection and try again.
pause
exit /b 1
