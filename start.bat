@echo off
REM Keep this file ASCII-only: cmd.exe misparses non-ASCII bytes in .bat files
REM (codepage vs UTF-8 line-offset misalignment), which can break even `cd`/`python`.
REM Chinese messages are printed by server.py, which handles UTF-8 correctly.
chcp 65001 >nul
cd /d "%~dp0engine"

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo [ERROR] Python not found in PATH.
  echo         Install Python and tick "Add python.exe to PATH", then run this again.
  echo.
  pause
  exit /b 1
)

echo ============================================================
echo  WzTalent-Screening local console
echo  Close this window to stop the backend.
echo ============================================================
echo.

REM server.py opens the browser itself (--open), only AFTER the port is actually
REM listening. Opening it here would race the server and show
REM "can't reach this page" for the first few hundred milliseconds.
python -u server.py --open

echo.
echo [backend stopped]
pause
