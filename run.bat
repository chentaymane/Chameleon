@echo off
setlocal
cd /d "%~dp0"
title Chameleon
python -c "import requests, socks" >nul 2>&1
if errorlevel 1 (
    echo Installing Chameleon dependencies...
    python -m pip install -r requirements.txt
    if errorlevel 1 goto failed
)
python chameleon.py
if errorlevel 1 goto failed
exit /b 0

:failed
echo Chameleon could not start. See the error above.
pause
exit /b 1
