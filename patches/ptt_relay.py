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

Installed into voice_mode/ptt_relay.py by patches/apply.sh.
Run standalone: `python3 -m voice_mode.ptt_relay`
"""
import json
import logging
import os
import socket
import socketserver
from typing import Optional

logger = logging.getLogger("voicemode.ptt_relay")

DEFAULT_PORT = 8765
VALID_ACTIONS = {"press", "short_press", "hold_start", "hold_release"}
MAX_LINE = 1024  # a line is an action name; anything larger is not ours


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
        try:
            for raw in self.rfile:
                text = raw.decode("utf-8", "replace")
                action = parse_action(text)
                if action is None:
                    if text.strip():
                        logger.info("relay <- rejected %r", text.strip()[:80])
                    continue
                logger.info("relay <- %s", action)
                try:
                    from voice_mode import ptt_control_client
                    reachable = ptt_control_client.available()
                    ptt_control_client.on_action(action)
                    if not reachable:
                        logger.info(
                            "   ...but voice-mode's control socket is not bound: "
                            "it exists only DURING a converse call, so this press "
                            "did nothing. Press while the assistant is speaking or "
                            "listening."
                        )
                except Exception as e:  # noqa: BLE001 - never kill the relay
                    logger.warning("forward failed: %s", e)
        except (OSError, socket.timeout):
            pass


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    # Bind to loopback only: WSL2 localhostForwarding reaches it from Windows,
    # and nothing off-box should be able to drive the mic.
    address_family = socket.AF_INET


def serve(port: Optional[int] = None, host: Optional[str] = None) -> None:
    """Serve the relay.

    Binds loopback by default. On WSL2 the producer is a Windows-side listener,
    and Windows reaches a WSL server through localhostForwarding only if it is
    bound to 0.0.0.0 -- set VOICEMODE_PTT_HOST=0.0.0.0 there. Inside WSL's NAT
    namespace that is the virtual adapter, not your LAN, but it is still wider
    than loopback, so it stays opt-in.
    """
    port = port if port is not None else int(os.getenv("VOICEMODE_PTT_PORT", DEFAULT_PORT))
    host = host if host is not None else os.getenv("VOICEMODE_PTT_HOST", "127.0.0.1")
    with _Server((host, port), _Handler) as srv:
        logger.info("PTT relay listening on %s:%d -> control socket", host, port)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    serve()
