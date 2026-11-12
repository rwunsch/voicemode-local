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


def test_wake_text_names_a_message_and_stays_short():
    """voice-mode 8.12's converse refuses a call with no `message` (live Mac test 2026-11-07:
    "converse needs either `message` ... or a non-empty `turns` list"). The Mac types this
    line into Terminal, and a long line is taken for a paste and never submitted."""
    assert 'message="Listening."' in mac.WAKE_TEXT
    assert "skip_tts" not in mac.WAKE_TEXT  # the woken session speaks the cue
    assert len(mac.WAKE_TEXT) <= 60


# --- Talos review 2026-11-12: stopping the listener mid-hold ------------------

def _run_with_fake_keyboard(monkeypatch, during_join):
    """Run run_listener with fake pynput, Quartz and voice_mode modules."""
    import signal
    import sys
    import types

    sent, signals = [], {}
    Key = types.SimpleNamespace(ctrl="ctrl", ctrl_l="ctrl_l", ctrl_r="ctrl_r",
                                alt="alt", alt_l="alt_l", alt_r="alt_r", space="space")

    class FakeListener:
        def __init__(self, on_press, on_release, **_):
            self.on_press, self.on_release = on_press, on_release

        def start(self):
            pass

        def join(self):
            during_join(self)

        def stop(self):
            pass

    keyboard = types.SimpleNamespace(Key=Key, Listener=FakeListener)
    pynput = types.ModuleType("pynput")
    pynput.keyboard = keyboard
    quartz = types.ModuleType("Quartz")
    for i, name in enumerate(("kCGEventFlagMaskControl", "kCGEventFlagMaskAlternate",
                              "kCGEventFlagMaskCommand", "kCGEventFlagMaskShift")):
        setattr(quartz, name, 1 << i)
    client = types.SimpleNamespace(available=lambda: True, socket_path=lambda: "/x",
                                   on_action=sent.append, send=sent.append)
    vm = types.ModuleType("voice_mode")
    vm.ptt_core, vm.ptt_control_client = core, client
    for name, mod in (("pynput", pynput), ("pynput.keyboard", keyboard), ("Quartz", quartz),
                      ("voice_mode", vm), ("voice_mode.ptt_core", core),
                      ("voice_mode.ptt_control_client", client)):
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.setattr(mac, "touch_pill", lambda: None)
    monkeypatch.setattr(signal, "signal", lambda sig, fn: signals.__setitem__(sig, fn))
    mac.run_listener()
    return sent, signals


def test_stopping_mid_hold_sends_hold_release(monkeypatch):
    """Killing the listener while Space is held must not leave the server's hold
    latched (mic open). Sent at once: no release tail, the process is exiting."""
    def during_join(fake):
        fake.on_press("ctrl_l")
        fake.on_press("alt_l")
        fake.on_press("space")
        raise KeyboardInterrupt

    sent, _ = _run_with_fake_keyboard(monkeypatch, during_join)
    assert sent == ["hold_start", "hold_release"]


def test_stopping_with_no_hold_sends_nothing(monkeypatch):
    def during_join(fake):
        fake.on_press("ctrl_l")
        raise KeyboardInterrupt

    sent, _ = _run_with_fake_keyboard(monkeypatch, during_join)
    assert sent == []


def test_sigterm_and_sighup_stop_the_listener_like_ctrl_c(monkeypatch):
    import signal

    def during_join(fake):
        raise KeyboardInterrupt

    _, signals = _run_with_fake_keyboard(monkeypatch, during_join)
    assert {signal.SIGTERM, signal.SIGHUP} <= set(signals)
    with pytest.raises(KeyboardInterrupt):
        signals[signal.SIGHUP](signal.SIGHUP, None)


def test_stopping_during_the_release_tail_still_sends_hold_release_once():
    # Final review minor: the release waits a short tail; stopping in that window lost it,
    # and the server's hold stayed latched with the mic open.
    rig = Rig()
    rig.press(5.0, 7.0)                       # a hold: its release waits for the tail timer
    assert rig.sent == ["hold_start"] and len(rig.timers) == 1
    rig.key.release_held(7.1)                 # the listener stops inside the tail
    assert rig.sent == ["hold_start", "hold_release"]
    rig.flush()                               # the timer fires late: nothing more
    assert rig.sent == ["hold_start", "hold_release"]
