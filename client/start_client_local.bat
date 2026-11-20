@echo off
rem RealTran client on the server PC itself: subtitles for this PC's microphone + system audio,
rem connecting to the RealTran server running on this same PC (start it first with start.bat).
rem First run creates a Python environment here (needs Python 3.10+ from python.org).
chcp 65001 >nul
cd /d "%~dp0"
call "%~dp0setup_env.bat" || exit /b 1
.venv\Scripts\python.exe realtran_client.py %*
