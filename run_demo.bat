@echo off
rem Generates dummy data, runs the reconciliation, answers a sample question, opens the Excel report.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" call setup.bat quiet
set PY=.venv\Scripts\python.exe

echo === 1. Generating dummy gateway and bank files ===
%PY% generate_data.py
echo.
echo === 2. Running reconciliation ===
%PY% reconcile.py --gateway data\gateway_settlement_20260930.csv --bank data\bank_statement_20260930.csv --out output
echo.
echo === 3. Asking the AI analyst a sample question ===
%PY% ai_analyst.py "Which missing in bank items are above 1 lakh and what should I do?"
echo.
echo Opening the Excel report...
start "" "output\Reconciliation_20260930.xlsx"
pause
