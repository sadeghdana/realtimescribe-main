#!/bin/bash
# RealTran server: one-time setup on an Apple Silicon Mac (runs on the Apple GPU via Metal).
# Creates .venv in this folder, installs PyTorch and the other packages, then downloads the models
# (about 20 GB) from Hugging Face and verifies them. Safe to run again after an interruption.
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "Creating the Python environment..."
  python3 -m venv .venv
fi
.venv/bin/python -c "import sys; sys.exit(not (3, 10) <= sys.version_info[:2] <= (3, 14))" \
  || { echo "Python 3.10 to 3.14 is needed (brew install python@3.12), then delete .venv and run this again."; exit 1; }
echo "Installing PyTorch and the other packages..."
.venv/bin/python -m pip install -q -U pip
.venv/bin/python -m pip install -q torch==2.11.0 torchaudio==2.11.0
.venv/bin/python -m pip install -q -r requirements.txt mlx mlx-lm mlx-whisper
echo "Building the Mac audio helper (Speaker input, echo-cancelled microphone, English voice)..."
.venv/bin/python -c "from realtran.audio import MacAudioHelper as H; H.build()" \
  || echo "  (skipped: install the Xcode command line tools with  xcode-select --install  to use the Speaker input)"
.venv/bin/python -c "import torch; assert torch.backends.mps.is_available(), 'PyTorch cannot use the Apple GPU'; print('PyTorch', torch.__version__, 'on the Apple GPU')"
echo
.venv/bin/python download_models.py --mlx
echo
echo "Setup finished. Start RealTran with ./start_mac.sh"
