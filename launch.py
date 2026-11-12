"""Set up a project-local Python environment and open the desktop dashboard."""

import os
from pathlib import Path
import subprocess
import sys

PROJECT = Path(__file__).resolve().parent


def launch():
    if sys.version_info < (3, 10):
        print("Chameleon requires Python 3.10 or newer.", file=sys.stderr)
        return 1
    try:
        import tkinter  # Check the interpreter before trying to install anything.
    except ImportError:
        print("This Python installation is missing Tkinter.", file=sys.stderr)
        if sys.platform.startswith("linux"):
            print("Install your distribution's Tkinter package (e.g. python3-tk or "
                  "python3-tkinter), then run bash run.sh again.", file=sys.stderr)
        else:
            print("Install Python with Tcl/Tk support and run the launcher again.", file=sys.stderr)
        return 1

    windows = os.name == "nt"
    environment = PROJECT / ".venv" / ("windows" if windows else "unix")
    interpreter = environment / ("Scripts/python.exe" if windows else "bin/python")
    try:
        if not interpreter.is_file():
            subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True)
        imports = subprocess.run([str(interpreter), "-c", "import requests, socks"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if imports.returncode:
            subprocess.run([str(interpreter), "-m", "pip", "install", "-r",
                            str(PROJECT / "requirements.txt")], check=True)
        return subprocess.call([str(interpreter), str(PROJECT / "chameleon.py")], cwd=PROJECT)
    except (OSError, subprocess.SubprocessError) as error:
        print(f"Could not prepare the Chameleon environment: {error}", file=sys.stderr)
        if sys.platform.startswith("linux"):
            print("On Debian or Ubuntu, install python3-venv if venv or ensurepip is missing.",
                  file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(launch())
