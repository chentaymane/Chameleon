#!/usr/bin/env bash
# Chameleon launcher for Linux / macOS
# Sets up everything automatically - no sudo/root required.
set -euo pipefail

cd "$(dirname "$0")"

echo "Chameleon - smart IP rotation"
echo "-----------------------------"

# Make sure a user-local bin dir is on PATH (where uv installs to)
export PATH="$HOME/.local/bin:$PATH"

VENV=".venv"
PYVER="3.12"

have_working_venv() {
    [ -x "$VENV/bin/python" ] && \
        "$VENV/bin/python" -c "import tkinter, requests, socks" >/dev/null 2>&1
}

# 1. Ensure uv is installed (gives us a Python with tkinter bundled, no sudo)
if ! command -v uv >/dev/null 2>&1; then
    echo "Installing uv (user-local, no sudo)..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

# 2. Build / repair the virtual environment if it's not fully working
if ! have_working_venv; then
    echo "Setting up Python $PYVER environment (bundles tkinter)..."
    uv python install "$PYVER"
    rm -rf "$VENV"
    uv venv --python "$PYVER" "$VENV"
    echo "Installing dependencies..."
    uv pip install --python "$VENV/bin/python" -r requirements.txt
fi

# 3. Final sanity check
if ! have_working_venv; then
    echo "ERROR: environment setup failed. tkinter/requests still missing." >&2
    exit 1
fi

echo
echo "Launching Chameleon..."
exec "$VENV/bin/python" chameleon.py
