#!/usr/bin/env bash
#
# kokoro-gpu-start.sh — start kokoro-onnx-server.py from its GPU venv, on CUDA.
#
# Usage: scripts/kokoro-gpu-start.sh <venv> [server args...]
#   e.g. scripts/kokoro-gpu-start.sh ~/.voicemode-local/kokoro-gpu-venv --port 8880 --require-gpu
#
# Why a wrapper (the same reason as faster-whisper-start.sh): onnxruntime-gpu dlopens
# the CUDA libraries from the library path the process started with, so it must be set
# before Python starts. The libraries come from the nvidia-*-cu12 pip packages (cublas,
# cudnn, cuda_runtime, cufft, curand, cuda_nvrtc, ...), which are namespace packages:
# each lib dir is nvidia.<pkg>.__path__[0]/lib. Every such dir in the venv goes on
# LD_LIBRARY_PATH. kokoro_onnx picks the provider from ONNX_PROVIDER (the server passes
# none), so it is set to CUDAExecutionProvider. Pass --require-gpu so a CUDA provider
# that fails to load ends the server instead of running it on the CPU.
# See docs/native-gpu-tts/README.md.

set -euo pipefail

if [ $# -lt 1 ]; then
    echo "usage: $0 <kokoro GPU venv> [server args...]" >&2
    exit 2
fi
VENV="$1"; shift
PY="$VENV/bin/python"
if [ ! -x "$PY" ]; then
    echo "Kokoro GPU venv not found: $VENV (no $PY). Create it with install.sh, or set KOKORO_ONNX_VENV in ~/.voicemode-local/config" >&2
    exit 2
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

CUDA_LIBS="$("$PY" -c '
import os
dirs = []
try:
    import nvidia
except ImportError:
    nvidia = None
for base in (getattr(nvidia, "__path__", None) or []):
    for name in sorted(os.listdir(base)):
        lib = os.path.join(base, name, "lib")
        if os.path.isdir(lib) and lib not in dirs:
            dirs.append(lib)
print(":".join(dirs))
')"

if [ -n "$CUDA_LIBS" ]; then
    export LD_LIBRARY_PATH="$CUDA_LIBS${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi
export ONNX_PROVIDER=CUDAExecutionProvider

exec "$PY" "$REPO_DIR/kokoro-onnx-server.py" "$@"
