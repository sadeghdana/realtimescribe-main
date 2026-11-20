@echo off
rem RealTran client for PCs without SSH access: connects straight to the server over the LAN
rem (https, port 8765; the server must be running with --lan, which start.bat does).
rem First run creates a Python environment here (needs Python 3.10+ from python.org).
set SERVER=wss://__SERVER_IP__:8765
chcp 65001 >nul
cd /d "%~dp0"
call "%~dp0setup_env.bat" || exit /b 1
.venv\Scripts\python.exe realtran_client.py --server "%SERVER%" %*
