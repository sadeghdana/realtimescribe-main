@echo off
rem RealTran server: one-time setup on a Windows PC with an NVIDIA GPU.
rem Creates .venv in this folder, installs PyTorch (CUDA 12.8) and the other packages, then downloads
rem the models (about 20 GB) from Hugging Face and verifies them. Safe to run again after an
rem interruption: finished steps are skipped quickly.
chcp 65001 >nul
cd /d "%~dp0"
rem Some files in the Python packages are nested ~150 characters deep: in a deeper folder their paths
rem exceed Windows' 260-character limit and the installation fails.
set "HERE=%~dp0"
if not "%HERE:~80,1%"=="" goto long_path

where nvidia-smi >nul 2>nul
if errorlevel 1 goto no_gpu
echo GPU:
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader

if exist .venv\Scripts\python.exe goto have_python
echo Creating the Python environment...
py -3.12 -m venv .venv >nul 2>nul
if not exist .venv\Scripts\python.exe py -3 -m venv .venv >nul 2>nul
if not exist .venv\Scripts\python.exe python -m venv .venv >nul 2>nul
if not exist .venv\Scripts\python.exe goto no_python
:have_python
.venv\Scripts\python.exe -c "import sys; sys.exit(not (3, 10) <= sys.version_info[:2] <= (3, 14))"
if errorlevel 1 goto old_python

echo.
echo Installing PyTorch with CUDA 12.8 - about 3 GB on the first run...
.venv\Scripts\python.exe -m pip install -q -U pip
.venv\Scripts\python.exe -m pip install torch==2.11.0 torchaudio==2.11.0 --index-url https://download.pytorch.org/whl/cu128
if errorlevel 1 goto failed
echo Installing the other packages...
.venv\Scripts\python.exe -m pip install -q -r requirements.txt
if errorlevel 1 goto failed
.venv\Scripts\python.exe -c "import torch; assert torch.cuda.is_available(), 'PyTorch cannot use the GPU: update the NVIDIA driver'; print('PyTorch', torch.__version__, 'on', torch.cuda.get_device_name(0))"
if errorlevel 1 goto failed

echo.
.venv\Scripts\python.exe download_models.py
if errorlevel 1 goto failed

echo.
echo Setup finished. Start the server with start.bat - see README_EN.md.
pause
exit /b 0

:long_path
echo This folder's path is too long for the Python packages - Windows allows 260 characters:
echo   %~dp0
echo Move the RealTran folder to a short path such as C:\RealTran and run setup_server.bat there.
pause
exit /b 1
:no_gpu
echo No NVIDIA driver was found. RealTran needs an NVIDIA GPU with 16 GB of video memory
echo or 12 GB when started with --light. Install the NVIDIA driver and run this again.
pause
exit /b 1
:no_python
echo Python was not found. Install Python 3.12 from https://www.python.org/downloads/windows/
echo and tick "Add python.exe to PATH" in the installer, then run this again.
pause
exit /b 1
:old_python
echo The Python found is not version 3.10 to 3.14. Install Python 3.12 from python.org,
echo delete the .venv folder here and run this again.
pause
exit /b 1
:failed
echo.
echo Setup stopped because of the error above. Fix it - usually the network or the NVIDIA driver -
echo and run setup_server.bat again.
pause
exit /b 1
