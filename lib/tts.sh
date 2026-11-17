#!/usr/bin/env bash
# tts.sh — speech output (TTS) engine helpers for install.sh.
#
# Sourced after lib/compute.sh (vm_write_config) and lib/stt.sh (vm_has_nvidia_gpu).
# Provides:
#   - vm_choose_tts_engine        : sets TTS_ENGINE to docker | native-gpu (asks when there is a GPU)
#   - vm_setup_kokoro_gpu V       : create venv V with uv and install kokoro-onnx on onnxruntime-gpu
#   - vm_write_tts_config E [V]   : write KOKORO_ONNX / KOKORO_ONNX_GPU / _VENV / _PORT /
#                                  KOKORO_CPU_FALLBACK to the config
#   - vm_choose_tts_cpu_fallback  : sets KOKORO_CPU_FALLBACK_CHOICE (asks, default No)
#   - vm_choose_kokoro_provider   : sets KOKORO_PROVIDER_CHOICE (asks with the GPU engine,
#                                  default auto); vm_write_tts_config writes it
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

# Sets KOKORO_CPU_FALLBACK_CHOICE. The figures are measured (2026-11-17, n=3, this machine;
# docs/native-gpu-tts/README.md "CPU fallback").
vm_choose_tts_cpu_fallback() {
    KOKORO_CPU_FALLBACK_CHOICE="$(vm_cpu_fallback_forced)"
    [ -z "$KOKORO_CPU_FALLBACK_CHOICE" ] || return 0
    echo ""
    echo "  Optional: a CPU fallback for speech output (Kokoro on the CPU, port 8882)."
    echo "    It keeps English speech output working if the GPU server dies or CUDA fails."
    echo "    Cost: no extra disk (same models and GPU venv), about 450 MiB RAM idle and"
    echo "    up to about 750 MiB in use. It is slower than real time under load."
    vm_ask_yes_no "  Turn on the speech output CPU fallback? [y/N]: "
    KOKORO_CPU_FALLBACK_CHOICE="$VM_ANSWER"
}

# Sets KOKORO_PROVIDER_CHOICE (Robert, 2026-11-17: "users should be able to decide by
# install and configuration"). Forced by KOKORO_PROVIDER_FORCED (install.sh
# --kokoro-provider) or VOICEMODE_KOKORO_PROVIDER: any of auto cuda cpu directml coreml.
# Otherwise asked only with the GPU engine (TTS_ENGINE=native-gpu, which needs an NVIDIA
# GPU): auto / cpu / cuda, default auto, also with no input. directml and coreml are never
# offered (never run by us). Without the GPU engine: empty, nothing is written.
vm_choose_kokoro_provider() {
    local forced="${KOKORO_PROVIDER_FORCED:-${VOICEMODE_KOKORO_PROVIDER:-}}"
    KOKORO_PROVIDER_CHOICE=""
    if [ -n "$forced" ]; then
        case "$forced" in
            auto|cuda|cpu|directml|coreml)
                KOKORO_PROVIDER_CHOICE="$forced"
                echo "  Speech output provider: $forced (forced)"
                return 0 ;;
            *)
                echo "  Unknown --kokoro-provider value: $forced (use auto, cpu, cuda, directml or coreml)"
                return 1 ;;
        esac
    fi
    [ "${TTS_ENGINE:-}" = "native-gpu" ] || return 0
    echo ""
    echo "  Where should Kokoro make speech: on the GPU or on the processor (CPU)?"
    echo "    1) auto (recommended) — the GPU when Kokoro really runs on it, otherwise the CPU,"
    echo "         with a note saying why in the log and in voicemode-switch status."
    echo "    2) cpu — never use the GPU. Much slower: measured on an RTX 4080 Laptop"
    echo "         (2026-11-17, n=5, busy machine) a 270-character reply took a median 63.7 s"
    echo "         on the CPU vs 4.6 s on the GPU."
    echo "    3) cuda — the GPU only: if CUDA does not work, Kokoro stops with an error instead"
    echo "         of falling back to the CPU."
    echo "    Disk: no difference between these; all three run from the GPU venv chosen above"
    echo "    (about 3.3 GB, CUDA libraries from pip). A CPU-only Kokoro venv is 144 MB."
    echo "    Change it later: KOKORO_PROVIDER in ~/.voicemode-local/config (KOKORO_THREADS there"
    echo "    sets the CPU threads; 0 = the default), then voicemode-switch stop && voicemode-switch start."
    local answer=""
    printf '%s' "  Choice [1]: "
    read -r answer || true
    echo ""
    case "${answer:-1}" in
        2) KOKORO_PROVIDER_CHOICE="cpu" ;;
        3) KOKORO_PROVIDER_CHOICE="cuda" ;;
        *) KOKORO_PROVIDER_CHOICE="auto" ;;
    esac
}

# KOKORO_PROVIDER (when chosen or forced) and, with the GPU engine, KOKORO_THREADS=0 unless
# the file already has a value: the key is then there to edit; 0 = onnxruntime's default.
vm_write_kokoro_provider_config() {
    [ -n "${KOKORO_PROVIDER_CHOICE:-}" ] || return 0
    vm_write_config KOKORO_PROVIDER "$KOKORO_PROVIDER_CHOICE"
    if [ -z "$(vm_read_config KOKORO_THREADS)" ]; then
        vm_write_config KOKORO_THREADS 0
    fi
}

vm_write_tts_config() {
    local engine="$1" venv="${2:-$KOKORO_GPU_DEFAULT_VENV}"
    vm_write_kokoro_provider_config
    if [ "$engine" = "native-gpu" ]; then
        vm_write_config KOKORO_ONNX true
        vm_write_config KOKORO_ONNX_GPU true
        vm_write_config KOKORO_ONNX_VENV "$venv"
        vm_write_config KOKORO_ONNX_PORT 8880
        # Opt-in: the answer to vm_choose_tts_cpu_fallback, else --cpu-fallback /
        # VOICEMODE_CPU_FALLBACK (vm_cpu_fallback_forced, lib/stt.sh), else off.
        local fallback="${KOKORO_CPU_FALLBACK_CHOICE:-$(vm_cpu_fallback_forced)}"
        vm_write_config KOKORO_CPU_FALLBACK "${fallback:-false}"
    else
        vm_write_config KOKORO_ONNX_GPU false
    fi
}
