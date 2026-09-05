#!/usr/bin/env python3
"""Give each voice session a distinguishable name in the conch.

The gap
=======
Upstream's conch payload already carries the right fields -- ``agent``,
``session_id``, ``project_path``, ``voice`` (``Conch._payload``). But
``converse.py`` constructs the Conch with a hardcoded ``agent_name="converse"``,
so every concurrent session reports the same name and ``voicemode conch status``
cannot tell them apart:

    conch = Conch(  # Named for event logging
        agent_name="converse",
        ...

For a single agent that is fine. For the multi-session workflow the conch queue
exists to serve -- several Claude Code sessions taking FIFO turns on one mic --
it is the one thing that makes the queue readable. "Who holds the floor?"
answers "converse", three times.

The fix
=======
Resolve a real label, in priority order:

  1. ``VOICEMODE_SESSION_NAME`` -- explicit launch-time override, always wins.
  2. ``~/.voicemode/session_names/<CLAUDE_CODE_SESSION_ID>.txt`` -- the label the
     agent writes at voice-mode start. The MCP server inherits the same session
     id at spawn, so it keys on its own environment with no plumbing.
  3. The current directory's basename -- a decent default (the repo name).
  4. ``"converse"`` -- upstream's value, if even the cwd is unavailable.

Every step is best-effort and can never raise into the voice path: a bad label
must not cost you a turn.

This restores a capability voicemode-local had in its own queue
(``voice_queue.session_project()``) and which upstream's conch does not provide.
Because the payload field already exists, the upstream change is genuinely this
small -- drafted as docs/upstream/pr-session-names.md.

Anchors verified against voice-mode 8.12.0 (2026-09-05).
Idempotent; fails loudly on drift.

Usage: patch_session_name.py [<path-to-converse.py>]
"""
import sys
from pathlib import Path

MARKER = "voicemode-local session name"

A_CONCH = (
    "    conch = Conch(  # Named for event logging\n"
    "        agent_name=\"converse\",\n"
)
R_CONCH = (
    "    conch = Conch(  # Named for event logging\n"
    "        agent_name=_vml_session_name(),  # voicemode-local session name\n"
)

# Helper is inserted just above the function that builds the Conch. Anchored on
# the DEBUG block that opens it, which is unique in the file.
A_HELPER = (
    "    if DEBUG:\n"
    "        # psutil, not resource.getrusage: the resource module is Unix-only\n"
    "        start_memory = psutil.Process().memory_info().rss // 1024\n"
    "        logger.debug(f\"Starting converse - Memory: {start_memory} KB\")\n"
)
R_HELPER = (
    "    if DEBUG:\n"
    "        # psutil, not resource.getrusage: the resource module is Unix-only\n"
    "        start_memory = psutil.Process().memory_info().rss // 1024\n"
    "        logger.debug(f\"Starting converse - Memory: {start_memory} KB\")\n"
)

HELPER_SRC = '''

def _vml_session_name() -> str:
    """Resolve a human-meaningful name for this session's conch entry.

    voicemode-local session name. Upstream hardcodes agent_name="converse", so
    concurrent sessions are indistinguishable in `voicemode conch status`.

    Order: VOICEMODE_SESSION_NAME env > ~/.voicemode/session_names/<session
    id>.txt > cwd basename > "converse". Never raises -- a label is a
    convenience and must not cost the caller a turn.
    """
    import os as _os
    from pathlib import Path as _Path
    try:
        name = _os.getenv("VOICEMODE_SESSION_NAME")
        if name and name.strip():
            return name.strip()[:64]
        sid = _os.getenv("CLAUDE_CODE_SESSION_ID")
        if sid:
            base = _Path(_os.path.expanduser("~/.voicemode")) / "session_names"
            try:
                label = (base / f"{sid}.txt").read_text().strip()
                if label:
                    return label[:64]
            except OSError:
                pass
        cwd = _Path(_os.getcwd()).name
        if cwd:
            return cwd[:64]
    except Exception:  # noqa: BLE001 - a label must never break the voice path
        pass
    return "converse"

'''


def apply(target: Path) -> int:
    src = target.read_text()
    if MARKER in src:
        print(f"  already patched: {target}")
        return 0

    for name, anchor in (("conch construction", A_CONCH), ("helper anchor", A_HELPER)):
        count = src.count(anchor)
        if count != 1:
            print(
                f"ANCHOR DRIFT: '{name}' matched {count} times (expected 1) in "
                f"{target}. Upstream converse.py changed — update "
                f"patches/patch_session_name.py.",
                file=sys.stderr,
            )
            return 1

    out = src.replace(A_CONCH, R_CONCH, 1)
    # Insert the helper at module level, immediately before the function that
    # contains the Conch construction. Find the enclosing `async def`/`def` line.
    idx = out.index(R_CONCH)
    head = out[:idx]
    fn_start = max(head.rfind("\nasync def "), head.rfind("\ndef "))
    if fn_start == -1:
        print(
            f"ANCHOR DRIFT: could not locate the enclosing function for the "
            f"Conch construction in {target}.",
            file=sys.stderr,
        )
        return 1
    out = out[:fn_start] + "\n" + HELPER_SRC.rstrip("\n") + "\n" + out[fn_start:]

    compile(out, str(target), "exec")  # syntax safety net before writing
    target.write_text(out)
    print(f"  patched (session name): {target}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        target = Path(sys.argv[1])
    else:
        import voice_mode
        target = Path(voice_mode.__file__).parent / "tools" / "converse.py"
    sys.exit(apply(target))
