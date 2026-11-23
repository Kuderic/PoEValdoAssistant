@echo off
rem Portable launcher: starts Valdo Sniper with the bundled, signed python.org runtime.
cd /d "%~dp0"
start "" "%~dp0python\pythonw.exe" "%~dp0launch.py"
