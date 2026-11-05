#!/bin/bash
# Apply voicemode-local patches to the installed voice_mode package.
#
# Usage: ./patches/apply.sh [venv-path]
#   venv-path defaults to the .venv in this repo directory.
#
# The work is done by patches/apply.py, in the order patches/manifest.json lists:
# the hard-link break, every patch run and every copied module. This wrapper only
# picks the venv's Python, as it always did, so callers need not change. apply.py
# was checked byte-identical to the shell version it replaced
# (tests/test_apply_py.py); docs/patching/README.md says why each step is there.
#
# HISTORY — what used to live here and why it is gone (see
# docs/superpowers/plans/artifacts/2026-09-05-patch-audit.md for the evidence):
#   patch_converse_cancel  -> fixed upstream in 8.12.0 (VM-2015)
#   patch_listen_stall     -> fixed upstream in 8.11.0 (AUDIO_STALL_TIMEOUT)
#   fcntl_shim/resource_shim -> superseded by upstream voice_mode/file_lock.py
#   patch_shutdown_abort   -> replaced 2026-10-01 by patch_turns_playback_stop
#                             (force-exit after mcp.run() could not reach a
#                             wait that happens inside mcp.run(); see #554)
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

PYBIN="$VENV_DIR/bin/python"
[ -x "$PYBIN" ] || PYBIN="$VENV_DIR/Scripts/python.exe"
[ -x "$PYBIN" ] || PYBIN="python3"

exec "$PYBIN" "$SCRIPT_DIR/apply.py" "$VENV_DIR"
