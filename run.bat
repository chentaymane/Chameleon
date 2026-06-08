@echo off
echo Anti-Detect Suite
echo -----------------
echo Installing dependencies...
python -m pip install -r requirements.txt -q
echo.
echo Launching app...
python ip_changer.py
pause
