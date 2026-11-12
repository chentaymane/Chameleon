@echo off
setlocal
cd /d "%~dp0" || exit /b 1
title Chameleon
set "CHECK=import sys; assert sys.version_info >= (3, 10)"

if exist ".venv\windows\Scripts\python.exe" (
    ".venv\windows\Scripts\python.exe" -c "%CHECK%" >nul 2>&1 && goto local
)
py -3 -c "%CHECK%" >nul 2>&1 && goto py_launcher
python -c "%CHECK%" >nul 2>&1 && goto python
python3 -c "%CHECK%" >nul 2>&1 && goto python3
echo Install Python 3.10 or newer with Tcl/Tk support, then run run.bat again.
goto failed

:local
".venv\windows\Scripts\python.exe" launch.py
goto result

:py_launcher
py -3 launch.py
goto result

:python
python launch.py
goto result

:python3
python3 launch.py

:result
if errorlevel 1 goto failed
exit /b 0

:failed
pause
exit /b 1
