#!/usr/bin/env python3
"""Stop voice-mode's log from saying voice will fail when ffmpeg is missing.

FELIX's setup does not install ffmpeg, and speech works without it (see patch_no_ffmpeg_gate.py). At
each start voice-mode still logged "Voice features will fail", so anyone reading the log to find a
real problem was told about one that does not exist. This replaces both warnings with one true line.

Idempotent. Stops setup if the lines it replaces no longer match voice-mode.
Verified against voice-mode 8.12.0.

Usage: patch_no_ffmpeg_warning.py <path-to-voice_mode/server.py>

Provenance: copied from felix-framework plugins/voicemode/patches/patch_no_ffmpeg_warning.py at commit
aea841ffd (branch feat/voice-opt-in). Changed here only to keep the file's own line
endings (CRLF stays CRLF); FELIX's writes LF whatever it reads. Same marker as FELIX's,
so either copy sees the other's work as done. voicemode-local applies it on native
Windows only (patches/manifest.windows.json).
"""
import sys
from pathlib import Path

MARKER = "FELIX: NO FFMPEG WARNING"
INDENT = " " * 8
OLD = (INDENT + 'logger.warning("FFmpeg is not installed - audio conversion features will not work")\n'
       + INDENT + 'logger.warning("Voice features will fail with helpful error messages")\n')
NEW = (INDENT + 'logger.info("FFmpeg is not installed; FELIX uses WAV audio, which does not need it")  # ' + MARKER + "\n")


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
        print(f"{target}: the ffmpeg warning was not found exactly once; voice-mode has changed", file=sys.stderr)
        return 1
    _write(target, source.replace(OLD, NEW), ending)
    return 0


if __name__ == "__main__":
    sys.exit(apply(Path(sys.argv[1])))
