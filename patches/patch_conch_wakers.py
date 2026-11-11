#!/usr/bin/env python3
"""Per-harness wakers for the conch queue's callback mode (see conch_wakers.py).

tools/converse.py: a requested conch_mode="callback" is served as "wait" when no waker
can reach this session (conch_wakers.waker_for), so an unwakeable session never holds
a callback grant that blocks the queue. Logged once per call.

conch_notify.py: notify_granted tries conch_wakers.deliver (harness command, Claude
Code's session socket) before upstream's own routes; on the release hot path
(block=False) it runs on upstream's daemon thread, as upstream's own push does.

Idempotent. Stops setup if an anchor is not found exactly once. Verified against
voice-mode 8.12.0, pristine and after the rest of the patch list. Keeps the file's own
line endings.

Usage: patch_conch_wakers.py <path to tools/converse.py or conch_notify.py>
"""
import sys
from pathlib import Path

MARKER = "VML: CONCH WAKERS"

CONVERSE_ANCHOR = (
    '    if resolved_conch_mode not in ("wait", "callback"):\n'
    '        resolved_conch_mode = "wait"\n'
)
CONVERSE_ADD = (
    "    if resolved_conch_mode == \"callback\":  # " + MARKER + "\n"
    "        # Upstream pushes a callback grantee only through tmux; with no waker for\n"
    "        # this harness the grant would block every other session. Serve it as wait.\n"
    "        try:\n"
    "            from voice_mode import conch_wakers as _vml_cw\n"
    "            _vml_waker = _vml_cw.waker_for(os.getpid())\n"
    "        except Exception:\n"
    "            _vml_waker = None\n"
    "        if _vml_waker is None:\n"
    "            logger.info(\"conch: no waker can reach this session; conch_mode=callback is served as wait\")\n"
    "            resolved_conch_mode = \"wait\"\n"
)

NOTIFY_ANCHOR = (
    '    if getattr(entry, "mode", "wait") != "callback":\n'
    "        return\n"
)
NOTIFY_ADD = (
    "    try:  # " + MARKER + ": our per-harness wakers first, upstream's routes after\n"
    "        from voice_mode import conch_wakers as _vml_cw\n"
    "    except Exception:\n"
    "        _vml_cw = None\n"
    "    if _vml_cw is not None:\n"
    "        if not block:\n"
    "            _dispatch_async(_vml_cw.deliver_then, entry, _local_nudge)\n"
    "            return\n"
    "        if _vml_cw.deliver(entry):\n"
    "            return\n"
)


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
    if target.name == "converse.py":
        anchor, add = CONVERSE_ANCHOR, CONVERSE_ADD
    elif target.name == "conch_notify.py":
        anchor, add = NOTIFY_ANCHOR, NOTIFY_ADD
    else:
        return _fail(target, "only tools/converse.py and conch_notify.py are patched")
    if source.count(anchor) != 1:
        return _fail(target, f"the anchor was found {source.count(anchor)} times, not once")
    _write(target, source.replace(anchor, anchor + add), ending)
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: patch_conch_wakers.py <path to tools/converse.py or conch_notify.py>",
              file=sys.stderr)
        sys.exit(2)
    sys.exit(apply(Path(sys.argv[1])))
