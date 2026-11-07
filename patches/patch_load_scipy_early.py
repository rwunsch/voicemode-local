#!/usr/bin/env python3
"""Load scipy.signal when voice-mode starts, not during recording.

voice-mode imports it inside the loop that reads the microphone. In a connector on native Windows that
import stalls mid-recording, so no sound is processed and recording never stops on silence: one chunk
was processed in 169 s (laptop, 2026-11-04, n=1). Upstream issue mbailey/voicemode#545 reports the same
on voice-mode 8.12.0, with loading it first as the workaround.

It stalls on native Windows and costs a Mac nothing, so both run the same patched code.

Idempotent. Stops setup if the line it follows no longer matches voice-mode.
Verified against voice-mode 8.12.0.

Usage: patch_load_scipy_early.py <path-to-voice_mode/tools/converse.py>

Provenance: copied from felix-framework plugins/voicemode/patches/patch_load_scipy_early.py at commit
aea841ffd (branch feat/voice-opt-in). Changed here only to keep the file's own line
endings (CRLF stays CRLF); FELIX's writes LF whatever it reads. Same marker as FELIX's,
so either copy sees the other's work as done. voicemode-local applies it on native
Windows only (patches/manifest.windows.json).
"""
import sys
from pathlib import Path

MARKER = "FELIX: LOAD SCIPY EARLY"
ANCHOR = "from scipy.io.wavfile import write\n"
NEW = ANCHOR + "from scipy import signal as _felix_early_scipy_signal  # " + MARKER + " (voicemode#545)\n"


def apply(target):
    source = target.read_bytes().decode("utf-8")
    if MARKER in source:
        return 0
    normalised = source.replace("\r\n", "\n")
    if normalised.count(ANCHOR) != 1:
        print(f"{target}: the scipy import line was not found exactly once; voice-mode has changed", file=sys.stderr)
        return 1
    ending = "\r\n" if "\r\n" in source else "\n"
    target.write_bytes(normalised.replace(ANCHOR, NEW).replace("\n", ending).encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(apply(Path(sys.argv[1])))
