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
