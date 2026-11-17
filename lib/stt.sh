#!/usr/bin/env bash
# stt.sh — speech recognition (STT) engine helpers for install.sh.
#
# Sourced after lib/compute.sh (uses vm_write_config). Provides:
#   - vm_has_nvidia_gpu          : 0 if Linux/WSL and nvidia-smi lists a GPU (sets VM_STT_GPU_NAME)
#   - vm_choose_stt_engine       : sets STT_ENGINE to docker | faster-whisper (asks when there is a GPU)
#   - vm_setup_faster_whisper V  : create venv V with uv and install faster-whisper + CUDA libraries
#   - vm_write_stt_config E [V M] : write STT_ENGINE (and FASTER_WHISPER_VENV / _MODEL /
#                                  STT_CPU_FALLBACK) to the config
#   - vm_cpu_fallback_forced      : true | false | "" from --cpu-fallback / VOICEMODE_CPU_FALLBACK
#   - vm_choose_stt_cpu_fallback  : sets STT_CPU_FALLBACK_CHOICE (asks, default No)
#
# The native engine needs no Docker and no sudo: the CUDA libraries come from pip
# (nvidia-cublas-cu12, nvidia-cudnn-cu12), not from a system CUDA install. Unlike
# vm_detect_gpu it therefore does not need the nvidia container runtime.
# See docs/native-gpu-stt/README.md.

FASTER_WHISPER_PACKAGES=(faster-whisper nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*")
FASTER_WHISPER_DEFAULT_VENV="$HOME/.voicemode-local/faster-whisper-venv"
FASTER_WHISPER_DEFAULT_MODEL="small"
VM_STT_GPU_NAME=""

vm_has_nvidia_gpu() {
    VM_STT_GPU_NAME=""
    [ "$(uname -s)" = "Linux" ] || return 1
    command -v nvidia-smi >/dev/null 2>&1 || return 1
    local line
    line="$(nvidia-smi -L 2>/dev/null | grep -m1 '^GPU [0-9]')" || return 1
    VM_STT_GPU_NAME="$(printf '%s' "$line" | sed -e 's/^GPU [0-9]*: //' -e 's/ (UUID:.*//')"
    return 0
}

# STT_CHOICE (install.sh --stt=...) or VOICEMODE_STT forces a choice:
#   docker      the Docker whisper behind whisper-proxy.py (as before)
#   native-gpu  faster-whisper in its own venv
vm_choose_stt_engine() {
    local forced="${STT_CHOICE:-${VOICEMODE_STT:-}}"
    case "$forced" in
        docker)     STT_ENGINE="docker";         echo "  Speech recognition: Docker whisper (forced)"; return 0 ;;
        native-gpu) STT_ENGINE="faster-whisper"; echo "  Speech recognition: native GPU, faster-whisper (forced)"; return 0 ;;
        "") ;;
        *) echo "  Unknown --stt value: $forced (use --stt=docker or --stt=native-gpu)"; return 1 ;;
    esac
    STT_ENGINE="docker"
    if ! vm_has_nvidia_gpu; then
        echo "  No NVIDIA GPU found: speech recognition stays on Docker whisper."
        return 0
    fi
    echo "  NVIDIA GPU found: $VM_STT_GPU_NAME"
    echo ""
    echo "    1) Native GPU (recommended) — faster-whisper on the GPU, no Docker, no sudo."
    echo "         Measured on an RTX 4080 Laptop (2026-11-16, n=5): a 7.6 s clip in a median"
    echo "         2.6 s (model small) vs 11.2 s for Docker whisper base on the CPU."
    echo "         Disk: the venv is 2.6 GB (CUDA libraries from pip), the small model 464 MB."
    echo "    2) Docker whisper on the CPU (as before)."
    echo ""
    local stt_choice=""
    read -r -p "  Choice [1]: " stt_choice || true
    case "${stt_choice:-1}" in
        2) STT_ENGINE="docker" ;;
        *) STT_ENGINE="faster-whisper" ;;
    esac
}

vm_setup_faster_whisper() {
    local venv="$1"
    if [ ! -x "$venv/bin/python" ]; then
        uv venv "$venv" || return 1
    fi
    uv pip install --python "$venv/bin/python" "${FASTER_WHISPER_PACKAGES[@]}" || return 1
}

# The CPU fallbacks (STT_CPU_FALLBACK, KOKORO_CPU_FALLBACK) are opt-in (Robert, 2026-11-17).
# vm_cpu_fallback_forced prints true / false when CPU_FALLBACK (install.sh --cpu-fallback,
# or --no-cpu-fallback, which keeps the default) or VOICEMODE_CPU_FALLBACK decides, else
# nothing; then the installer asks, default No. No input (a non-interactive run) is No.
vm_cpu_fallback_forced() {
    case "${CPU_FALLBACK:-${VOICEMODE_CPU_FALLBACK:-}}" in
        true|yes|on|1)   echo true ;;
        false|no|off|0)  echo false ;;
        *) ;;
    esac
}

# Ask one y/N question; sets VM_ANSWER to true or false. $1 = the prompt, printed to stdout
# (read -p prints only on a terminal, so a piped run would not show the question).
vm_ask_yes_no() {
    local answer=""
    printf '%s' "$1"
    read -r answer || true
    echo ""
    case "$answer" in [Yy]|[Yy][Ee][Ss]) VM_ANSWER=true ;; *) VM_ANSWER=false ;; esac
}

# Sets STT_CPU_FALLBACK_CHOICE. The figures are measured (2026-11-17, n=3, this machine;
# docs/native-gpu-stt/README.md "CPU fallback").
vm_choose_stt_cpu_fallback() {
    STT_CPU_FALLBACK_CHOICE="$(vm_cpu_fallback_forced)"
    [ -z "$STT_CPU_FALLBACK_CHOICE" ] || return 0
    echo ""
    echo "  Optional: a CPU fallback for speech recognition (faster-whisper, base model, port 2023)."
    echo "    It keeps speech input working if the GPU engine dies."
    echo "    Cost: about 142 MB extra disk (model cached once), about 260 MiB RAM idle."
    echo "    Speed: 2.5-5 s for a 7.6 s clip on a quiet machine, but 23-30 s under heavy load."
    vm_ask_yes_no "  Turn on the speech recognition CPU fallback? [y/N]: "
    STT_CPU_FALLBACK_CHOICE="$VM_ANSWER"
}

vm_write_stt_config() {
    local engine="$1" venv="${2:-$FASTER_WHISPER_DEFAULT_VENV}" model="${3:-$FASTER_WHISPER_DEFAULT_MODEL}"
    vm_write_config STT_ENGINE "$engine"
    if [ "$engine" = "faster-whisper" ]; then
        vm_write_config FASTER_WHISPER_VENV "$venv"
        vm_write_config FASTER_WHISPER_MODEL "$model"
        local fallback="${STT_CPU_FALLBACK_CHOICE:-$(vm_cpu_fallback_forced)}"
        vm_write_config STT_CPU_FALLBACK "${fallback:-false}"
    fi
}
