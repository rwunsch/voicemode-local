"""Tests for patches/ptt_core.py — press/hold/release classification."""
import sys
from pathlib import Path

PATCHES_DIR = Path(__file__).parent.parent / "patches"
sys.path.insert(0, str(PATCHES_DIR))

import ptt_core  # noqa: E402


def test_press_always_returns_press_action():
    state = ptt_core.PTTKeyState(hold_threshold=1.0)
    assert state.on_press(0.0) == ptt_core.PTTAction.PRESS


def test_key_repeat_press_is_idempotent():
    state = ptt_core.PTTKeyState(hold_threshold=1.0)
    state.on_press(0.0)
    # OS key-repeat sends more press events while held; must not reset the clock
    state.on_press(0.5)
    action = state.on_release(0.8)
    assert action == ptt_core.PTTAction.SHORT_PRESS


def test_short_press_below_threshold():
    state = ptt_core.PTTKeyState(hold_threshold=1.0)
    state.on_press(0.0)
    assert state.poll_hold(0.5) is None
    assert state.on_release(0.9) == ptt_core.PTTAction.SHORT_PRESS


def test_hold_crossing_threshold_fires_once():
    state = ptt_core.PTTKeyState(hold_threshold=1.0)
    state.on_press(0.0)
    assert state.poll_hold(0.5) is None
    assert state.poll_hold(1.0) == ptt_core.PTTAction.HOLD_START
    # Continuing to poll while still held must not fire again
    assert state.poll_hold(1.5) is None


def test_release_after_hold_returns_hold_release():
    state = ptt_core.PTTKeyState(hold_threshold=1.0)
    state.on_press(0.0)
    state.poll_hold(1.0)
    assert state.on_release(1.6) == ptt_core.PTTAction.HOLD_RELEASE


def test_spurious_release_returns_none():
    state = ptt_core.PTTKeyState(hold_threshold=1.0)
    assert state.on_release(5.0) is None


def test_state_resets_after_release_for_next_cycle():
    state = ptt_core.PTTKeyState(hold_threshold=1.0)
    state.on_press(0.0)
    state.on_release(0.3)
    state.on_press(1.0)
    assert state.on_release(1.2) == ptt_core.PTTAction.SHORT_PRESS
