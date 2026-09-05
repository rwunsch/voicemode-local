#!/bin/bash
# Apply voicemode-local patches to the installed voice_mode package.
# Portable: works on Linux/WSL, macOS, and Windows (Git Bash/WSL).
#
# Usage: ./patches/apply.sh [venv-path]
#   venv-path defaults to the .venv in this repo directory.
#
# Anchors verified against voice-mode 8.12.0 (2026-09-05).
#
# HISTORY — what used to live here and why it is gone (see
# docs/superpowers/plans/artifacts/2026-09-05-patch-audit.md for the evidence):
#   patch_converse_cancel  -> fixed upstream in 8.12.0 (VM-2015)
#   patch_listen_stall     -> fixed upstream in 8.11.0 (AUDIO_STALL_TIMEOUT)
#   fcntl_shim/resource_shim -> superseded by upstream voice_mode/file_lock.py
#   voice_queue + patch_converse_queue -> superseded by upstream's conch queue
#                             (8.8.0 epic VM-1610). Upstream's is better: order
#                             is allocated under an flock'd counter rather than
#                             epoch-us, so it is correct across machines, and it
#                             ships callback mode, notify-on-give, fair
#                             promotion, an MCP tool and a CLI we never built.

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

PYBIN="$VENV_DIR/bin/python"
[ -x "$PYBIN" ] || PYBIN="$VENV_DIR/Scripts/python.exe"
[ -x "$PYBIN" ] || PYBIN="python3"

# Break hardlinks before patching (validated 2026-09-05).
#
# `uv pip install` populates a venv by HARDLINKING from ~/.cache/uv, so a file
# in site-packages typically has link count > 1 and is shared with the cache
# entry AND with every other venv built from it. Patching in place therefore
# writes through to all of them: measured 4 test venvs sharing inode 14004386
# with links=5, where patching one made a supposedly pristine venv report
# "already patched" and poisoned the cache for every future install.
#
# Replacing each target with a private copy first makes patching local to this
# venv. Cheap, idempotent, and a no-op when pip (which copies) was used instead.
unshare_file() {
    if [ -f "$1" ] && [ "$(stat -c %h "$1" 2>/dev/null || echo 1)" -gt 1 ]; then
        cp -p "$1" "$1.vmltmp" && mv -f "$1.vmltmp" "$1"
        echo "[patches] unshared hardlink: $1"
    fi
}
for _f in "$VM_DIR/tools/converse.py" "$VM_DIR/server.py" "$VM_DIR/simple_failover.py" \
          "$VM_DIR/control_channel.py"; do
    unshare_file "$_f"
done

# Never truncate active speech at listen_duration_max: once the user has
# started speaking, the listen window extends until the normal silence exit
# (bounded by VOICEMODE_LISTEN_OVERRUN).
#
# STILL NEEDED as of 8.12.0. Upstream's stall backstop (8.11.0) is explicitly
# NOT a length cap — converse.py:1462 says so — so the loop is still bounded by
# `recording_duration < max_duration` and a user still talking at 60s/120s is
# cut off mid-word. Upstreamed as docs/upstream/pr-listen-overrun.md.
if [ -f "$SCRIPT_DIR/patch_listen_overrun.py" ]; then
    "$PYBIN" "$SCRIPT_DIR/patch_listen_overrun.py" "$VM_DIR/tools/converse.py"
fi

# Force-exit voice-mode on shutdown so a mid-playback audio stream can't keep
# the process alive as an orphan holding its WSLg RDPSink sink-input — the cause
# of the "two streams mixing -> stutter + stale trailing audio" failure on WSL.
# (Paired with the reap logic in voicemode-mcp.)
#
# STILL NEEDED as of 8.12.0. Upstream's mcp_shutdown_patch.py (VM-2015) restores
# transport-close cancellation of in-flight handlers — a different problem.
# Nothing upstream force-exits when a lingering PortAudio thread holds the
# interpreter open.
if [ -f "$SCRIPT_DIR/patch_shutdown_abort.py" ]; then
    "$PYBIN" "$SCRIPT_DIR/patch_shutdown_abort.py" "$VM_DIR/server.py"
fi

# Remove the silent OpenAI voice swap: upstream maps a local voice (af_sky) to
# an OpenAI voice (nova) when it falls through to the OpenAI endpoint, so a
# Kokoro/Piper outage silently switches the user to a cloud voice mid-
# conversation. Policy here is "OpenAI last-resort, no silent swaps".
#
# STILL NEEDED as of 8.12.0 — simple_failover.py:84-97 still carries the
# voice_mapping table. Upstreamed as docs/upstream/pr-no-silent-voice-swap.md.
if [ -f "$SCRIPT_DIR/patch_simple_failover.py" ]; then
    "$PYBIN" "$SCRIPT_DIR/patch_simple_failover.py" "$VM_DIR/simple_failover.py"
fi

# Push-to-talk: a level-triggered *hold* on upstream's control channel.
#
# 8.11's control channel already covers two thirds of PTT -- skip_forward is
# press-to-barge-in AND short-press-to-end-turn. The missing third is the hold:
# mic open exactly while the key is down, with silence detection suppressed so
# a pause mid-thought doesn't end the turn. These two patches add a hold_start/
# hold_end intent pair mirroring the existing skip_forward pair (~100 lines),
# replacing the old 1,984-line patch_converse_ptt/patch_core_ptt approach.
#
# Order-independent of patch_listen_overrun (verified byte-identical either way):
# that one rewrites the `while (...)` header, these touch the init line above it
# and two sites in the body.
if [ -f "$SCRIPT_DIR/patch_control_hold.py" ]; then
    "$PYBIN" "$SCRIPT_DIR/patch_control_hold.py" "$VM_DIR/control_channel.py"
fi
if [ -f "$SCRIPT_DIR/patch_converse_hold.py" ]; then
    "$PYBIN" "$SCRIPT_DIR/patch_converse_hold.py" "$VM_DIR/tools/converse.py"
fi

echo "[patches] Done. Restart Claude Code for changes to take effect."
