# patches/ptt_listener_windows.py
"""Windows-host push-to-talk companion for WSL2.

Run this on the WINDOWS side (not inside WSL) with a Windows Python
install: `python ptt_listener_windows.py`. It captures Control-Space,
checks that the focused window is your WSL terminal (Windows Terminal,
by process name/title match), classifies press/release the same way
patches/ptt_core.py does (key down: hold_start at once; key up within
TAP_MAX_S = 0.8 s: short_press, a full stop; later: hold_release), and sends the classified events to
127.0.0.1:<port> — which WSL2's localhostForwarding delivers into the
Linux guest's relay server (patches/ptt_ipc.py / ptt_listener's role is
played by the guest-side relay started by `voicemode-switch` or apply.sh;
this script is ONLY the producer, it does not start a relay server itself).

Requires (Windows-side): pip install pynput pywin32 psutil
(psutil names the focused window's process; without it every press is dropped).

Authentication: the relay closes a connection whose first line is not
``token <hex>``, the secret in the WSL user's ~/.voicemode/ptt-relay.token. This
script reads it from VOICEMODE_PTT_TOKEN_PATH, else from the WSL home it is run
from (a wsl.localhost share path under home/<user>). Without a readable token it
sends nothing. The PowerShell listener (ptt-listener-windows.ps1) is the supported
one; this companion is kept for reference.

This file intentionally duplicates ptt_core.py's logic inline (copy-paste,
not import) since the WSL guest and Windows host do not share a Python
environment or filesystem path for imports.
"""
import ctypes
import json
import os
import re
import socket
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import win32gui
import win32process
from pynput import keyboard

PORT = 8765
TAP_MAX_S = 0.8  # as ptt_core.TAP_MAX_S: a release sooner is a tap (stop, back to text)
TERMINAL_PROCESS_NAMES = {"WindowsTerminal.exe", "OpenConsole.exe"}


class PTTAction(Enum):
    PRESS = "press"
    SHORT_PRESS = "short_press"
    HOLD_START = "hold_start"
    HOLD_RELEASE = "hold_release"


@dataclass
class PTTKeyState:
    tap_max: float = TAP_MAX_S
    _press_ts: Optional[float] = field(default=None, init=False)

    def on_press(self, ts):
        if self._press_ts is not None:
            return None
        self._press_ts = ts
        return PTTAction.HOLD_START

    def on_release(self, ts):
        if self._press_ts is None:
            return None
        held_for = ts - self._press_ts
        self._press_ts = None
        return PTTAction.HOLD_RELEASE if held_for >= self.tap_max - 1e-9 else PTTAction.SHORT_PRESS


def focused_window_is_terminal() -> bool:
    hwnd = win32gui.GetForegroundWindow()
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    try:
        import psutil
        return psutil.Process(pid).name() in TERMINAL_PROCESS_NAMES
    except Exception:
        return False


_token = None
_token_problem_logged = False


def token_path() -> Optional[str]:
    """VOICEMODE_PTT_TOKEN_PATH, else ~/.voicemode/ptt-relay.token in the WSL home this
    file is run from, else None."""
    env = os.environ.get("VOICEMODE_PTT_TOKEN_PATH")
    if env:
        return env
    m = re.match(r"^(\\\\wsl(?:\.localhost|\$)\\[^\\]+\\home\\[^\\]+)\\", os.path.abspath(__file__))
    return m.group(1) + "\\.voicemode\\ptt-relay.token" if m else None


def relay_token() -> Optional[str]:
    """The relay's token, read once and kept. None (logged once) until it can be read."""
    global _token, _token_problem_logged
    if _token:
        return _token
    path = token_path()
    try:
        with open(path, encoding="ascii") as f:
            text = f.read().strip()
        if not re.fullmatch(r"[0-9a-f]{64}", text):
            raise ValueError("it does not hold a token")
        _token = text
        return _token
    except (TypeError, OSError, ValueError) as e:
        if not _token_problem_logged:
            print(f"relay token not readable at {path} ({e}); set VOICEMODE_PTT_TOKEN_PATH. "
                  "Push-to-talk sends nothing until then.")
            _token_problem_logged = True
        return None


def send_event(action: PTTAction) -> None:
    token = relay_token()
    if token is None:
        return  # the relay would refuse it: send nothing
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=1) as sock:
            sock.sendall(("token " + token + "\n"
                          + json.dumps({"type": action.value, "ts": time.time()}) + "\n").encode())
    except OSError:
        pass  # relay not reachable right now — drop the event, don't crash the listener


def main() -> None:
    state = PTTKeyState()
    held = False
    ctrls = {keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r}
    ctrl_down = set()   # each Ctrl on its own: letting go of one leaves the other held

    def on_press(key):
        nonlocal held
        if key in ctrls:
            ctrl_down.add(key)
            return
        if ctrl_down and key == keyboard.Key.space and not held:
            if not focused_window_is_terminal():
                return
            held = True
            action = state.on_press(time.monotonic())
            if action is not None:
                send_event(action)

    def on_release(key):
        nonlocal held
        if key in ctrls:
            ctrl_down.discard(key)
            return
        if held and key == keyboard.Key.space:
            held = False
            action = state.on_release(time.monotonic())
            if action is not None:
                send_event(action)

    listener = keyboard.Listener(on_press=on_press, on_release=on_release)
    listener.start()
    print(f"WSL2 push-to-talk companion running — forwarding to 127.0.0.1:{PORT}")
    try:
        listener.join()
    except KeyboardInterrupt:
        pass
    finally:
        listener.stop()
        if held:   # stopped mid-hold: end it, or the server's hold stays latched with the mic open
            held = False
            action = state.on_release(time.monotonic())
            if action is not None:
                send_event(PTTAction.HOLD_RELEASE)


if __name__ == "__main__":
    main()
