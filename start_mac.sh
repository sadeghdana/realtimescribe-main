#!/bin/bash
# RealTran on a Mac: Japanese speech -> English captions (and speech), shown at http://localhost:8766/
# Input (the menu in the page, or --input mic|speaker|both): this Mac's microphone, what the Mac
# plays (Zoom / Teams / Meet participants, videos; also with headphones), or both. For the speaker,
# allow "Screen & System Audio Recording" for the app this runs in once (only audio is used).
# The models run on Apple's MLX framework, Gemma quantized to 4 bits (~6 GB of memory instead of
# ~16 GB): fetch them once with  .venv/bin/python download_models.py --mlx
# --langs ja,en --to en: only Japanese / English are recognized, and only English is produced,
#   which halves the translation work. For all three languages: ./start_mac.sh --langs zh,ja,en --to zh,ja,en
# Extra options are passed through, e.g.  ./start_mac.sh --device 2   (see --list-devices)
cd "$(dirname "$0")"
M=models
exec .venv/bin/python -W ignore app.py \
  --asr-model $M/mlx-whisper-large-v3-turbo --asr-partial-model $M/mlx-whisper-large-v3-turbo \
  --mt-model $M/mlx-gemma-4-e4b-it-4bit \
  --langs ja,en --to en "$@"
