#!/usr/bin/env python3
"""Stop voice-mode's log from saying voice will fail when ffmpeg is missing.

FELIX's setup does not install ffmpeg, and speech works without it (see patch_no_ffmpeg_gate.py). At
each start voice-mode still logged "Voice features will fail", so anyone reading the log to find a
real problem was told about one that does not exist. This replaces both warnings with one true line.

Idempotent. Stops setup if the lines it replaces no longer match voice-mode.
Verified against voice-mode 8.12.0.

Usage: patch_no_ffmpeg_warning.py <path-to-voice_mode/server.py>
"""
import sys
from pathlib import Path

MARKER = "FELIX: NO FFMPEG WARNING"
INDENT = " " * 8
OLD = (INDENT + 'logger.warning("FFmpeg is not installed - audio conversion features will not work")\n'
       + INDENT + 'logger.warning("Voice features will fail with helpful error messages")\n')
NEW = (INDENT + 'logger.info("FFmpeg is not installed; FELIX uses WAV audio, which does not need it")  # ' + MARKER + "\n")


def apply(target):
    source = target.read_text(encoding="utf-8")
    if MARKER in source:
        return 0
    if source.count(OLD) != 1:
        print(f"{target}: the ffmpeg warning was not found exactly once; voice-mode has changed", file=sys.stderr)
        return 1
    target.write_text(source.replace(OLD, NEW), encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(apply(Path(sys.argv[1])))
