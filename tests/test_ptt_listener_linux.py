"""Tests for patches/ptt_listener_linux.py.

The listener is now a thin synchronous client of upstream's control socket, so
what is worth testing is the pure part: terminal-focus scoping (PTT must not
fire while another window has focus) and PID resolution. The key-capture loop
itself needs a real X11 display and pynput, so it is exercised manually.
"""
import importlib.util
import subprocess
from pathlib import Path
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def listener():
    """Import the module directly — its pure helpers must not need voice_mode."""
    spec = importlib.util.spec_from_file_location(
        "vml_ptt_listener_linux", REPO / "patches" / "ptt_listener_linux.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(stdouts):
    """Fake subprocess.run returning successive stdout values."""
    it = iter(stdouts)
    def fake(*a, **k):
        return mock.Mock(stdout=next(it), returncode=0)
    return fake


# --- focus scoping ----------------------------------------------------------

def test_focused_terminal_matches(listener):
    with mock.patch.object(subprocess, "run", _run(["12345", "999"])):
        assert listener.is_focused_terminal(999) is True


def test_focused_other_window_does_not_match(listener):
    """The whole point: Ctrl-Space in another app must not grab the mic."""
    with mock.patch.object(subprocess, "run", _run(["12345", "4242"])):
        assert listener.is_focused_terminal(999) is False


def test_missing_xdotool_is_not_focused(listener):
    with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError):
        assert listener.is_focused_terminal(999) is False


def test_xdotool_failure_is_not_focused(listener):
    with mock.patch.object(subprocess, "run",
                           side_effect=subprocess.CalledProcessError(1, "xdotool")):
        assert listener.is_focused_terminal(999) is False


def test_xdotool_timeout_is_not_focused(listener):
    with mock.patch.object(subprocess, "run",
                           side_effect=subprocess.TimeoutExpired("xdotool", 1)):
        assert listener.is_focused_terminal(999) is False


def test_nonnumeric_pid_is_not_focused(listener):
    with mock.patch.object(subprocess, "run", _run(["12345", "not-a-pid"])):
        assert listener.is_focused_terminal(999) is False


# --- terminal PID resolution ------------------------------------------------

def test_resolve_terminal_pid_walks_to_the_top(listener):
    import psutil
    top = mock.Mock(pid=100); top.parent.return_value = mock.Mock(pid=1)
    mid = mock.Mock(pid=200); mid.parent.return_value = top
    leaf = mock.Mock(pid=300); leaf.parent.return_value = mid
    with mock.patch.object(psutil, "Process", return_value=leaf):
        assert listener.resolve_terminal_pid() == 100


def test_resolve_terminal_pid_survives_a_psutil_error(listener):
    import psutil
    with mock.patch.object(psutil, "Process", side_effect=psutil.Error()):
        assert listener.resolve_terminal_pid() is None


# --- shape ------------------------------------------------------------------

def test_listener_no_longer_depends_on_the_retired_event_bus(listener):
    """Check IMPORTS, not prose — the module docstring legitimately names both
    ptt_ipc and asyncio while explaining what it stopped using."""
    import ast
    src = (REPO / "patches" / "ptt_listener_linux.py").read_text()
    imported = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module.split(".")[0])
            imported.update(a.name for a in node.names)
    assert "ptt_ipc" not in imported, "still importing the retired event bus"
    assert "asyncio" not in imported, "the listener should be synchronous now"
    assert "ptt_control_client" in imported


# --- the key rule (2026-11-07): down opens the mic, a release within 0.8 s stops ---

def _core():
    spec = importlib.util.spec_from_file_location("vml_ptt_core_linux", REPO / "patches" / "ptt_core.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _hotkey(listener, focused=True):
    sent = []
    key = listener.Hotkey(_core().PTTKeyState(), sent.append, lambda: focused)
    return key, sent


def test_ctrl_space_down_sends_hold_start_at_once(listener):
    key, sent = _hotkey(listener)
    key.ctrl(True)
    key.space(True, 10.0)
    assert sent == ["hold_start"]  # no threshold wait


@pytest.mark.parametrize("held_for,expected", [(0.5, ["hold_start", "short_press"]),
                                               (1.2, ["hold_start", "hold_release"]),
                                               (0.8, ["hold_start", "hold_release"])])
def test_release_time_decides_tap_or_talk(listener, held_for, expected):
    key, sent = _hotkey(listener)
    key.ctrl(True)
    key.space(True, 10.0)
    key.space(True, 10.3)  # auto-repeat
    key.space(False, 10.0 + held_for)
    assert sent == expected


def test_space_without_ctrl_or_focus_does_nothing(listener):
    key, sent = _hotkey(listener)
    key.space(True, 1.0)
    key.space(False, 2.0)
    key, sent2 = _hotkey(listener, focused=False)
    key.ctrl(True)
    key.space(True, 1.0)
    key.space(False, 2.0)
    assert sent == [] and sent2 == []


def test_the_listener_no_longer_polls_a_threshold(listener):
    src = (REPO / "patches" / "ptt_listener_linux.py").read_text()
    assert "poll_hold" not in src and "HOLD_THRESHOLD" not in src


# --- Talos review 2026-11-12: two Ctrl keys, and stopping mid-hold ------------

def test_releasing_one_of_two_ctrl_keys_keeps_the_hotkey(listener):
    """Both Ctrl keys down, one let go: Ctrl is still down, so Ctrl-Space works.
    One Boolean for both keys used to drop the hotkey here."""
    key, sent = _hotkey(listener)
    key.ctrl(True, "ctrl_l")
    key.ctrl(True, "ctrl_r")
    key.ctrl(False, "ctrl_l")
    key.space(True, 10.0)
    key.space(False, 11.2)
    assert sent == ["hold_start", "hold_release"]


def test_releasing_both_ctrl_keys_drops_the_hotkey(listener):
    key, sent = _hotkey(listener)
    key.ctrl(True, "ctrl_l")
    key.ctrl(True, "ctrl_r")
    key.ctrl(False, "ctrl_l")
    key.ctrl(False, "ctrl_r")
    key.space(True, 10.0)
    assert sent == []


def _run_with_fake_keyboard(listener, monkeypatch, during_join):
    """Run run_listener with fake pynput and voice_mode modules. ``during_join``
    gets the fake listener (its on_press/on_release) and raises to end the run."""
    import sys
    import types

    sent, signals = [], {}
    Key = types.SimpleNamespace(ctrl_l="ctrl_l", ctrl_r="ctrl_r", space="space")

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
    client = types.SimpleNamespace(available=lambda: True, socket_path=lambda: "/x",
                                   on_action=sent.append)
    vm = types.ModuleType("voice_mode")
    vm.ptt_core, vm.ptt_control_client = _core(), client
    for name, mod in (("pynput", pynput), ("pynput.keyboard", keyboard), ("voice_mode", vm),
                      ("voice_mode.ptt_core", vm.ptt_core),
                      ("voice_mode.ptt_control_client", client)):
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.setattr(listener, "resolve_terminal_pid", lambda pid=None: 4242)
    monkeypatch.setattr(listener, "is_focused_terminal", lambda pid: True)
    import signal
    monkeypatch.setattr(signal, "signal", lambda sig, fn: signals.__setitem__(sig, fn))
    listener.run_listener()
    return sent, signals


def test_stopping_mid_hold_sends_hold_release(listener, monkeypatch):
    """Killing the listener while Space is held must not leave the server's
    hold latched (mic open): the finally sends hold_release."""
    def during_join(fake):
        fake.on_press("ctrl_l")
        fake.on_press("space")
        raise KeyboardInterrupt

    sent, _ = _run_with_fake_keyboard(listener, monkeypatch, during_join)
    assert sent == ["hold_start", "hold_release"]


def test_stopping_with_no_hold_sends_nothing(listener, monkeypatch):
    def during_join(fake):
        fake.on_press("ctrl_l")
        raise KeyboardInterrupt

    sent, _ = _run_with_fake_keyboard(listener, monkeypatch, during_join)
    assert sent == []


def test_sigterm_and_sighup_stop_the_listener_like_ctrl_c(listener, monkeypatch):
    import signal

    def during_join(fake):
        raise KeyboardInterrupt

    _, signals = _run_with_fake_keyboard(listener, monkeypatch, during_join)
    assert {signal.SIGTERM, signal.SIGHUP} <= set(signals)
    with pytest.raises(KeyboardInterrupt):
        signals[signal.SIGTERM](signal.SIGTERM, None)
