"""Pure press/release classification for push-to-talk.

No I/O, no threading, no wall-clock reads -- a state machine driven entirely
by timestamps the caller supplies. Used by every Python key listener (native
Linux, macOS); the Windows PowerShell listener and the legacy Python companion
carry the same rule inline because they cannot import this file.

The rule (Robert, 2026-11-07, the same on every platform)
=========================================================
* Key down: ``HOLD_START`` at once. The mic opens and the reply is cut at the
  instant of the press, not after a threshold.
* Key up at least ``TAP_MAX_S`` later: ``HOLD_RELEASE``. The recording is
  transcribed and the agent answers.
* Key up sooner: ``SHORT_PRESS``, a tap. It is a full stop: the control client
  sends ``stop`` (hint switch-to-text), the partial recording is dropped and
  the agent carries on in text.

The wire names (short_press, hold_start, hold_release) are unchanged from the
threshold design so the WSL relay and the Windows sender keep their protocol;
only what a short press means changed (it used to be skip_forward).

Installed into voice_mode/ptt_core.py by patches/apply.sh.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

# Release before this many seconds and the press was a tap (stop, back to text).
# Robert's number, 2026-11-07: a deliberate tap measured 0.66 s on his keyboard.
TAP_MAX_S = 0.8


class PTTAction(Enum):
    PRESS = "press"                # wire word only: a probe that sends nothing
    SHORT_PRESS = "short_press"    # released before TAP_MAX_S: a tap, full stop
    HOLD_START = "hold_start"      # key went down: open the mic now
    HOLD_RELEASE = "hold_release"  # released after TAP_MAX_S: transcribe and answer


@dataclass
class PTTKeyState:
    """Tracks one key-down/key-up cycle for a single hotkey."""
    tap_max: float = TAP_MAX_S
    _press_ts: Optional[float] = field(default=None, init=False)

    def on_press(self, ts: float) -> Optional[PTTAction]:
        """Call when the hotkey goes down. OS key-repeat calls this again while
        held; only the first call starts the clock and returns HOLD_START."""
        if self._press_ts is not None:
            return None
        self._press_ts = ts
        return PTTAction.HOLD_START

    def on_release(self, ts: float) -> Optional[PTTAction]:
        """Call when the hotkey goes up. None for a release with no press."""
        if self._press_ts is None:
            return None
        held_for = ts - self._press_ts
        self._press_ts = None
        # The epsilon keeps exactly TAP_MAX_S a hold despite float subtraction
        # (100.8 - 100.0 is 0.79999...).
        return PTTAction.HOLD_RELEASE if held_for >= self.tap_max - 1e-9 else PTTAction.SHORT_PRESS
