#!/usr/bin/env python3
"""Never delete a temp audio file while it is still open (native Windows).

voice-mode writes each TTS reply to a NamedTemporaryFile(delete=False) and deletes it with
os.unlink inside the `with` that still holds it open. Linux allows that. Windows refuses
with WinError 32 ("being used by another process"), the exception escapes text_to_speech,
and the failover reports "TTS service connection failed": after the audio has already
played on the success path, and before listening starts. Every spoken turn failed this way
in the first native Windows test (2026-11-06, n=1 run, 3 of 3 turns). The STT side already
closes first (upstream's "Windows fix ... Issue #135"); the TTS side never got it, and
9.0.2 still has all six sites.

core.py: the six os.unlink(tmp_file.name) calls become _vml_close_unlink(tmp_file), which
closes the handle and then deletes, and never raises: a leftover temp file is better than a
failed turn.
streaming.py: the two save-a-copy paths close tmp_wav before deleting it.

Idempotent. Stops setup if the lines it replaces are not found the expected number of times.
Verified against voice-mode 8.12.0, pristine and after the native patch list. Keeps the
file's own line endings. voicemode-local applies it on native Windows only
(patches/manifest.windows.json); WSL and Mac never hit the error.

Usage: patch_windows_tempfile.py <path to core.py or streaming.py>
"""
import sys
from pathlib import Path

MARKER = "VML: WINDOWS TEMPFILE"
HELPER = (
    "def _vml_close_unlink(handle):  # " + MARKER + "\n"
    '    """Close a temp file, then delete it. Windows cannot delete an open file."""\n'
    "    try:\n"
    "        handle.close()\n"
    "        os.unlink(handle.name)\n"
    "    except OSError:\n"
    "        pass\n"
)
CORE_ANCHOR = 'logger = logging.getLogger("voicemode")\n'
CORE_OLD, CORE_NEW, CORE_COUNT = "os.unlink(tmp_file.name)", "_vml_close_unlink(tmp_file)", 6
STREAM_OLD = "os.unlink(tmp_wav.name)\n"
STREAM_NEW = "tmp_wav.close(); os.unlink(tmp_wav.name)  # " + MARKER + "\n"
STREAM_COUNT = 2


def _read(target):
    raw = target.read_bytes().decode("utf-8")
    return raw.replace("\r\n", "\n"), ("\r\n" if "\r\n" in raw else "\n")


def _write(target, text, ending):
    target.write_bytes(text.replace("\n", ending).encode("utf-8"))


def _fail(target, what):
    print(f"{target}: {what}; voice-mode has changed", file=sys.stderr)
    return 1


def apply(target):
    source, ending = _read(target)
    if MARKER in source:
        return 0
    if target.name == "core.py":
        if source.count(CORE_ANCHOR) != 1:
            return _fail(target, "the logger line was not found exactly once")
        if source.count(CORE_OLD) != CORE_COUNT:
            return _fail(target, f"expected {CORE_COUNT} temp-file deletes, found {source.count(CORE_OLD)}")
        source = source.replace(CORE_OLD, CORE_NEW)
        source = source.replace(CORE_ANCHOR, CORE_ANCHOR + "\n\n" + HELPER)
    elif target.name == "streaming.py":
        if source.count(STREAM_OLD) != STREAM_COUNT:
            return _fail(target, f"expected {STREAM_COUNT} temp-file deletes, found {source.count(STREAM_OLD)}")
        source = source.replace(STREAM_OLD, STREAM_NEW)
    else:
        return _fail(target, "only core.py and streaming.py are patched")
    _write(target, source, ending)
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: patch_windows_tempfile.py <path to core.py or streaming.py>", file=sys.stderr)
        sys.exit(2)
    sys.exit(apply(Path(sys.argv[1])))
