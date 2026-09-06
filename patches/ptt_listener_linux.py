# patches/ptt_listener_linux.py
"""Native Linux (X11) push-to-talk key listener.

Captures Control-Space globally via pynput, classifies press/hold/release with
``ptt_core.PTTKeyState``, and forwards the classified action straight to
voice-mode's control channel — but only while the Claude Code terminal window
has focus (checked via ``xdotool``).

Not usable on pure-Wayland desktops (no XWayland); see the design doc's
terminal-keybinding fallback for that case.

**2026-09-05 rewrite.** This used to run its own asyncio relay bus
(``ptt_ipc.PTTEventServer``) and speak to a converse-side bridge we patched in.
Upstream 8.11 shipped a control channel that does the transport properly —
AF_UNIX socket, peer-credential auth, bounded input — so the listener is now a
plain synchronous client of that socket (``ptt_control_client``). No asyncio, no
relay server, no event bus. The whole file got shorter and the failure modes got
smaller: a control channel that is down makes PTT inert rather than wedging a
background server.

Requires ``VOICEMODE_CONTROL_CHANNEL_ENABLED=true`` — upstream's control channel
is opt-in.

Installed into voice_mode/ptt_listener_linux.py by patches/apply.sh.
Run standalone: `python3 -m voice_mode.ptt_listener_linux`
"""
import logging
import os
import subprocess
import time
from typing import Optional

import psutil

logger = logging.getLogger("voicemode.ptt_listener_linux")

POLL_INTERVAL = 0.05  # seconds; how often to check the hold threshold while held


def is_focused_terminal(terminal_pid: int) -> bool:
    """True if the currently-focused X11 window belongs to terminal_pid."""
    try:
        window = subprocess.run(
            ["xdotool", "getactivewindow"], capture_output=True, text=True,
            timeout=1, check=True,
        ).stdout.strip()
        focused_pid = subprocess.run(
            ["xdotool", "getwindowpid", window], capture_output=True, text=True,
            timeout=1, check=True,
        ).stdout.strip()
        return int(focused_pid) == terminal_pid
    except (FileNotFoundError, subprocess.CalledProcessError, ValueError, subprocess.TimeoutExpired):
        return False


def resolve_terminal_pid(pid: Optional[int] = None) -> Optional[int]:
    """Walks the process tree upward from `pid` (default: this process) and
    returns the oldest ancestor before PID 1 — a best-effort proxy for "the
    terminal emulator that ultimately owns this session" (documented v1
    heuristic; see the design doc's known limitation on ambiguous titles)."""
    try:
        proc = psutil.Process(pid)
        last = proc
        while True:
            parent = last.parent()
            if parent is None or parent.pid <= 1:
                return last.pid
            last = parent
    except psutil.Error:
        return None


def run_listener() -> None:
    """Capture Control-Space and drive the control channel until interrupted."""
    from pynput import keyboard

    # Deferred: import the voice_mode-installed modules here, not at module
    # level, so the pure functions above stay importable on a fresh checkout
    # before patches/apply.sh has installed them.
    from voice_mode import ptt_core, ptt_control_client

    terminal_pid = resolve_terminal_pid()
    if terminal_pid is None:
        logger.error("Could not resolve terminal PID — PTT listener not starting")
        return

    if not ptt_control_client.available():
        logger.warning(
            "voice-mode control socket at %s is not accepting connections. "
            "PTT will be inert until voice-mode is running with "
            "VOICEMODE_CONTROL_CHANNEL_ENABLED=true.",
            ptt_control_client.socket_path(),
        )

    state = ptt_core.PTTKeyState(
        hold_threshold=float(
            os.getenv("VOICEMODE_PTT_HOLD_THRESHOLD", str(ptt_core.DEFAULT_HOLD_THRESHOLD))
        )
    )
    held = False
    ctrl_down = False

    def _emit(action) -> None:
        if action is None:
            return
        ptt_control_client.on_action(action.value)

    def _matches_hotkey(key) -> bool:
        return key == keyboard.Key.space

    def on_press(key):
        nonlocal ctrl_down, held
        if key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
            ctrl_down = True
            return
        if ctrl_down and _matches_hotkey(key) and not held:
            if not is_focused_terminal(terminal_pid):
                return
            held = True
            _emit(state.on_press(time.monotonic()))

    def on_release(key):
        nonlocal ctrl_down, held
        if key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
            ctrl_down = False
            return
        if held and _matches_hotkey(key):
            held = False
            _emit(state.on_release(time.monotonic()))

    listener = keyboard.Listener(on_press=on_press, on_release=on_release)
    listener.start()
    logger.info(
        "PTT listener active (Ctrl-Space) -> %s", ptt_control_client.socket_path()
    )

    try:
        while True:
            if held:
                _emit(state.poll_hold(time.monotonic()))
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        pass
    finally:
        listener.stop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_listener()
