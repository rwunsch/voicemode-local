# patches/ptt_relay.py
"""WSL2 hop: receive PTT actions from the Windows companion, forward to the
voice-mode control socket.

Why this still exists after the control-channel rewrite
======================================================
On WSL2 the key press happens on the *Windows* side — an X11 listener inside
the guest cannot see a Windows-focused window. The Windows companion
(``ptt_listener_windows.py``) therefore runs on the host and needs a way in.
It cannot write to the guest's AF_UNIX control socket directly, so one TCP hop
remains: the companion connects to ``127.0.0.1:<port>``, WSL2's
localhostForwarding delivers it into the guest, and this relay hands the action
to ``ptt_control_client``.

That is all it does. The old ``ptt_ipc.py`` was a general event bus with
subscribers, an asyncio server and a converse-side consumer; upstream's control
channel owns all of that now. What is left is a ~10-line translation, so this
module is deliberately small and synchronous.

Not used on native Linux or macOS — there the listener talks to the control
socket directly.

Wire format: one JSON object per line, ``{"action": "<ptt_core action name>"}``.
A bare action name on its own line is also accepted, so the companion can stay
trivial.

Authentication (Talos review, 2026-11-12): the first line of every connection
must be ``token <hex>``, the shared secret in ``~/.voicemode/ptt-relay.token``.
The relay creates that file on its first start (32 random bytes as hex, mode
600) and reuses it afterwards. A connection with a missing or wrong token is
closed and nothing it sends is forwarded. Without this, any process that could
reach the port could stop speech, hold the mic open or wake an idle session.
The Windows listener reads the file over the WSL share.

Limits: a line longer than MAX_LINE closes the connection (it is read with a
bound, never buffered whole), and at most MAX_CONNECTIONS connections are
served at once; more are closed at once.

Hold state is relay-wide, not per connection, on purpose: the Windows listener
opens a new connection for every action, so hold_start and its hold_release
never share one. A lock keeps concurrent connections from interleaving it.

Installed into voice_mode/ptt_relay.py by patches/apply.sh.
Run standalone: `python3 -m voice_mode.ptt_relay`
"""
import hmac
import json
import logging
import os
import re
import secrets
import socket
import socketserver
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger("voicemode.ptt_relay")

DEFAULT_PORT = 8765
VALID_ACTIONS = {"press", "short_press", "hold_start", "hold_release"}
MAX_LINE = 1024  # a line is an action name; anything larger is not ours
MAX_CONNECTIONS = 8  # the listener opens one short connection per action
DEFAULT_TOKEN_PATH = Path.home() / ".voicemode" / "ptt-relay.token"


def _valid_token(text: str) -> bool:
    # Exactly what the Windows listener accepts (^[0-9a-f]{64}$). Not
    # bytes.fromhex: it allows spaces and upper case.
    return re.fullmatch(r"[0-9a-f]{64}", text) is not None


def load_or_create_token(path=None) -> str:
    """The relay's shared secret: read from ``path``, or created there once
    (32 random bytes as hex, mode 600). A damaged file is replaced."""
    path = Path(path) if path is not None else DEFAULT_TOKEN_PATH
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        text = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        text = None
    if text is not None and _valid_token(text):
        os.chmod(path, 0o600)
        return text
    if text is not None:
        logger.warning("relay token file %s is damaged; writing a new one", path)
        path.unlink()
    token = secrets.token_hex(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(token + "\n")
    return token


def parse_action(line: str) -> Optional[str]:
    """Extract a valid action name from one wire line, or None.

    Accepts ``{"action": "hold_start"}`` or a bare ``hold_start``. Unknown
    names are rejected rather than forwarded, so a stray connection cannot
    drive the control channel.
    """
    line = (line or "").strip()
    if not line or len(line) > MAX_LINE:
        return None
    if line.startswith("{"):
        try:
            payload = json.loads(line)
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        # "type" is what the old ptt_ipc.PTTEvent used; accept both.
        line = str(payload.get("action") or payload.get("type") or "").strip()
    return line if line in VALID_ACTIONS else None


class _Handler(socketserver.StreamRequestHandler):
    timeout = 2.0

    def handle(self) -> None:
        slots = self.server.slots
        if not slots.acquire(blocking=False):
            logger.info("relay: more than %d connections; closing one", self.server.max_conns)
            return
        try:
            self._serve()
        finally:
            slots.release()

    def _lines(self):
        """Lines read with a bound. Stops at EOF and at an over-long line,
        which also ends the connection."""
        while True:
            raw = self.rfile.readline(MAX_LINE + 2)
            if not raw:
                return
            if len(raw) > MAX_LINE + 1:
                logger.info("relay <- line over %d bytes; closing", MAX_LINE)
                return
            yield raw

    def _authenticated(self, raw: bytes) -> bool:
        expected = self.server.token
        if expected is None:
            return True
        parts = raw.strip().split(b" ", 1)
        if len(parts) != 2 or parts[0] != b"token":
            return False
        return hmac.compare_digest(parts[1].strip(), expected.encode("ascii"))

    def _serve(self) -> None:
        try:
            lines = self._lines()
            if self.server.token is not None:
                first = next(lines, None)
                if first is None or not self._authenticated(first):
                    logger.info("relay <- missing or wrong token; closing")
                    return
            for raw in lines:
                text = raw.decode("utf-8", "replace")
                action = parse_action(text)
                if action is None:
                    if text.strip():
                        logger.info("relay <- rejected %r", text.strip()[:80])
                    continue
                logger.info("relay <- %s", action)
                forward(action, self.server.client)
        except (OSError, socket.timeout):
            pass


_woke_only = False   # the current hold began with no voice channel
_state_lock = threading.Lock()  # connections run on their own threads


def forward(action: str, client=None) -> None:
    """Hand one action to the control channel. A hold released with no voice turn
    running (``no-channel``) wakes the last voice session instead (session_wake).
    Never raises."""
    try:
        if client is None:
            from voice_mode import ptt_control_client as client
        global _woke_only
        with _state_lock:
            if action == "hold_release" and _woke_only:
                # The hold began with no voice turn: there is nothing to end, only a wake.
                _woke_only = False
                outcome = "no-channel"
            else:
                outcome = client.action_outcome(action)
                if action == "hold_start":
                    _woke_only = outcome == "no-channel"
        if outcome == "no-channel":
            if action == "hold_release":
                ok, detail = client.wake_idle_session()
                logger.info("   no voice turn running: %s", detail if ok else "could not wake: " + detail)
            else:
                logger.info(
                    "   ...but voice-mode's control socket is not bound: it exists only "
                    "DURING a converse call. A hold (0.8 s or more) wakes the last voice "
                    "session instead.")
    except Exception as e:  # noqa: BLE001 - never kill the relay
        logger.warning("forward failed: %s", e)


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    # Bind to loopback only: WSL2 localhostForwarding reaches it from Windows,
    # and nothing off-box should be able to drive the mic.
    address_family = socket.AF_INET


def make_server(host: str, port: int, token: Optional[str], client=None,
                max_conns: int = MAX_CONNECTIONS) -> _Server:
    """A bound relay server. ``token`` None turns authentication off (tests
    only; serve() always passes one). ``client`` None means voice-mode's
    ptt_control_client."""
    srv = _Server((host, port), _Handler)
    srv.token, srv.client, srv.max_conns = token, client, max_conns
    srv.slots = threading.BoundedSemaphore(max_conns)
    return srv


def serve(port: Optional[int] = None, host: Optional[str] = None,
          token_path=None) -> None:
    """Serve the relay.

    Binds loopback by default. On WSL2 the producer is a Windows-side listener,
    and Windows reaches a WSL server through localhostForwarding only if it is
    bound to 0.0.0.0 -- set VOICEMODE_PTT_HOST=0.0.0.0 there. Inside WSL's NAT
    namespace that is the virtual adapter, not your LAN, but it is still wider
    than loopback, so it stays opt-in.
    """
    port = port if port is not None else int(os.getenv("VOICEMODE_PTT_PORT", DEFAULT_PORT))
    host = host if host is not None else os.getenv("VOICEMODE_PTT_HOST", "127.0.0.1")
    token = load_or_create_token(token_path)
    with make_server(host, port, token) as srv:
        logger.info("PTT relay listening on %s:%d -> control socket (token: %s)",
                    host, port, token_path or DEFAULT_TOKEN_PATH)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    serve()
