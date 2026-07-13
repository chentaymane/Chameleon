#!/usr/bin/env bash
# Chameleon launcher for Linux / macOS
set -e

echo "Chameleon - smart IP rotation"
echo "-----------------------------"

# Pick a Python 3 interpreter
if command -v python3 >/dev/null 2>&1; then
    PY=python3
elif command -v python >/dev/null 2>&1; then
    PY=python
else
    echo "Python 3 not found. Install it first (e.g. 'sudo apt install python3 python3-tk')."
    exit 1
fi

# tkinter is required and is not always bundled on Linux
if ! "$PY" -c "import tkinter" >/dev/null 2>&1; then
    echo "tkinter is missing. On Debian/Ubuntu: sudo apt install python3-tk"
    echo "On Fedora:       sudo dnf install python3-tkinter"
    echo "On Arch:         sudo pacman -S tk"
    exit 1
fi

echo "Installing dependencies..."
"$PY" -m pip install -r requirements.txt -q || \
    echo "pip install failed (continuing — deps may already be present)."

echo
echo "Launching Chameleon..."
exec "$PY" chameleon.py
