#!/usr/bin/env python3
"""Let voice-mode's `converse` run without ffmpeg, on a Mac and on Windows.

voice-mode refuses every conversation when ffmpeg is not on PATH. FELIX's setup does not install
ffmpeg: the speech service answers with WAV, FELIX sets VOICEMODE_STT_AUDIO_FORMAT=wav, and
whisper-server runs without its --convert option, so nothing on that path needs it. That saves a
third-party program of over 100 MB (plan 2026-11-04-voice-opt-in.md, D15).

Idempotent. Stops setup if the line it replaces no longer matches voice-mode.
Verified against voice-mode 8.12.0.

Usage: patch_no_ffmpeg_gate.py <path-to-voice_mode/tools/converse.py>

Provenance: copied from felix-framework plugins/voicemode/patches/patch_no_ffmpeg_gate.py at commit
aea841ffd (branch feat/voice-opt-in). Changed here only to keep the file's own line
endings (CRLF stays CRLF); FELIX's writes LF whatever it reads. Same marker as FELIX's,
so either copy sees the other's work as done. voicemode-local applies it on native
Windows only (patches/manifest.windows.json).
"""
import sys
from pathlib import Path

MARKER = "FELIX: NO FFMPEG"
OLD = "ffmpeg_available = getattr(voice_mode.config, 'FFMPEG_AVAILABLE', True)  # Default to True if not set"
NEW = "ffmpeg_available = True  # " + MARKER + " (WAV only, see FELIX's patch_no_ffmpeg_gate.py)"


def _read(target):
    """The text with LF endings, and the ending the file had."""
    raw = target.read_bytes().decode("utf-8")
    return raw.replace("\r\n", "\n"), ("\r\n" if "\r\n" in raw else "\n")


def _write(target, text, ending):
    target.write_bytes(text.replace("\n", ending).encode("utf-8"))


def apply(target):
    source, ending = _read(target)
    if MARKER in source:
        return 0
    if source.count(OLD) != 1:
        print(f"{target}: the ffmpeg check was not found exactly once; voice-mode has changed", file=sys.stderr)
        return 1
    _write(target, source.replace(OLD, NEW), ending)
    return 0


if __name__ == "__main__":
    sys.exit(apply(Path(sys.argv[1])))
