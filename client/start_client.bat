@echo off
rem RealTran client: subtitles for this PC's microphone + system audio, through an SSH tunnel.
rem First run creates a Python environment here (needs Python 3.10+ from python.org).
rem Change SERVER below if you log in to the RealTran server with a different SSH name / alias.
set SERVER=__SSH_USER__@__SERVER_IP__
chcp 65001 >nul
cd /d "%~dp0"
call "%~dp0setup_env.bat" || exit /b 1
.venv\Scripts\python.exe realtran_client.py --ssh "%SERVER%" %*
