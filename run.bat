@echo off
title Chameleon
echo Chameleon - smart IP rotation
echo -----------------------------
echo Installing dependencies...
python -m pip install -r requirements.txt -q
echo.
echo Launching Chameleon...
python chameleon.py
pause
