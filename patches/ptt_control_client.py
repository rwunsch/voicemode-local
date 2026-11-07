"""Send push-to-talk intents to voice-mode's control channel.

This replaces the converse-side half of our old PTT stack. Upstream 8.11 owns
the transport now: an AF_UNIX socket at ``~/.voicemode/control.sock`` speaking
newline-delimited JSON, with peer-credential auth and a 0700 directory
(VM-1688). We only need to be a well-behaved client.

Mapping from ``ptt_core.PTTAction`` to control commands
=======================================================

The rule since 2026-11-07 (Robert, every platform): key down opens the mic at
once; a release within ``ptt_core.TAP_MAX_S`` (0.8 s) is a full stop.

``hold_start``   -> ``hold_start``
    Sent on key DOWN, with no threshold wait. The server cuts the reply wherever
    it is -- TTS generation (patch_ptt_instant), playback (patch_hold_barges_in)
    -- and records with silence detection off while the hold lasts. No status
    query: upstream's status cannot report live playback.

``hold_release`` -> ``hold_end``
    Key up after TAP_MAX_S: the recording is transcribed and returned.

``short_press``  -> ``stop`` (hint ``switch-to-text``)
    Key up before TAP_MAX_S: a tap. The stop dominates the hold the key-down
    started; the recording is dropped untranscribed and converse returns
    ``[control: stop] user switched to text mode ...``, so the agent carries on
    in text. (Until 2026-11-07 a short press was ``skip_forward``.)

``press``        -> nothing
    The relay's reachability probe. Never acts.

Everything is best-effort: a control channel that is disabled, not yet bound,
or mid-restart must degrade to "PTT does nothing", never to an exception in a
key-listener thread.

Sender for the native Windows listener
======================================
``python -m voice_mode.ptt_control_client --stdin`` reads one action name per
line and answers each with one line: ``ready`` (for ``warm``, sent once at
listener start so the first press does not load modules), ``sent``, ``none`` (nothing to send),
``no-channel`` (no voice turn is serving the channel; after a ``hold_release``
that answered ``no-channel`` the sender also wakes the last voice session, see
session_wake), ``busy``, ``refused: ...``
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


# What a command carries besides its name. A PTT stop is "back to text mode": the
# server turns this named hint into its own sentence for the agent (CONTROL_INTENTS).
HINTS = {"stop": "switch-to-text"}


def _payload(command: str) -> dict:
    payload = {"command": command}
    if command in HINTS:
        payload["hint"] = HINTS[command]
    return payload


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
            s.sendall((json.dumps(_payload(command)) + "\n").encode("utf-8"))
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
    _send_line(_payload(command))
    return True


def on_action(action: str) -> None:
    """Translate one ptt_core action name into control-channel traffic.

    See the module docstring for the table. ``hold_start`` never queries status
    or adds a skip_forward: upstream's status cannot report live playback, and a
    skip_forward with nothing playing latches STATE_SKIP_FORWARD, which the
    recording loop reads as "end this turn".
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


_WAKE_FILE = Path(__file__).with_name("session_wake.py")
_WAKE = None


def _session_wake():
    """session_wake: the installed voice_mode's, or the file beside this one."""
    global _WAKE
    installed = sys.modules.get("voice_mode.session_wake")
    if installed is not None:
        return installed
    if _WAKE is None:
        spec = importlib.util.spec_from_file_location("_vml_session_wake", _WAKE_FILE)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _WAKE = mod
    return _WAKE


def wake_idle_session():
    """Push-to-talk held with no voice turn: ask the last voice session to listen.

    session_wake checks it is idle, rate-limits (one per 5 s) and logs the outcome to
    ~/.voicemode/logs/session-wake.log. Returns (ok, detail); never raises."""
    try:
        mod = _session_wake()
        ok, detail = mod.wake(reason="ptt")
        label = getattr(mod, "last_label", None)
        if ok and label:
            detail = f"woke {label}; talk after Listening."
        return ok, detail
    except Exception as e:  # noqa: BLE001 - never break a key listener
        return False, "failed: " + _one_line(e)


def _wake_and_log() -> None:
    ok, detail = wake_idle_session()
    logger.info("no voice turn running: %s", detail if ok else "could not wake: " + detail)


def warm() -> str:
    """Load now what the first press would otherwise load: the path resolver and,
    without AF_UNIX (Windows), the pipe transport. Sends nothing."""
    socket_path()
    if not hasattr(socket, "AF_UNIX"):
        _control_transport()
    return "ready"


def serve_lines(stdin, stdout) -> None:
    """One answer line per non-empty action line, flushed at once, until EOF.

    ``warm`` answers ``ready`` (see warm()). EOF while a hold is open means the
    listener died mid-hold (``ptt off``, watchdog, kill_tree): send hold_end, or the
    recording runs to max_duration."""
    holding = False
    woke_only = False   # this hold began with no voice channel: nothing to end on release
    for line in iter(stdin.readline, ""):
        action = line.strip()
        if action:
            if action == "hold_start":
                holding = True
            elif action in ("hold_release", "short_press"):
                holding = False
            if action == "hold_release" and woke_only:
                woke_only = False
                answer = "no-channel"
                stdout.write(answer + "\n")
                stdout.flush()
                _wake_and_log()
                continue
            if action == "warm":
                try:
                    answer = warm()
                except Exception as e:  # noqa: BLE001 - a failed warm-up is not fatal
                    answer = "failed: " + _one_line(e)
            else:
                answer = action_outcome(action)
            stdout.write(answer + "\n")
            stdout.flush()
            if action == "hold_start":
                woke_only = answer == "no-channel"
            # A hold with no voice turn running: wake the last voice session. After the
            # answer, so the listener never waits on it; once per press (one release).
            if action == "hold_release" and answer == "no-channel":
                _wake_and_log()
    if holding and not woke_only:
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
COMMANDS = {"short_press": "stop", "hold_start": "hold_start", "hold_release": "hold_end"}


def available() -> bool:
    """True if the control socket exists and accepts a connection."""
    try:
        with _connect(socket_path(), CONNECT_TIMEOUT):
            return True
    except Exception:  # noqa: BLE001
        return False


if __name__ == "__main__":
    sys.exit(main())
