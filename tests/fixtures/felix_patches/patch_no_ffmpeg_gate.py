#!/usr/bin/env python3
"""Let voice-mode's `converse` run without ffmpeg, on a Mac and on Windows.

voice-mode refuses every conversation when ffmpeg is not on PATH. FELIX's setup does not install
ffmpeg: the speech service answers with WAV, FELIX sets VOICEMODE_STT_AUDIO_FORMAT=wav, and
whisper-server runs without its --convert option, so nothing on that path needs it. That saves a
third-party program of over 100 MB (plan 2026-11-04-voice-opt-in.md, D15).

Idempotent. Stops setup if the line it replaces no longer matches voice-mode.
Verified against voice-mode 8.12.0.

Usage: patch_no_ffmpeg_gate.py <path-to-voice_mode/tools/converse.py>
"""
import sys
from pathlib import Path

MARKER = "FELIX: NO FFMPEG"
OLD = "ffmpeg_available = getattr(voice_mode.config, 'FFMPEG_AVAILABLE', True)  # Default to True if not set"
NEW = "ffmpeg_available = True  # " + MARKER + " (WAV only, see FELIX's patch_no_ffmpeg_gate.py)"


def apply(target):
    source = target.read_text(encoding="utf-8")
    if MARKER in source:
        return 0
    if source.count(OLD) != 1:
        print(f"{target}: the ffmpeg check was not found exactly once; voice-mode has changed", file=sys.stderr)
        return 1
    target.write_text(source.replace(OLD, NEW), encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(apply(Path(sys.argv[1])))
