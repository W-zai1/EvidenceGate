@echo off
REM Keep this file ASCII-only: cmd.exe misparses non-ASCII bytes in .bat files
REM (codepage vs UTF-8 line-offset misalignment), which can break even `cd`/`python`.
REM Chinese messages are printed by server.py, which handles UTF-8 correctly.
chcp 65001 >nul

REM Run from the PROJECT ROOT, not from engine\.
REM server.py lives at engine\api\server.py and is started as a module
REM (`-m engine.api.server`), because every import in it is package-qualified
REM (`from engine.api import api`). Running it as a bare script path fails with
REM "No module named 'engine'".
cd /d "%~dp0"

REM Default port is 8123. If it is busy the server tries 8124..8128 automatically.
REM To force one: set PORT=8300 && start.bat

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo [ERROR] Python not found in PATH.
  echo         Install Python and tick "Add python.exe to PATH", then run this again.
  echo.
  pause
  exit /b 1
)

if not exist "engine\api\server.py" (
  echo.
  echo [ERROR] engine\api\server.py not found.
  echo         Run this .bat from the EvidenceGate folder ^(do not move it^).
  echo.
  pause
  exit /b 1
)

echo ============================================================
echo  EvidenceGate local console
echo  Close this window to stop the backend.
echo ============================================================
echo.

REM --open makes the server open the browser itself, only AFTER the port is
REM actually listening. Opening it here would race the server and show
REM "can't reach this page" for the first few hundred milliseconds.
python -u -m engine.api.server --open

echo.
echo [backend stopped]
pause
