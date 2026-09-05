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

# Keep the queue floor alive while audio plays on the PortAudio callback thread.
#
# UNDER REVIEW as of 8.12.0: upstream's _wait_for_player_with_control is now an
# async poll loop rather than a blocking executor wait, so the event-loop
# starvation this patched around is largely gone — and the heartbeat it served
# belonged to our now-deleted queue. Kept guarded until the conch hold-TTL
# question in the audit doc is settled.
if [ -f "$SCRIPT_DIR/patch_audio_keepalive.py" ]; then
    "$PYBIN" "$SCRIPT_DIR/patch_audio_keepalive.py" "$VM_DIR/core.py"
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

echo "[patches] Done. Restart Claude Code for changes to take effect."
