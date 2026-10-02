@echo off
rem Starts the FastAPI server and opens the interactive API docs in your browser.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" call setup.bat quiet
echo Starting API on http://127.0.0.1:8000  (press Ctrl+C to stop)
start "" cmd /c "timeout /t 4 >nul & start http://127.0.0.1:8000/docs"
.venv\Scripts\python.exe -m uvicorn api:app --host 127.0.0.1 --port 8000 --reload
pause
