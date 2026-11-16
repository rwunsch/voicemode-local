#!/usr/bin/env bash
#
# faster-whisper-start.sh — start faster-whisper-server.py from its venv, with the
# venv's CUDA libraries on LD_LIBRARY_PATH.
#
# Usage: scripts/faster-whisper-start.sh <venv> [server args...]
#   e.g. scripts/faster-whisper-start.sh ~/.voicemode-local/faster-whisper-venv --port 2022 --model small
#
# Why a wrapper: ctranslate2 dlopens cuBLAS and cuDNN when it is imported, so the
# library path must be set before the process starts; setting it in-process is too
# late. The libraries come from the pip packages nvidia-cublas-cu12 and
# nvidia-cudnn-cu12, which are namespace packages: their lib dirs are
# nvidia.cublas.__path__[0]/lib and nvidia.cudnn.__path__[0]/lib. Without them (a
# CPU-only venv) the path is left as it was. voicemode-switch and install.sh both
# start the server through this script.

set -euo pipefail

if [ $# -lt 1 ]; then
    echo "usage: $0 <faster-whisper venv> [server args...]" >&2
    exit 2
fi
VENV="$1"; shift
PY="$VENV/bin/python"
if [ ! -x "$PY" ]; then
    echo "faster-whisper venv not found: $VENV (no $PY). Create it with install.sh, or set FASTER_WHISPER_VENV in ~/.voicemode-local/config" >&2
    exit 2
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

CUDA_LIBS="$("$PY" -c '
import os
dirs = []
for name in ("nvidia.cublas", "nvidia.cudnn"):
    try:
        module = __import__(name, fromlist=["_"])
    except ImportError:
        continue
    for base in module.__path__:
        lib = os.path.join(base, "lib")
        if os.path.isdir(lib):
            dirs.append(lib)
print(":".join(dirs))
')"

if [ -n "$CUDA_LIBS" ]; then
    export LD_LIBRARY_PATH="$CUDA_LIBS${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi

exec "$PY" "$REPO_DIR/faster-whisper-server.py" "$@"
