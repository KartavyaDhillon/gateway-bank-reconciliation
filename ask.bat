@echo off
rem Ask the AI analyst questions about the latest reconciliation. Run run_demo.bat first.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" call setup.bat quiet
if not exist "output\results_*.csv" (
    echo No results yet - run run_demo.bat first.
    pause
    exit /b 1
)
:loop
echo.
set "Q="
set /p Q=Ask a question (or press Enter to quit): 
if "%Q%"=="" exit /b 0
.venv\Scripts\python.exe ai_analyst.py %Q%
goto loop
