@echo off
cd /d "%~dp0"
if not exist "runtime\temp" mkdir "runtime\temp"
set "TEMP=%CD%\runtime\temp"
set "TMP=%TEMP%"
set "PIP_CACHE_DIR=%CD%\runtime\pip-cache"
python -B -m venv .venv
if not exist ".venv\Scripts\python.exe" goto failed
.venv\Scripts\python.exe -B -m pip --version >nul 2>&1
if errorlevel 1 python -B -m pip --python .venv\Scripts\python.exe install pip
.venv\Scripts\python.exe -B -m pip install -r requirements-workflow.txt
if errorlevel 1 goto failed
echo Setup complete. Run start.cmd.
pause
exit /b 0
:failed
echo Setup failed. Keep this window and share the error with the team.
pause
exit /b 1
