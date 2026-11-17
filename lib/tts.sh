#!/usr/bin/env bash
# tts.sh — speech output (TTS) engine helpers for install.sh.
#
# Sourced after lib/compute.sh (vm_write_config) and lib/stt.sh (vm_has_nvidia_gpu).
# Provides:
#   - vm_choose_tts_engine        : sets TTS_ENGINE to docker | native-gpu (asks when there is a GPU)
#   - vm_setup_kokoro_gpu V       : create venv V with uv and install kokoro-onnx on onnxruntime-gpu
#   - vm_write_tts_config E [V]   : write KOKORO_ONNX / KOKORO_ONNX_GPU / _VENV / _PORT to the config
#
# Native GPU Kokoro is kokoro-onnx-server.py from its own venv on CUDA, started through
# scripts/kokoro-gpu-start.sh with --require-gpu. Like the native STT it needs no Docker
# and no sudo: the CUDA libraries come from pip. It takes port 8880 in place of the
# Docker Kokoro. See docs/native-gpu-tts/README.md.
#
# Two traps, both measured 2026-11-17 (docs/native-gpu-tts/README.md):
#   - the default pip onnxruntime-gpu (1.32.0) needs CUDA 13 and silently runs on the
#     CPU with the CUDA 12 pip libraries: pin onnxruntime-gpu==1.23.2;
#   - kokoro-onnx requires the CPU `onnxruntime`, which shares its module directory with
#     onnxruntime-gpu; installed together the CPU build won (1.32.0, no CUDA provider).
#     So `onnxruntime` is removed and onnxruntime-gpu reinstalled, then checked.

KOKORO_GPU_ORT="onnxruntime-gpu==1.23.2"
KOKORO_GPU_PACKAGES=(kokoro-onnx soundfile numpy "$KOKORO_GPU_ORT" nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"
                     nvidia-cuda-runtime-cu12 nvidia-cufft-cu12 nvidia-curand-cu12 nvidia-cuda-nvrtc-cu12)
KOKORO_GPU_DEFAULT_VENV="$HOME/.voicemode-local/kokoro-gpu-venv"

# TTS_CHOICE (install.sh --tts=...) or VOICEMODE_TTS forces a choice:
#   docker      the Docker Kokoro on 8880 (as before)
#   native-gpu  kokoro-onnx on CUDA in its own venv, on 8880
vm_choose_tts_engine() {
    local forced="${TTS_CHOICE:-${VOICEMODE_TTS:-}}"
    case "$forced" in
        docker)     TTS_ENGINE="docker";     echo "  Speech output: Docker Kokoro (forced)"; return 0 ;;
        native-gpu) TTS_ENGINE="native-gpu"; echo "  Speech output: native GPU Kokoro (forced)"; return 0 ;;
        "") ;;
        *) echo "  Unknown --tts value: $forced (use --tts=docker or --tts=native-gpu)"; return 1 ;;
    esac
    TTS_ENGINE="docker"
    if ! vm_has_nvidia_gpu; then
        echo "  No NVIDIA GPU found: speech output stays on the Docker Kokoro."
        return 0
    fi
    echo "  NVIDIA GPU found: $VM_STT_GPU_NAME"
    echo ""
    echo "    1) Native GPU Kokoro (recommended) — kokoro-onnx on the GPU, no Docker, no sudo."
    echo "         Replaces the Docker Kokoro on port 8880. Measured on an RTX 4080 Laptop"
    echo "         (2026-11-17, n=5, busy machine): a 270-character reply in a median 4.6 s"
    echo "         vs 63.7 s for kokoro-onnx on the CPU; about the same as the Docker GPU Kokoro."
    echo "         Disk: the venv is about 3.3 GB (CUDA libraries from pip)."
    echo "    2) Docker Kokoro (as before)."
    echo ""
    local tts_choice=""
    read -r -p "  Choice [1]: " tts_choice || true
    case "${tts_choice:-1}" in
        2) TTS_ENGINE="docker" ;;
        *) TTS_ENGINE="native-gpu" ;;
    esac
}

vm_setup_kokoro_gpu() {
    local venv="$1"
    local py="$venv/bin/python"
    if [ ! -x "$py" ]; then
        uv venv "$venv" || return 1
    fi
    uv pip install --python "$py" "${KOKORO_GPU_PACKAGES[@]}" || return 1
    # kokoro-onnx pulled in the CPU onnxruntime over onnxruntime-gpu's files: remove it
    # and put onnxruntime-gpu back.
    uv pip uninstall --python "$py" onnxruntime || true
    uv pip install --python "$py" --reinstall-package onnxruntime-gpu "$KOKORO_GPU_ORT" || return 1
    if ! "$py" -c 'import sys, onnxruntime; sys.exit(0 if "CUDAExecutionProvider" in onnxruntime.get_available_providers() else 1)'; then
        echo "  $venv: onnxruntime offers no CUDAExecutionProvider after the install" >&2
        return 1
    fi
}

vm_write_tts_config() {
    local engine="$1" venv="${2:-$KOKORO_GPU_DEFAULT_VENV}"
    if [ "$engine" = "native-gpu" ]; then
        vm_write_config KOKORO_ONNX true
        vm_write_config KOKORO_ONNX_GPU true
        vm_write_config KOKORO_ONNX_VENV "$venv"
        vm_write_config KOKORO_ONNX_PORT 8880
    else
        vm_write_config KOKORO_ONNX_GPU false
    fi
}
