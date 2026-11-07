#!/usr/bin/env python3
"""The session label's folder fallback: the client's folder, not voice-mode's (native Windows).

The native launcher starts voice-mode in its runtime folder, because upstream loads every
.voicemode/voicemode.env it finds walking up from its working folder: started from a WSL
checkout it read the WSL setup's file (2026-11-06). The label rule (Robert: the Claude
session's name, else the folder name) then needs the folder the client was in, which the
launcher passes as VOICEMODE_CLIENT_FOLDER. Without it, the old cwd fallback holds.

Runs after patch_session_name.py, whose line it changes. Idempotent; stops setup if that
line is missing. Keeps the file's line endings. Native Windows list only.

Usage: patch_native_client_folder.py <path to voice_mode/tools/converse.py>
"""
import sys
from pathlib import Path

MARKER = "VML: NATIVE CLIENT FOLDER"
OLD = "        cwd = _Path(_os.getcwd()).name\n"
NEW = ('        cwd = _Path(_os.environ.get("VOICEMODE_CLIENT_FOLDER") or _os.getcwd()).name'
       "  # " + MARKER + "\n")


def apply(target):
    raw = target.read_bytes().decode("utf-8")
    ending = "\r\n" if "\r\n" in raw else "\n"
    source = raw.replace("\r\n", "\n")
    if MARKER in source:
        return 0
    if source.count(OLD) != 1:
        print(f"{target}: the session label's cwd line was not found exactly once; "
              "patch_session_name.py must run first", file=sys.stderr)
        return 1
    target.write_bytes(source.replace(OLD, NEW).replace("\n", ending).encode("utf-8"))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: patch_native_client_folder.py <path to converse.py>", file=sys.stderr)
        sys.exit(2)
    sys.exit(apply(Path(sys.argv[1])))
