#!/usr/bin/env bash
#
# kokoro-cpu-start.sh — start kokoro-onnx-server.py on the CPU: the CPU fallback for the
# native GPU Kokoro (KOKORO_CPU_FALLBACK=true, port KOKORO_CPU_FALLBACK_PORT, default 8882).
#
# Usage: scripts/kokoro-cpu-start.sh <venv> [server args...]
#   e.g. scripts/kokoro-cpu-start.sh ~/.voicemode-local/kokoro-gpu-venv --port 8882 --no-download
#
# It runs from the SAME venv as the GPU server: onnxruntime-gpu also offers
# CPUExecutionProvider, so installer users need no second venv. When that venv is absent
# it uses the repo's .venv python. kokoro_onnx takes the provider from ONNX_PROVIDER, which
# is set to CPUExecutionProvider whatever the caller had. No CUDA libraries go on the
# library path and --require-gpu is never passed on: the fallback must not depend on
# anything CUDA, since it exists for the times CUDA is what failed.
# See docs/native-gpu-tts/README.md ("CPU fallback").

set -euo pipefail

if [ $# -lt 1 ]; then
    echo "usage: $0 <kokoro venv> [server args...]" >&2
    exit 2
fi
VENV="$1"; shift

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PY="$VENV/bin/python"
if [ ! -x "$PY" ]; then
    PY="$REPO_DIR/.venv/bin/python"
    if [ ! -x "$PY" ]; then
        echo "No python for the Kokoro CPU fallback: neither $VENV/bin/python (KOKORO_ONNX_VENV) nor $REPO_DIR/.venv/bin/python exists" >&2
        exit 2
    fi
fi

args=()
for a in "$@"; do
    [ "$a" = "--require-gpu" ] || args+=("$a")
done

export ONNX_PROVIDER=CPUExecutionProvider
unset VOICEMODE_KOKORO_REQUIRE_GPU   # the server's env twin of --require-gpu
exec "$PY" "$REPO_DIR/kokoro-onnx-server.py" ${args[@]+"${args[@]}"}
