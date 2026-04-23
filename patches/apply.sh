#!/bin/bash
# Apply voicemode-local patches to the installed voice_mode package.
# Portable: works on Linux/WSL, macOS, and Windows (Git Bash/WSL).
#
# Usage: ./patches/apply.sh [venv-path]
#   venv-path defaults to the .venv in this repo directory.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
VENV_DIR="${1:-$REPO_DIR/.venv}"

# Find the voice_mode package directory
VM_DIR=""
for pyver in "$VENV_DIR"/lib/python*/site-packages/voice_mode; do
    if [ -d "$pyver" ]; then
        VM_DIR="$pyver"
        break
    fi
done

# Also check Windows-style venv layout
if [ -z "$VM_DIR" ] && [ -d "$VENV_DIR/Lib/site-packages/voice_mode" ]; then
    VM_DIR="$VENV_DIR/Lib/site-packages/voice_mode"
fi

if [ -z "$VM_DIR" ]; then
    echo "[patches] ERROR: Could not find voice_mode in $VENV_DIR"
    exit 1
fi

PROMPTS_DIR="$VM_DIR/prompts"
TOOLS_DIR="$VM_DIR/tools"

# Apply converse prompt patch
if [ -f "$SCRIPT_DIR/converse.py" ]; then
    cp "$SCRIPT_DIR/converse.py" "$PROMPTS_DIR/converse.py"
    echo "[patches] Applied converse.py → $PROMPTS_DIR/converse.py"
fi

# Apply switch_mode prompt patch (adds /voicemode:switch-mode to slash menu)
if [ -f "$SCRIPT_DIR/switch_mode_prompt.py" ]; then
    cp "$SCRIPT_DIR/switch_mode_prompt.py" "$PROMPTS_DIR/switch_mode.py"
    echo "[patches] Applied switch_mode_prompt.py → $PROMPTS_DIR/switch_mode.py"
fi

# Apply switch_mode tool patch
if [ -f "$SCRIPT_DIR/switch_mode.py" ] && [ -d "$TOOLS_DIR" ]; then
    cp "$SCRIPT_DIR/switch_mode.py" "$TOOLS_DIR/switch_mode.py"
    echo "[patches] Applied switch_mode.py → $TOOLS_DIR/switch_mode.py"

    # Add switch_mode to default tools list so it loads without env var config
    TOOLS_INIT="$TOOLS_DIR/__init__.py"
    if [ -f "$TOOLS_INIT" ] && ! grep -q "switch_mode" "$TOOLS_INIT"; then
        sed -i 's/default_tools = {"converse", "service", "connect_status"}/default_tools = {"converse", "service", "connect_status", "switch_mode"}/' "$TOOLS_INIT"
        echo "[patches] Added switch_mode to default tools in __init__.py"
    fi
fi

echo "[patches] Done. Restart Claude Code for changes to take effect."
