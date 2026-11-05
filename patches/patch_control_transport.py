#!/usr/bin/env python3
"""Give voice_mode/control_socket.py a named-pipe route where AF_UNIX is missing.

Why this exists
===============
Windows Python has no ``socket.AF_UNIX``. Upstream's listener creates its
server with ``socket.socket(socket.AF_UNIX, ...)`` (``_bind``), so with the
control channel enabled natively that line raises ``AttributeError``.
``start_control_listener`` only catches ``OSError``, and converse calls it
outside its ``try``, so the error escapes and the turn is lost. The two CLI
client paths (``send_control_command``, ``query_status``) fail the same way.

What it changes
===============
Only when ``socket.AF_UNIX`` is missing:

* ``_bind`` returns ``control_transport.create_server(path)`` -- an owner-only
  named pipe offering the socket methods the accept loop and handlers use --
  instead of a Unix socket;
* ``_unlink_socket`` does nothing (a pipe leaves no file);
* both clients connect through ``control_transport.connect(path, timeout)``,
  which refuses a pipe not owned by the current user.

A missing ``control_transport`` module turns into ``OSError``, which
``start_control_listener`` already treats as non-fatal. Where AF_UNIX exists
the code paths are the original ones: the transport is not even imported.

Anchors verified against voice-mode 8.12.0 (2026-11-05), pristine and after
patch_voice_barge / patch_voice_pill (which touch only the status payload).
Idempotent; fails loudly on drift.

Usage: patch_control_transport.py [<path-to-control_socket.py>]
"""
import sys
from pathlib import Path

MARKER = "voicemode-local control transport"

# --- 1. listener: bind a pipe when there is no AF_UNIX ----------------------
A_BIND = (
    "        path = self._socket_path\n"
    "        path.parent.mkdir(parents=True, exist_ok=True)\n"
)
R_BIND = (
    "        path = self._socket_path\n"
    f"        # {MARKER}: no AF_UNIX (Windows) -> an owner-only named pipe.\n"
    "        if not hasattr(socket, \"AF_UNIX\"):\n"
    "            server = _control_transport().create_server(path)\n"
    "            server.settimeout(_POLL_TIMEOUT)\n"
    "            return server\n"
    "        path.parent.mkdir(parents=True, exist_ok=True)\n"
)

# --- 2. listener: nothing to unlink for a pipe ------------------------------
A_UNLINK = (
    "        path = str(self._socket_path)\n"
    "        try:\n"
    "            st = os.lstat(path)\n"
)
R_UNLINK = (
    f"        if not hasattr(socket, \"AF_UNIX\"):  # {MARKER}: a pipe leaves no file\n"
    "            return\n"
    "        path = str(self._socket_path)\n"
    "        try:\n"
    "            st = os.lstat(path)\n"
)

# --- 3. client helpers -------------------------------------------------------
A_HELPERS = "def send_control_command(\n"
R_HELPERS = (
    f"def _control_transport():\n"
    f"    \"\"\"{MARKER}: the named-pipe module, or OSError if not installed.\"\"\"\n"
    "    try:\n"
    "        from voice_mode import control_transport\n"
    "    except ImportError as exc:\n"
    "        raise OSError(\n"
    "            f\"control channel needs AF_UNIX or voice_mode.control_transport: {exc}\"\n"
    "        ) from exc\n"
    "    return control_transport\n"
    "\n"
    "\n"
    "def _control_connect(path, timeout):\n"
    f"    \"\"\"{MARKER}: a connected client, AF_UNIX where available, else the pipe.\"\"\"\n"
    "    if not hasattr(socket, \"AF_UNIX\"):\n"
    "        return _control_transport().connect(path, timeout)\n"
    "    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
    "    try:\n"
    "        client.settimeout(timeout)\n"
    "        client.connect(str(path))\n"
    "    except BaseException:\n"
    "        client.close()\n"
    "        raise\n"
    "    return client\n"
    "\n"
    "\n"
    "def send_control_command(\n"
)

# --- 4. both clients connect through the helper -----------------------------
# send_control_command and query_status open their connection identically.
A_CONNECT = (
    "    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:\n"
    "        client.settimeout(timeout)\n"
    "        client.connect(str(path))\n"
)
R_CONNECT = (
    f"    with _control_connect(path, timeout) as client:  # {MARKER}\n"
)

# (name, anchor, replacement, expected_occurrences)
EDITS = [
    ("listener bind", A_BIND, R_BIND, 1),
    ("listener unlink", A_UNLINK, R_UNLINK, 1),
    ("client helpers", A_HELPERS, R_HELPERS, 1),
    ("client connect", A_CONNECT, R_CONNECT, 2),
]


def _read(target: Path):
    """The file as LF text, and whether it used CRLF. UTF-8 whatever the locale."""
    text = target.read_bytes().decode("utf-8")
    return text.replace("\r\n", "\n"), "\r\n" in text


def _write(target: Path, text: str, crlf: bool) -> None:
    """Write UTF-8 in the file's own line endings."""
    target.write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))


def apply(target: Path) -> int:
    src, crlf = _read(target)
    if MARKER in src:
        print(f"  already patched: {target}")
        return 0
    for name, anchor, _, expected in EDITS:
        count = src.count(anchor)
        if count != expected:
            print(
                f"ANCHOR DRIFT: '{name}' matched {count} times (expected "
                f"{expected}) in {target}. Upstream control_socket.py changed "
                f"— update patches/patch_control_transport.py.",
                file=sys.stderr,
            )
            return 1
    out = src
    for _, anchor, repl, expected in EDITS:
        out = out.replace(anchor, repl, expected)
    compile(out, str(target), "exec")  # syntax safety net before writing
    _write(target, out, crlf)
    print(f"  patched (control transport): {target}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        target = Path(sys.argv[1])
    else:
        import voice_mode
        target = Path(voice_mode.__file__).parent / "control_socket.py"
    sys.exit(apply(target))
