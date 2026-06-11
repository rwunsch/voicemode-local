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

# NOTE: as of voice-mode 8.7.x we no longer patch the converse PROMPT or ship a
# switch_mode tool/slash-command. Upstream now provides native config tools
# (configuration_management: update_config/config_reload) that write
# voicemode.env, and the session-queue LLM contract lives in the QUEUED status
# message + the converse tool docstring (added by patch_converse_queue.py) +
# the project CLAUDE.md. Mode switching is the `voicemode-switch` CLI.

# Install the session queue module and patch converse.py to use it.
# If the patcher aborts on upstream drift (set -e), voice_queue.py is left
# copied but converse.py unpatched — converse then falls back to its own
# conch arbitration and the install stays functional. Fix the anchors in
# patch_converse_queue.py and re-run.
if [ -f "$SCRIPT_DIR/voice_queue.py" ]; then
    cp "$SCRIPT_DIR/voice_queue.py" "$VM_DIR/voice_queue.py"
    echo "[patches] Applied voice_queue.py → $VM_DIR/voice_queue.py"
fi
if [ -f "$SCRIPT_DIR/patch_converse_queue.py" ]; then
    PYBIN="$VENV_DIR/bin/python"
    [ -x "$PYBIN" ] || PYBIN="$VENV_DIR/Scripts/python.exe"
    [ -x "$PYBIN" ] || PYBIN="python3"
    "$PYBIN" "$SCRIPT_DIR/patch_converse_queue.py" "$VM_DIR/tools/converse.py"
fi

# Re-raise client cancellations in converse.py. Upstream 8.7.1 swallows
# CancelledError and returns a result; under fastmcp 3.x/mcp>=1.26 that
# double-responds and kills the MCP server (next call: -32000 Connection
# closed). See patch_converse_cancel.py for details.
if [ -f "$SCRIPT_DIR/patch_converse_cancel.py" ]; then
    PYBIN="$VENV_DIR/bin/python"
    [ -x "$PYBIN" ] || PYBIN="$VENV_DIR/Scripts/python.exe"
    [ -x "$PYBIN" ] || PYBIN="python3"
    "$PYBIN" "$SCRIPT_DIR/patch_converse_cancel.py" "$VM_DIR/tools/converse.py"
fi

# Apply the "no silent OpenAI voice swap" patch to simple_failover.py.
if [ -f "$SCRIPT_DIR/patch_simple_failover.py" ]; then
    PYBIN="$VENV_DIR/bin/python"
    [ -x "$PYBIN" ] || PYBIN="$VENV_DIR/Scripts/python.exe"
    [ -x "$PYBIN" ] || PYBIN="python3"
    "$PYBIN" "$SCRIPT_DIR/patch_simple_failover.py" "$VM_DIR/simple_failover.py"
fi

# Windows-only shims for POSIX-only stdlib modules voice-mode imports (fcntl,
# resource). Dormant on Linux/macOS (the real stdlib wins import resolution); on
# Windows, stdlib lookup fails and Python falls through to these in site-packages.
if [ -d "$VENV_DIR/Lib/site-packages" ]; then
    SITE_PKGS="$VENV_DIR/Lib/site-packages"
    if [ -f "$SCRIPT_DIR/fcntl_shim.py" ]; then
        cp "$SCRIPT_DIR/fcntl_shim.py" "$SITE_PKGS/fcntl.py"
        echo "[patches] Installed Windows fcntl shim → $SITE_PKGS/fcntl.py"
    fi
    if [ -f "$SCRIPT_DIR/resource_shim.py" ]; then
        cp "$SCRIPT_DIR/resource_shim.py" "$SITE_PKGS/resource.py"
        echo "[patches] Installed Windows resource shim → $SITE_PKGS/resource.py"
    fi
fi

echo "[patches] Done. Restart Claude Code for changes to take effect."
