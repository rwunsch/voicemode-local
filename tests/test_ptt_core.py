"""Tests for patches/ptt_core.py: key down opens the mic, key up decides tap or hold.

Robert's rule (2026-11-07): instant listening, but a release within TAP_MAX_S (0.8 s)
is a full stop, back to text mode.
"""
import sys
from pathlib import Path

PATCHES_DIR = Path(__file__).parent.parent / "patches"
sys.path.insert(0, str(PATCHES_DIR))

import ptt_core  # noqa: E402

A = ptt_core.PTTAction


def _cycle(down, up, tap_max=None):
    state = ptt_core.PTTKeyState() if tap_max is None else ptt_core.PTTKeyState(tap_max=tap_max)
    return [state.on_press(down), state.on_release(up)]


def test_tap_max_is_robert_s_number():
    assert ptt_core.TAP_MAX_S == 0.8


def test_key_down_starts_the_hold_at_once():
    assert ptt_core.PTTKeyState().on_press(10.0) == A.HOLD_START


def test_short_release_is_a_tap():
    assert _cycle(10.0, 10.5) == [A.HOLD_START, A.SHORT_PRESS]


def test_long_release_ends_the_hold():
    assert _cycle(10.0, 11.2) == [A.HOLD_START, A.HOLD_RELEASE]


def test_the_boundary_belongs_to_the_hold():
    assert _cycle(10.0, 10.8) == [A.HOLD_START, A.HOLD_RELEASE]
    assert _cycle(10.0, 10.79) == [A.HOLD_START, A.SHORT_PRESS]


def test_tap_max_can_be_given():
    assert _cycle(0.0, 0.5, tap_max=0.4) == [A.HOLD_START, A.HOLD_RELEASE]


def test_key_repeat_sends_nothing_and_keeps_the_clock():
    state = ptt_core.PTTKeyState()
    assert state.on_press(0.0) == A.HOLD_START
    assert state.on_press(0.5) is None  # OS auto-repeat while held
    assert state.on_press(0.7) is None
    assert state.on_release(0.79) == A.SHORT_PRESS


def test_spurious_release_returns_none():
    assert ptt_core.PTTKeyState().on_release(5.0) is None


def test_state_resets_after_release_for_next_cycle():
    state = ptt_core.PTTKeyState()
    state.on_press(0.0)
    state.on_release(0.3)
    assert state.on_press(1.0) == A.HOLD_START
    assert state.on_release(2.0) == A.HOLD_RELEASE


def test_nothing_needs_polling_any_more():
    assert not hasattr(ptt_core.PTTKeyState, "poll_hold")
