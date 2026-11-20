@echo off
rem Makes sure .venv in this folder is a working Python environment for the client.
rem A .venv copied from another PC points at that PC's Python and cannot run here, so it is rebuilt.
cd /d "%~dp0"
if exist .venv\Scripts\python.exe (
    .venv\Scripts\python.exe -c "import numpy, soundcard, websockets" >nul 2>nul && exit /b 0
    echo The Python environment in this folder does not work on this PC - rebuilding it...
    rmdir /s /q .venv
)
echo Setting up the client environment, first run only...
py -3 -m venv .venv >nul 2>nul || python -m venv .venv >nul 2>nul
if not exist .venv\Scripts\python.exe (
    echo Python 3.10 or newer was not found. Install it from https://www.python.org/ and run this again.
    pause
    exit /b 1
)
.venv\Scripts\python.exe -m pip install -q -U pip
.venv\Scripts\python.exe -m pip install -q -r requirements.txt
if errorlevel 1 (
    echo Installing the required packages failed. Check the network connection and run this again.
    rmdir /s /q .venv
    pause
    exit /b 1
)
exit /b 0
