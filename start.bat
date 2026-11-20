@echo off
rem RealTran: start the subtitle server for this PC and the LAN.
rem Extra options are passed through, e.g.  start.bat --device 3   or   start.bat --loopback
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
".venv\Scripts\python.exe" -W ignore app.py --lan %*
pause
