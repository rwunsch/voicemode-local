"""patches/ptt_listener_macos.py's key rule, without a Mac: the pure MacHotkey.

Robert, 2026-11-07: Control-Option-Space down opens the mic at once; released within
ptt_core.TAP_MAX_S (0.8 s) it is a full stop; with no voice call running, only a HOLD
wakes the last session, a tap does nothing. Control-Option-Escape (stop) is in the
Quartz intercept and is not touched by this class.
"""
import importlib.util
from pathlib import Path

import pytest

PATCHES = Path(__file__).resolve().parent.parent / "patches"


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, PATCHES / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mac = _load("vml_ptt_listener_macos", "ptt_listener_macos.py")
core = _load("vml_ptt_core_mac", "ptt_core.py")


class Rig:
    def __init__(self, available=True, tail=0.35, wake_enabled=True):
        self.sent, self.wakes, self.timers, self.touches = [], [], [], 0
        self.available = available
        self.key = mac.MacHotkey(
            core.PTTKeyState(), emit=self.sent.append, available=lambda: self.available,
            wake=lambda: self.wakes.append(1), timer=lambda delay, fn: self.timers.append((delay, fn)),
            touch=self._touch, release_tail=tail, wake_enabled=wake_enabled)

    def _touch(self):
        self.touches += 1

    def press(self, down, up, repeat=True):
        self.key.space(True, down, mods=True)
        if repeat:
            self.key.space(True, down + 0.3, mods=True)  # auto-repeat
        self.key.space(False, up, mods=True)

    def flush(self):
        for _, fn in self.timers:
            fn()
        self.timers.clear()


def test_down_sends_hold_start_at_once():
    rig = Rig()
    rig.key.space(True, 5.0, mods=True)
    assert rig.sent == ["hold_start"] and rig.touches == 1


def test_a_tap_is_a_stop_and_is_not_delayed():
    rig = Rig()
    rig.press(5.0, 5.5)
    assert rig.sent == ["hold_start", "short_press"] and rig.timers == []


@pytest.mark.parametrize("held_for", [1.2, 0.8])
def test_a_hold_ends_after_the_release_tail(held_for):
    rig = Rig()
    rig.press(5.0, 5.0 + held_for)
    assert rig.sent == ["hold_start"]
    assert [d for d, _ in rig.timers] == [0.35]  # the last syllable is kept
    rig.flush()
    assert rig.sent == ["hold_start", "hold_release"]


def test_no_tail_sends_hold_release_at_once():
    rig = Rig(tail=0)
    rig.press(5.0, 6.2)
    assert rig.sent == ["hold_start", "hold_release"]


def test_space_without_the_modifiers_does_nothing():
    rig = Rig()
    rig.key.space(True, 1.0, mods=False)
    rig.key.space(False, 2.0, mods=False)
    assert rig.sent == [] and rig.wakes == []


def test_idle_hold_wakes_the_last_session():
    rig = Rig(available=False)
    rig.press(5.0, 6.2)
    assert rig.wakes == [1] and rig.sent == []


def test_idle_tap_does_nothing():
    rig = Rig(available=False)
    rig.press(5.0, 5.5)
    assert rig.wakes == [] and rig.sent == []


def test_idle_wake_has_a_cooldown_and_a_switch():
    rig = Rig(available=False)
    rig.press(5.0, 6.0)
    rig.press(7.0, 8.0)  # within WAKE_COOLDOWN of the first
    assert rig.wakes == [1]
    off = Rig(available=False, wake_enabled=False)
    off.press(5.0, 6.0)
    assert off.wakes == []


def test_the_listener_no_longer_polls_a_threshold():
    src = (PATCHES / "ptt_listener_macos.py").read_text(encoding="utf-8")
    assert "poll_hold" not in src and "HOLD_THRESHOLD" not in src
