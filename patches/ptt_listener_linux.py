# patches/ptt_listener_linux.py
"""Native Linux (X11) push-to-talk key listener.

Captures Control-Space globally via pynput and forwards it straight to
voice-mode's control channel -- but only while the Claude Code terminal window
has focus (checked via ``xdotool``). Key down sends ``hold_start`` at once (the
reply is cut and the mic opens); key up after ``ptt_core.TAP_MAX_S`` (0.8 s)
sends ``hold_release`` (transcribe and answer), sooner it sends ``short_press``,
which the control client turns into a full ``stop`` (back to text mode).

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
import signal
import subprocess
import time
from typing import Optional

import psutil

logger = logging.getLogger("voicemode.ptt_listener_linux")


class Hotkey:
    """Control-Space as a down/up pair, fed to a ``ptt_core.PTTKeyState``.

    Pure: ``emit`` gets the action name, ``focused`` says whether the terminal has
    focus. Only a fresh press while focused starts a cycle; auto-repeat is ignored,
    and the release ends the cycle wherever focus has gone by then.
    """

    def __init__(self, state, emit, focused):
        self._state, self._emit, self._focused = state, emit, focused
        self._ctrls = set()  # which Ctrl keys are down; Ctrl is down while any is
        self._held = False

    def ctrl(self, down: bool, which="ctrl") -> None:
        """Track each Ctrl key on its own: with both down, letting go of one
        leaves Ctrl down (one Boolean for both used to drop the hotkey)."""
        if down:
            self._ctrls.add(which)
        else:
            self._ctrls.discard(which)

    def release_held(self, ts: float) -> None:
        """The listener is stopping. If a hold is active, send hold_release so
        the server's hold (mic open) is not left latched. Otherwise nothing."""
        if not self._held:
            return
        self._held = False
        self._state.on_release(ts)
        self._emit("hold_release")

    def space(self, down: bool, ts: float) -> None:
        if down:
            if self._held or not self._ctrls or not self._focused():
                return
            self._held = True
            action = self._state.on_press(ts)
        else:
            if not self._held:
                return
            self._held = False
            action = self._state.on_release(ts)
        if action is not None:
            self._emit(action.value)


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


def _interrupt(signum, frame):
    raise KeyboardInterrupt


def install_stop_signals() -> None:
    """SIGTERM and SIGHUP stop the listener the way Ctrl-C does, so its finally
    runs and an active hold is released. Main thread only; elsewhere a no-op."""
    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _interrupt)
        except ValueError:  # not the main thread
            pass


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

    hotkey = Hotkey(ptt_core.PTTKeyState(), ptt_control_client.on_action,
                    lambda: is_focused_terminal(terminal_pid))

    def on_press(key):
        if key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
            hotkey.ctrl(True, key)
        elif key == keyboard.Key.space:
            hotkey.space(True, time.monotonic())

    def on_release(key):
        if key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
            hotkey.ctrl(False, key)
        elif key == keyboard.Key.space:
            hotkey.space(False, time.monotonic())

    install_stop_signals()
    listener = keyboard.Listener(on_press=on_press, on_release=on_release)
    listener.start()
    logger.info(
        "PTT listener active (Ctrl-Space) -> %s", ptt_control_client.socket_path()
    )

    try:
        listener.join()
    except KeyboardInterrupt:
        pass
    finally:
        listener.stop()
        hotkey.release_held(time.monotonic())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_listener()
