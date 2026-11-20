@echo off
rem Valdo Sniper launcher: double-click to run. The first run creates a
rem private Python environment in .venv and installs what it needs; later
rem runs start straight away. Extra arguments go to the app (e.g. --headless).
setlocal
cd /d "%~dp0"
title Valdo Sniper

if exist ".venv\Scripts\python.exe" goto run

echo First run: setting up Valdo Sniper (about a minute)...
set "PY="
where py >nul 2>nul && py -3.12 -c "import sys" >nul 2>nul && set "PY=py -3.12"
if not defined PY where py >nul 2>nul && py -3 -c "import sys; sys.exit(sys.version_info < (3, 12))" >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && python -c "import sys; sys.exit(sys.version_info < (3, 12))" >nul 2>nul && set "PY=python"
if not defined PY goto nopython
%PY% -m venv .venv || goto fail

:run
".venv\Scripts\python.exe" tools\bootstrap.py || goto fail
".venv\Scripts\python.exe" -m sniper %*
if errorlevel 1 goto fail
exit /b 0

:nopython
echo.
echo Python 3.12 or newer is required and was not found.
echo   Install it from https://www.python.org/downloads/
echo   (tick "Add python.exe to PATH"), or run:
echo   winget install -e --id Python.Python.3.12
echo Then double-click start.bat again.
pause
exit /b 1

:fail
echo.
echo Valdo Sniper stopped with an error - see the message above.
pause
exit /b 1
