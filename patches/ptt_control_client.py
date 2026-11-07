"""Send push-to-talk intents to voice-mode's control channel.

This replaces the converse-side half of our old PTT stack. Upstream 8.11 owns
the transport now: an AF_UNIX socket at ``~/.voicemode/control.sock`` speaking
newline-delimited JSON, with peer-credential auth and a 0700 directory
(VM-1688). We only need to be a well-behaved client.

Mapping from ``ptt_core.PTTAction`` to control commands
=======================================================

``short_press``  -> ``skip_forward``
    Upstream's skip_forward is already context-sensitive: pressed while the
    assistant speaks it cuts the utterance and hands over the mic; pressed
    while *you* speak it ends the recording and transcribes. That is exactly
    both halves of a short press, so we send one command and let the server
    decide which it meant.

``hold_start``   -> ``hold_start``
    One command. patch_hold_barges_in makes the playback loops abort on
    ``is_holding``, so a press during speech cuts the utterance AND holds the
    mic. No status query: upstream's status cannot report live playback (its
    own docstring says now_playing is the previous COMPLETED utterance), so a
    client-side barge-in decision would be a guess.

``hold_release`` -> ``hold_end``
    Ends the recording via the same path skip_forward uses.

``press``        -> nothing
    Fires on every key-down, before we know whether it is a short press or a
    hold. Acting here would double-fire.

Everything is best-effort: a control channel that is disabled, not yet bound,
or mid-restart must degrade to "PTT does nothing", never to an exception in a
key-listener thread.

Sender for the native Windows listener
======================================
``python -m voice_mode.ptt_control_client --stdin`` reads one action name per
line and answers each with one line: ``sent``, ``none`` (nothing to send),
``no-channel`` (no voice turn is serving the channel), ``busy``, ``refused: ...``
(the pipe's owner or server is not this user; see control_transport) or
``failed: ...`` (also for an unknown action word). ptt-listener-windows.ps1 starts it in native mode: the
PowerShell side only reads the keys, and the mapping, the pipe name and the
owner check stay here, in one place. (A PowerShell pipe client that read the
token next to the key polling was quarantined by Windows Defender as malicious,
2026-11-07; see docs/windows-native/README.md.)

Installed into voice_mode/ptt_control_client.py by patches/apply.sh.
"""
import argparse
import importlib.util
import json
import logging
import os
import socket
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger("voicemode.ptt")

# Upstream's config.CONTROL_SOCKET_PATH (config.py:697): VOICEMODE_CONTROL_SOCKET,
# else BASE_DIR/control.sock, resolved by voice_pill_state so a user who moved
# the socket or the base dir does not have to configure it twice.
# VOICEMODE_CONTROL_SOCKET_PATH is the name this file used to read by mistake;
# still honoured as a fallback.

CONNECT_TIMEOUT = 0.5   # a local unix socket answers instantly or not at all
STATUS_TIMEOUT = 0.5


def socket_path() -> Path:
    legacy = os.getenv("VOICEMODE_CONTROL_SOCKET_PATH")
    if legacy and not os.getenv("VOICEMODE_CONTROL_SOCKET"):
        return Path(os.path.expanduser(legacy))
    return _pill_state().control_socket_path()


_STATE_FILE = Path(__file__).with_name("voice_pill_state.py")
_STATE = None


def _pill_state():
    """The path resolver: the installed voice_mode's, or the file beside this one."""
    global _STATE
    installed = sys.modules.get("voice_mode.voice_pill_state")
    if installed is not None:
        return installed
    if _STATE is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("_vml_pill_state", _STATE_FILE)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _STATE = mod
    return _STATE


# Where the transport may come from: the installed voice_mode package, or the
# file beside this one (patches/ in the repo; voice_mode/ itself once installed).
# Never a bare `control_transport` found elsewhere on sys.path.
_TRANSPORT_FILE = Path(__file__).with_name("control_transport.py")
_TRANSPORT = None


def _control_transport():
    """The named-pipe transport, for Pythons without AF_UNIX (Windows)."""
    global _TRANSPORT
    installed = sys.modules.get("voice_mode.control_transport")
    if installed is not None:
        return installed
    if _TRANSPORT is None:
        if not _TRANSPORT_FILE.is_file():  # an install that never copied the module
            raise OSError(f"no control transport at {_TRANSPORT_FILE}")
        spec = importlib.util.spec_from_file_location("_vml_control_transport", _TRANSPORT_FILE)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _TRANSPORT = mod
    return _TRANSPORT


def _connect(path: Path, timeout: float):
    """A connected client: AF_UNIX where it exists, else the owner-checked pipe."""
    if not hasattr(socket, "AF_UNIX"):
        return _control_transport().connect(path, timeout)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(timeout)
        s.connect(str(path))
    except BaseException:
        s.close()
        raise
    return s


def _send_line(payload: dict, read_reply: bool = False) -> Optional[str]:
    """Connect, send one JSON line, optionally read one line back.

    Returns the reply line for a query, or None. Never raises: a PTT keypress
    must not be able to kill the listener thread.
    """
    path = socket_path()
    try:
        with _connect(path, CONNECT_TIMEOUT) as s:
            s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            if not read_reply:
                return None
            s.settimeout(STATUS_TIMEOUT)
            buf = b""
            while b"\n" not in buf and len(buf) < 8192:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
            return buf.decode("utf-8", "replace").split("\n", 1)[0] or None
    except (OSError, socket.timeout) as e:
        # Not bound yet, disabled, or restarting -- PTT is simply inert.
        logger.debug("control socket %s unavailable (%s)", path, e)
        return None
    except Exception as e:  # noqa: BLE001 - never break a key listener
        logger.debug("control send failed: %s", e)
        return None


def deliver(command: str) -> str:
    """Send one command; say what happened (see the module docstring). Never raises."""
    path = socket_path()
    try:
        with _connect(path, CONNECT_TIMEOUT) as s:
            s.sendall((json.dumps({"command": command}) + "\n").encode("utf-8"))
        return "sent"
    except (FileNotFoundError, ConnectionRefusedError):
        return "no-channel"
    except PermissionError as e:
        return "refused: " + _one_line(e)
    except (socket.timeout, TimeoutError):
        return "busy"
    except Exception as e:  # noqa: BLE001 - never break a key listener
        return "failed: " + _one_line(e)


def _one_line(error: BaseException) -> str:
    text = error.strerror if isinstance(error, OSError) and error.strerror else str(error)
    return " ".join(str(text or type(error).__name__).split())


def send(command: str) -> bool:
    """Fire one control command. True if it went out (not that it was acted on)."""
    if not command:
        return False
    _send_line({"command": command})
    return True


def on_action(action: str) -> None:
    """Translate one ptt_core action name into control-channel traffic.

    ``short_press``  -> ``skip_forward``
        Upstream's skip_forward is already context-sensitive: pressed while the
        assistant speaks it cuts the utterance and hands over the mic; pressed
        while *you* speak it ends the recording and transcribes. One command
        covers both halves of a short press.

    ``hold_start``   -> ``hold_start``
        Just the one command. The hold is self-sufficient: patch_hold_barges_in
        makes the playback loops abort on ``is_holding``, so a press during
        speech cuts the utterance AND holds the mic.

        This deliberately does NOT query status first and send skip_forward.
        Upstream's status query cannot report live playback -- its own docstring
        says now_playing is "the previous completed utterance, not the live
        in-flight stream", and state is "running" whether speaking or listening
        -- so any client-side barge-in decision would be a guess. Sending
        skip_forward unconditionally is worse: with nothing playing it latches
        STATE_SKIP_FORWARD, which the recording loop reads as "end this turn".

    ``hold_release`` -> ``hold_end``
    ``press``        -> nothing (fires before short-vs-hold is known)
    """
    command = COMMANDS.get(action)
    if command:
        send(command)


def action_outcome(action: str) -> str:
    """on_action's mapping, answering what happened: ``none`` for ``press``, which sends
    nothing; an unknown word is a visible failure, not a silent ``none``."""
    command = COMMANDS.get(action)
    if command:
        return deliver(command)
    return "none" if action == "press" else f"failed: unknown action {action!r}"


def serve_lines(stdin, stdout) -> None:
    """One answer line per non-empty action line, flushed at once, until EOF.

    EOF while a hold is open means the listener died mid-hold (``ptt off``, watchdog,
    kill_tree): send hold_end, or the recording runs to max_duration."""
    holding = False
    for line in iter(stdin.readline, ""):
        action = line.strip()
        if action:
            if action == "hold_start":
                holding = True
            elif action in ("hold_release", "short_press"):
                holding = False
            stdout.write(action_outcome(action) + "\n")
            stdout.flush()
    if holding:
        deliver(COMMANDS["hold_release"])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Send push-to-talk actions to voice-mode's control channel.")
    parser.add_argument("--stdin", action="store_true", required=True,
                        help="read one action per line, answer one outcome per line")
    parser.parse_args(argv)
    # .NET Framework's redirected stdin begins with a UTF-8 BOM, which made the first action
    # of every run unknown (Windows, 2026-11-07); utf-8-sig drops it. The listener reads
    # stdout as UTF-8.
    for stream, encoding in ((sys.stdin, "utf-8-sig"), (sys.stdout, "utf-8")):
        if hasattr(stream, "reconfigure"):
            # errors="replace": stray bytes become an unknown action ("failed: ..."), not a crash.
            stream.reconfigure(encoding=encoding, errors="replace")
    serve_lines(sys.stdin, sys.stdout)
    return 0


# ptt_core action -> control command; press (and anything else) sends nothing.
COMMANDS = {"short_press": "skip_forward", "hold_start": "hold_start", "hold_release": "hold_end"}


def available() -> bool:
    """True if the control socket exists and accepts a connection."""
    try:
        with _connect(socket_path(), CONNECT_TIMEOUT):
            return True
    except Exception:  # noqa: BLE001
        return False


if __name__ == "__main__":
    sys.exit(main())
