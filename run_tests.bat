@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" call setup.bat quiet
.venv\Scripts\python.exe -m pytest -v tests
pause
