@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -B app.py serve
) else (
  python -B app.py serve
)
pause
