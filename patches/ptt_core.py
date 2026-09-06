"""Pure press/hold/release classification for push-to-talk.

No I/O, no threading, no wall-clock reads — a state machine driven entirely
by timestamps the caller supplies. Used identically by every platform-
specific key listener (native Linux, the WSL2 Windows companion, future
macOS/Windows-native listeners) so the interaction semantics — short press
vs. hold, the threshold, key-repeat handling — live in exactly one place.

Design: docs/superpowers/specs/2026-07-07-push-to-talk-design.md
Installed into voice_mode/ptt_core.py by patches/apply.sh.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

DEFAULT_HOLD_THRESHOLD = 1.0  # seconds; VOICEMODE_PTT_HOLD_THRESHOLD


class PTTAction(Enum):
    PRESS = "press"                # key went down (fires immediately, every press)
    SHORT_PRESS = "short_press"    # released before the hold threshold
    HOLD_START = "hold_start"      # held past the threshold, still down
    HOLD_RELEASE = "hold_release"  # release of a press that had reached HOLD_START


@dataclass
class PTTKeyState:
    """Tracks one key-down/key-up cycle for a single hotkey."""
    hold_threshold: float = DEFAULT_HOLD_THRESHOLD
    _press_ts: Optional[float] = field(default=None, init=False)
    _hold_fired: bool = field(default=False, init=False)

    def on_press(self, ts: float) -> PTTAction:
        """Call when the hotkey goes down. OS key-repeat sends this
        repeatedly while held — only the first call starts the clock."""
        if self._press_ts is None:
            self._press_ts = ts
            self._hold_fired = False
        return PTTAction.PRESS

    def poll_hold(self, now: float) -> Optional[PTTAction]:
        """Call periodically while the key is down (e.g. every 50ms).
        Returns HOLD_START exactly once, the moment the threshold is
        crossed; None at every other call, including while still held
        after HOLD_START has already fired."""
        if self._press_ts is None or self._hold_fired:
            return None
        if now - self._press_ts >= self.hold_threshold:
            self._hold_fired = True
            return PTTAction.HOLD_START
        return None

    def on_release(self, ts: float) -> Optional[PTTAction]:
        """Call when the hotkey goes up. Returns the resulting action, or
        None for a spurious release with no matching press."""
        if self._press_ts is None:
            return None
        was_hold = self._hold_fired
        self._press_ts = None
        self._hold_fired = False
        return PTTAction.HOLD_RELEASE if was_hold else PTTAction.SHORT_PRESS
