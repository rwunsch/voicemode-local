#!/usr/bin/env python3
"""Patch voice_mode/server.py to force prompt termination on shutdown.

Root cause of the WSL "orphaned playback stream" stutter
========================================================
voice-mode is an stdio MCP server: Claude Code shuts it down (on reconnect,
config reload, or after a cancelled converse) by closing stdin / signalling it.
`mcp.run(transport="stdio")` then returns and main() falls off the end — and the
process is *supposed* to exit.

But if a converse was mid-playback, a sounddevice/PortAudio OutputStream is still
open (its host thread alive), and on WSL that stream is wired through WSLg's
`module-rdp-sink`, which buffers ~1s+ of audio. The lingering audio thread keeps
the interpreter alive after mcp.run() returns, so the old process does NOT die —
it becomes an orphan still holding its PulseAudio sink-input. When Claude starts
the replacement instance, a SECOND stream opens; the two mix in the deep RDP
buffer and you hear stutter plus "stale" trailing audio that survives stopping
voicemode.

The fix: wrap mcp.run() so that the instant it returns OR raises (both shutdown
paths — stdin EOF and SIGINT/SIGTERM), we os._exit(0). That bypasses any
still-running audio thread, terminates immediately, and releases the sink-input,
so no orphan can form. mcp.run() is the last statement in main(); nothing of
value runs after it, so the hard exit loses nothing.

This is layer 1 of the defense; the voicemode-mcp wrapper also reaps a stale
same-session voice-mode on launch (layer 2) as a backstop.

The replacement is anchored on the single `mcp.run(transport="stdio")` line.
Idempotent; fails loudly if the anchor drifts. Verified against voice-mode 8.7.1.

Usage: patch_shutdown_abort.py [<path-to-server.py>]
"""
import sys
from pathlib import Path

MARKER = "force-exit on shutdown (voicemode-local)"

ANCHOR = '    mcp.run(transport="stdio")\n'
REPLACE = (
    "    # force-exit on shutdown (voicemode-local): the instant mcp.run()\n"
    "    # returns (stdin EOF) or is signalled, terminate hard so a still-open\n"
    "    # audio OutputStream can't keep this process alive as an orphan holding\n"
    "    # its WSLg RDPSink sink-input (which then mixes stale audio into the next\n"
    "    # instance -> stutter). mcp.run() is the last statement in main(), so the\n"
    "    # hard exit loses nothing. See patch_shutdown_abort.py for the full why.\n"
    "    try:\n"
    '        mcp.run(transport="stdio")\n'
    "    finally:\n"
    "        import os as _vml_os\n"
    "        _vml_os._exit(0)\n"
)


def apply(target: Path) -> int:
    src = target.read_text()
    if MARKER in src:
        print(f"  already patched: {target}")
        return 0
    if src.count(ANCHOR) != 1:
        print(f"ANCHOR DRIFT: 'mcp.run(transport=\"stdio\")' matched "
              f"{src.count(ANCHOR)} times (expected 1) in {target}. Upstream "
              f"server.py changed — update patch_shutdown_abort.py.",
              file=sys.stderr)
        return 1
    out = src.replace(ANCHOR, REPLACE, 1)
    compile(out, str(target), "exec")
    target.write_text(out)
    print(f"  patched (force-exit on shutdown): {target}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        target = Path(sys.argv[1])
    else:
        import voice_mode
        target = Path(voice_mode.__file__).parent / "server.py"
    sys.exit(apply(target))
