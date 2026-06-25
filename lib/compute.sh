#!/usr/bin/env bash
# compute.sh — shared helpers for VoiceMode compute mode (CPU vs GPU).
#
# Sourced by install.sh and voicemode-switch. Provides:
#   - vm_detect_gpu        : 0 if this host can actually run GPU containers
#   - vm_gpu_reason        : human-readable explanation of the last detect result
#   - vm_read_config KEY [DEFAULT]
#   - vm_write_config KEY VALUE   (upsert into ~/.voicemode-local/config)
#   - vm_recommended_whisper_model MODE
#
# Keep this dependency-free (bash + coreutils + docker) so it works in install.sh
# before the venv exists.

VM_CONFIG_FILE="${VM_CONFIG_FILE:-$HOME/.voicemode-local/config}"
VM_GPU_REASON=""

# Read a KEY=VALUE from the config file, echoing VALUE (or DEFAULT if absent).
vm_read_config() {
    local key="$1" default="${2:-}"
    if [ -f "$VM_CONFIG_FILE" ]; then
        local val
        val="$(grep -oP "^${key}=\K.*" "$VM_CONFIG_FILE" 2>/dev/null | tail -1)"
        if [ -n "${val:-}" ]; then echo "$val"; return 0; fi
    fi
    echo "$default"
}

# Upsert KEY=VALUE into the config file (creates the file/dir if needed).
vm_write_config() {
    local key="$1" value="$2"
    mkdir -p "$(dirname "$VM_CONFIG_FILE")"
    touch "$VM_CONFIG_FILE"
    if grep -q "^${key}=" "$VM_CONFIG_FILE" 2>/dev/null; then
        # Portable in-place edit (avoids sed -i delimiter clashes on image paths).
        local tmp; tmp="$(mktemp)"
        grep -v "^${key}=" "$VM_CONFIG_FILE" > "$tmp"
        printf '%s=%s\n' "$key" "$value" >> "$tmp"
        mv "$tmp" "$VM_CONFIG_FILE"
    else
        printf '%s=%s\n' "$key" "$value" >> "$VM_CONFIG_FILE"
    fi
}

# Can this host actually run GPU containers? Checks all three links in the chain:
#   1. an NVIDIA GPU + driver the host can see (nvidia-smi),
#   2. the nvidia container runtime is registered with the Docker engine,
#   3. docker itself is reachable.
# Sets VM_GPU_REASON. Returns 0 (yes) / 1 (no).
vm_detect_gpu() {
    VM_GPU_REASON=""
    if ! command -v docker >/dev/null 2>&1; then
        VM_GPU_REASON="docker not found"; return 1
    fi
    if ! command -v nvidia-smi >/dev/null 2>&1 || ! nvidia-smi -L >/dev/null 2>&1; then
        VM_GPU_REASON="no NVIDIA GPU/driver visible (nvidia-smi failed)"; return 1
    fi
    if ! docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q '"nvidia"'; then
        VM_GPU_REASON="nvidia-container-toolkit / nvidia docker runtime not registered"; return 1
    fi
    local gpu; gpu="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)"
    VM_GPU_REASON="GPU ready: ${gpu:-NVIDIA GPU}"
    return 0
}

# Sensible default Whisper model per mode. Full-GPU can afford a bigger model;
# hybrid and cpu keep Whisper on CPU, where "base" is the fast, sensible default.
vm_recommended_whisper_model() {
    case "$1" in
        gpu)    echo "small" ;;  # whisper on GPU
        hybrid) echo "base"  ;;  # whisper on CPU (kokoro on GPU)
        *)      echo "base"  ;;
    esac
}
