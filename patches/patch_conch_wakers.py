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

Usage: patch_conch_wakers.py <path to tools/converse.py, conch_notify.py, conch_queue.py or conch.py>
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

# conch_queue.py: 1) ping a lone callback grantee (upstream pings only the ones it skips),
# 2) mark a `conch give`, 3) a present wait waiter passes an absent callback grant.
QUEUE_PAIRS = [
    ('                "granted_at": datetime.now().isoformat(),\n'
     "            },\n"
     "        )\n"
     "        return target\n",
     '                "granted_at": datetime.now().isoformat(),\n'
     "            },\n"
     "        )\n"
     "        if getattr(target, \"mode\", \"wait\") == \"callback\":  # " + MARKER + "\n"
     "            cls._notify_callback(target, block=notify_block)  # upstream pings only skipped callbacks\n"
     "        return target\n"),
    ("                        # VM-1967 safety net -- see grant_next().\n"
     '                        "granted_at": datetime.now().isoformat(),\n',
     "                        # VM-1967 safety net -- see grant_next().\n"
     '                        "granted_at": datetime.now().isoformat(),\n'
     '                        "given": True,  # VML: an explicit give holds for CONCH_GRANT_TTL\n'),
    ("                if cls._grant_wedged(g, e):\n"
     "                    cls.deregister(sid)\n"
     "                    cls.grant_next(notify_block=False)\n"
     "                    return cls._current_grant()\n"
     "                return g\n",
     "                if cls._grant_wedged(g, e):\n"
     "                    cls.deregister(sid)\n"
     "                    cls.grant_next(notify_block=False)\n"
     "                    return cls._current_grant()\n"
     "                try:  # VML: present beats absent: a woken callback waiter past its grace\n"
     "                    from voice_mode import conch_wakers as _vml_cw\n"
     "                    if _vml_cw.grant_is_absent(g, e, ttl=_get_grant_ttl()):\n"
     "                        _vml_next = next((w for w in cls._scan(clean=True) if w.mode == \"wait\"), None)\n"
     "                        if _vml_next is not None:  # it keeps its place; the wait waiter talks\n"
     "                            _vml_g = {\"session_id\": _vml_next.session_id, \"seq\": _vml_next.seq,\n"
     "                                      \"granted_at\": datetime.now().isoformat()}\n"
     "                            cls._atomic_write_json(gf, _vml_g)\n"
     "                            cls._notify_callback(e, block=False)\n"
     "                            return _vml_g\n"
     "                except Exception:\n"
     "                    pass\n"
     "                return g\n"),
]

# conch.py: a free mic is served past an absent callback grant (nobody present is in wait
# mode, else _current_grant above has already moved the grant to them).
CONCH_PAIRS = [
    ("        return grantee is not None and grantee != self.session_id\n",
     "        if grantee is not None and grantee != self.session_id:\n"
     "            try:  # " + MARKER + "\n"
     "                from voice_mode import conch_wakers as _vml_cw\n"
     "                from voice_mode.conch_queue import _get_grant_ttl as _vml_ttl\n"
     "                _g = ConchQueue._current_grant()\n"
     "                for _e in ConchQueue.list():\n"
     "                    if _g and _e.session_id == _g.get(\"session_id\") and \\\n"
     "                            _vml_cw.grant_is_absent(_g, _e, ttl=_vml_ttl()):\n"
     "                        return False\n"
     "            except Exception:\n"
     "                pass\n"
     "        return grantee is not None and grantee != self.session_id\n"),
]


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
        pairs = [(CONVERSE_ANCHOR, CONVERSE_ANCHOR + CONVERSE_ADD)]
    elif target.name == "conch_notify.py":
        pairs = [(NOTIFY_ANCHOR, NOTIFY_ANCHOR + NOTIFY_ADD)]
    elif target.name == "conch_queue.py":
        pairs = QUEUE_PAIRS
    elif target.name == "conch.py":
        pairs = CONCH_PAIRS
    else:
        return _fail(target, "only tools/converse.py, conch_notify.py, conch_queue.py and conch.py are patched")
    for anchor, _ in pairs:
        if source.count(anchor) != 1:
            return _fail(target, f"an anchor was found {source.count(anchor)} times, not once")
    for anchor, replacement in pairs:
        source = source.replace(anchor, replacement)
    compile(source, str(target), "exec")
    _write(target, source, ending)
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: patch_conch_wakers.py <path to tools/converse.py, conch_notify.py, conch_queue.py or conch.py>",
              file=sys.stderr)
        sys.exit(2)
    sys.exit(apply(Path(sys.argv[1])))
