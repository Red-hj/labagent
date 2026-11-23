@echo off
setlocal
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Please install Python 3.10 or newer, then reopen this file.
  goto finish
)
if exist "runtime\mcp-ensp.json" goto connect
python -B scripts\connect_lab.py --setup
if errorlevel 1 goto finish
python -B scripts\connect_lab.py --probe
if errorlevel 1 goto finish
:connect
python -B scripts\connect_lab.py
:finish
pause
