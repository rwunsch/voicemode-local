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
  2. The name the user gave the Claude session, from Claude Code's
     ``~/.claude/sessions/<pid>.json`` (``nameSource`` "user"), matched by session id.
  3. ``~/.voicemode/session_names/<CLAUDE_CODE_SESSION_ID>.txt`` -- the label the
     agent writes at voice-mode start. The MCP server inherits the same session
     id at spawn, so it keys on its own environment with no plumbing.
  4. The current directory's basename -- a decent default (the repo name).
  5. ``"converse"`` -- upstream's value, if even the cwd is unavailable.

Every step is best-effort and can never raise into the voice path: a bad label
must not cost you a turn.

This restores a capability voicemode-local had in its own queue
(``voice_queue.session_project()``) and which upstream's conch does not provide.
Because the payload field already exists, the upstream change is genuinely this
small -- drafted as docs/upstream/pr-session-names.md.

Both sites (holder + queued waiter) are upstream PR #556; retire the env/cwd
steps once it merges and keep only the session_names/ file lookup.

Anchors verified against voice-mode 8.12.0 (2026-09-05; queue site 2026-10-01).
Idempotent; fails loudly on drift.

Usage: patch_session_name.py [<path-to-converse.py>]
"""
import sys
from pathlib import Path

MARKER = "voicemode-local session name"

# Second site: queued waiters are registered under the same hardcoded name, so
# `conch status` showed every *waiter* as "converse" even with the holder fixed.
# Own marker, so venvs patched before this site existed still pick it up.
QUEUE_MARKER = "voicemode-local queue name"

# Third step: the name the user gave the Claude session (/rename or a launch name), read
# from Claude Code's own ~/.claude/sessions/<pid>.json, matched by session id. Its own
# marker, so venvs patched earlier pick it up. Internal Claude Code file: any surprise falls
# through to the next step. Robert, 2026-11-05: the session's name when set, else the folder.
CLAUDE_NAME_MARKER = "voicemode-local claude session name"
A_CLAUDE_NAME = (
    "        sid = _os.getenv(\"CLAUDE_CODE_SESSION_ID\")\n"
    "        if sid:\n"
)
R_CLAUDE_NAME = (
    "        sid = _os.getenv(\"CLAUDE_CODE_SESSION_ID\")\n"
    "        try:  # voicemode-local claude session name\n"
    "            import json as _json\n"
    "            _cfg = _os.getenv(\"CLAUDE_CONFIG_DIR\") or _os.path.expanduser(\"~/.claude\")\n"
    "            _dir = _Path(_cfg) / \"sessions\"\n"
    "            _parent = _dir / f\"{_os.getppid()}.json\"\n"
    "            for _f in [_parent] + (sorted(_dir.glob(\"*.json\")) if sid else []):\n"
    "                try:\n"
    "                    _rec = _json.loads(_f.read_text())\n"
    "                except (OSError, ValueError):\n"
    "                    continue\n"
    "                if not isinstance(_rec, dict) or (sid and _rec.get(\"sessionId\") != sid):\n"
    "                    continue\n"
    "                _name = _rec.get(\"name\")\n"
    "                if _rec.get(\"nameSource\") == \"user\" and isinstance(_name, str) and _name.strip():\n"
    "                    return _name.strip()[:64]\n"
    "                break\n"
    "        except Exception:  # noqa: BLE001\n"
    "            pass\n"
    "        if sid:\n"
)
A_QUEUE = (
    "                        queue_session_id,\n"
    "                        agent=\"converse\",\n"
)
R_QUEUE = (
    "                        queue_session_id,\n"
    "                        agent=_vml_session_name(),  # voicemode-local queue name\n"
)

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

    Order: VOICEMODE_SESSION_NAME env > the Claude session's user-set name >
    ~/.voicemode/session_names/<session id>.txt > cwd basename > "converse". Never raises -- a label is a
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


def _read(target: Path):
    """The file as LF text, and whether it used CRLF. UTF-8 whatever the locale."""
    text = target.read_bytes().decode("utf-8")
    return text.replace("\r\n", "\n"), "\r\n" in text


def _write(target: Path, text: str, crlf: bool) -> None:
    """Write UTF-8 in the file's own line endings."""
    target.write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))


def apply(target: Path) -> int:
    src, crlf = _read(target)
    if MARKER in src and QUEUE_MARKER in src and CLAUDE_NAME_MARKER in src:
        print(f"  already patched: {target}")
        return 0

    out = src
    if MARKER not in out:
        for name, anchor in (("conch construction", A_CONCH), ("helper anchor", A_HELPER)):
            count = out.count(anchor)
            if count != 1:
                print(
                    f"ANCHOR DRIFT: '{name}' matched {count} times (expected 1) in "
                    f"{target}. Upstream converse.py changed — update "
                    f"patches/patch_session_name.py.",
                    file=sys.stderr,
                )
                return 1

        out = out.replace(A_CONCH, R_CONCH, 1)
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

    if QUEUE_MARKER not in out:
        count = out.count(A_QUEUE)
        if count != 1:
            print(
                f"ANCHOR DRIFT: 'queue register' matched {count} times (expected 1) in "
                f"{target}. Upstream converse.py changed (has PR #556 merged?) — "
                f"update patches/patch_session_name.py.",
                file=sys.stderr,
            )
            return 1
        out = out.replace(A_QUEUE, R_QUEUE, 1)

    if CLAUDE_NAME_MARKER not in out and "\ndef _vml_session_name() -> str:" in out:
        count = out.count(A_CLAUDE_NAME)
        if count != 1:
            print(
                f"ANCHOR DRIFT: 'claude session name' matched {count} times (expected 1) in "
                f"{target}. Update patches/patch_session_name.py.",
                file=sys.stderr,
            )
            return 1
        out = out.replace(A_CLAUDE_NAME, R_CLAUDE_NAME, 1)

    compile(out, str(target), "exec")  # syntax safety net before writing
    _write(target, out, crlf)
    print(f"  patched (session name): {target}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        target = Path(sys.argv[1])
    else:
        import voice_mode
        target = Path(voice_mode.__file__).parent / "tools" / "converse.py"
    sys.exit(apply(target))
