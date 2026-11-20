# Sourced by the start scripts: makes sure .venv in this folder is a working environment.
# A .venv copied from another machine points at that machine's Python, so it is rebuilt.
if [ -d .venv ] && ! .venv/bin/python -c "import numpy, soundcard, websockets, tkinter" 2>/dev/null; then
    echo "The Python environment in this folder does not work on this machine - rebuilding it..."
    rm -rf .venv
fi
if [ ! -d .venv ]; then
    echo "Setting up the client environment (first run only)..."
    if ! python3 -c "import tkinter" 2>/dev/null; then
        echo "Python 3.10+ with Tk was not found. Install Python from https://www.python.org/ and run this again."
        return 1 2>/dev/null || exit 1
    fi
    if ! python3 -m venv .venv || ! .venv/bin/pip install -q -U pip || ! .venv/bin/pip install -q -r requirements.txt; then
        echo "Setting up failed. Check the network connection and run this again."
        rm -rf .venv
        return 1 2>/dev/null || exit 1
    fi
fi
