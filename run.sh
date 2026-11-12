#!/usr/bin/env bash
set -eu

cd -- "$(dirname -- "$0")"

for python in .venv/unix/bin/python python3 python; do
    if "$python" -c 'import sys; assert sys.version_info >= (3, 10)' >/dev/null 2>&1; then
        exec "$python" launch.py
    fi
done

echo "Install Python 3.10 or newer with Tkinter and venv, then run bash run.sh again." >&2
exit 1
