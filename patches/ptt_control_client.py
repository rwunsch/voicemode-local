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

``hold_start``   -> (``skip_forward`` if audio is playing) then ``hold_start``
    The hold flag only suppresses silence detection in the recording loop; it
    does not cut playback. So if the assistant is mid-utterance we barge in
    first. We *query* rather than fire blindly, because a latched
    skip_forward with nothing playing would cut the NEXT utterance instead.

``hold_release`` -> ``hold_end``
    Ends the recording via the same path skip_forward uses.

``press``        -> nothing
    Fires on every key-down, before we know whether it is a short press or a
    hold. Acting here would double-fire.

Everything is best-effort: a control channel that is disabled, not yet bound,
or mid-restart must degrade to "PTT does nothing", never to an exception in a
key-listener thread.

Installed into voice_mode/ptt_control_client.py by patches/apply.sh.
"""
import json
import logging
import os
import socket
from pathlib import Path
from typing import Optional

logger = logging.getLogger("voicemode.ptt")

# Matches upstream's config.CONTROL_SOCKET_PATH default. Read from the same env
# var so a user who moved the socket does not have to configure it twice.
DEFAULT_SOCKET = "~/.voicemode/control.sock"

CONNECT_TIMEOUT = 0.5   # a local unix socket answers instantly or not at all
STATUS_TIMEOUT = 0.5


def socket_path() -> Path:
    raw = os.getenv("VOICEMODE_CONTROL_SOCKET_PATH") or DEFAULT_SOCKET
    return Path(os.path.expanduser(raw))


def _send_line(payload: dict, read_reply: bool = False) -> Optional[str]:
    """Connect, send one JSON line, optionally read one line back.

    Returns the reply line for a query, or None. Never raises: a PTT keypress
    must not be able to kill the listener thread.
    """
    path = socket_path()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(CONNECT_TIMEOUT)
            s.connect(str(path))
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


def send(command: str) -> bool:
    """Fire one control command. True if it went out (not that it was acted on)."""
    if not command:
        return False
    _send_line({"command": command})
    return True


def is_playing() -> bool:
    """Best-effort: is an utterance being spoken right now?

    Uses upstream's read-only ``status`` query. On any doubt returns False, so
    we skip a barge-in rather than risk latching a skip_forward that would cut
    the next utterance instead of this one.
    """
    line = _send_line({"command": "status"}, read_reply=True)
    if not line:
        return False
    try:
        data = json.loads(line)
    except (ValueError, TypeError):
        return False
    if not isinstance(data, dict):
        return False
    # Upstream's status payload reports the now-playing entry when speaking.
    now = data.get("now_playing") or data.get("nowPlaying")
    if now:
        return True
    return str(data.get("state", "")).lower() in ("playing", "running_playback")


def on_action(action: str) -> None:
    """Translate one ptt_core action name into control-channel traffic."""
    if action == "short_press":
        send("skip_forward")
    elif action == "hold_start":
        if is_playing():
            send("skip_forward")   # barge in, then keep the mic open
        send("hold_start")
    elif action == "hold_release":
        send("hold_end")
    # "press" is deliberately ignored -- see the module docstring.


def available() -> bool:
    """True if the control socket exists and accepts a connection."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(CONNECT_TIMEOUT)
            s.connect(str(socket_path()))
            return True
    except Exception:  # noqa: BLE001
        return False
