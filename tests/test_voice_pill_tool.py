"""The voice_pill MCP tool (patches/voice_barge_tool.py): show, hide or check the pill."""
import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest

PATCHES = Path(__file__).resolve().parent.parent / "patches"


@pytest.fixture()
def tool(monkeypatch, tmp_path):
    """(module, state, fake autostart, registered tools), the state under a throwaway HOME."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("VOICEMODE_BASE_DIR", raising=False)
    monkeypatch.setenv("VOICEMODE_PILL", "true")
    spec = importlib.util.spec_from_file_location("vps_tool", PATCHES / "voice_pill_state.py")
    st = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(st)
    monkeypatch.setattr(st, "upstream_home", lambda: tmp_path / "home" / ".voicemode")

    pa = types.SimpleNamespace(calls=[], alive=False, result=True, error=None)

    def ensure(base=None):
        pa.calls.append(base)
        if pa.error:
            raise pa.error
        return pa.result

    pa.ensure = ensure
    pa.backend_alive = lambda base=None: pa.alive
    pa.kind, pa.autostart, pa.mac_venv = "windows", True, tmp_path / "pill-venv-python"
    pa.platform_kind = lambda: pa.kind
    pa.autostart_on = lambda: pa.autostart
    pa.mac_python = lambda: str(pa.mac_venv)
    pa.LAUNCH_SPACING_S = 20.0

    tools = []
    server = types.ModuleType("voice_mode.server")
    server.mcp = types.SimpleNamespace(tool=lambda: (lambda f: tools.append(f) or f))
    barge = types.ModuleType("voice_mode.voice_barge")
    barge.barge_control = lambda a: a
    pkg = types.ModuleType("voice_mode")
    pkg.voice_pill_state, pkg.pill_autostart = st, pa
    for name, mod in (("voice_mode", pkg), ("voice_mode.server", server),
                      ("voice_mode.voice_barge", barge), ("voice_mode.voice_pill_state", st),
                      ("voice_mode.pill_autostart", pa)):
        monkeypatch.setitem(sys.modules, name, mod)
    spec = importlib.util.spec_from_file_location("vbt_tool", PATCHES / "voice_barge_tool.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, st, pa, tools


def run(mod, action="status"):
    return asyncio.run(mod.voice_pill(action))


def test_registered_on_mcp(tool):
    mod, _, _, tools = tool
    assert mod.voice_pill in tools
    assert "pill" in mod.voice_pill.__doc__


def test_show_clears_closed_and_starts_the_window(tool):
    mod, st, pa, _ = tool
    st.set_pill_closed(True)
    out = run(mod, "show")
    assert not st.pill_closed() and len(pa.calls) == 1
    assert "start" in out.lower() or "shown" in out.lower()


def test_show_with_a_window_already_up(tool):
    mod, st, pa, _ = tool
    st.set_pill_closed(True)
    pa.alive, pa.result = True, False
    out = run(mod, "show")
    assert not st.pill_closed()
    assert "already" in out.lower()


def test_show_names_why_the_window_was_not_started(tool):
    """A chat asked to open the pill must be able to say why it did not: the reasons are the ones
    pill_autostart.should_start/ensure already apply, so the tool names them, and a start that is
    already under way is not reported as a failure."""
    mod, _, pa, _ = tool
    pa.result = False
    pa.kind = None
    assert "no pill window on this system" in run(mod, "show")
    pa.kind, pa.autostart = "windows", False
    assert "VOICEMODE_PILL_AUTOSTART" in run(mod, "show")
    pa.kind, pa.autostart = "macos", True   # mac_venv does not exist
    out = run(mod, "show")
    assert "not set up" in out and "voicemode-switch" not in out
    pa.kind = "windows"
    out = run(mod, "show")
    assert "already" in out and "starting" in out and "could not" not in out.lower()


def test_show_with_the_pill_off_does_not_start_it(tool, monkeypatch):
    mod, st, pa, _ = tool
    monkeypatch.setenv("VOICEMODE_PILL", "false")
    out = run(mod, "show")
    assert pa.calls == []
    # Say what is off, never a command only some installs ship (FELIX review of v1.2.7).
    assert "VOICEMODE_PILL" in out and "voicemode-switch" not in out


def test_hide_sets_closed(tool):
    mod, st, _, _ = tool
    out = run(mod, "hide")
    assert st.pill_closed()
    assert "hidden" in out and "show the pill" in out


def test_status_reports_the_three_facts(tool):
    mod, st, pa, _ = tool
    out = run(mod, "status")
    assert "enabled" in out and "not closed" in out and "no window" in out
    pa.alive = True
    st.set_pill_closed(True)
    out = run(mod, "status")
    assert "window is running" in out and "closed by the user" in out and "enabled" in out


def test_status_with_the_pill_off(tool, monkeypatch):
    mod, _, _, _ = tool
    monkeypatch.setenv("VOICEMODE_PILL", "false")
    assert "turned off" in run(mod, "status")


def test_default_action_is_status(tool):
    mod, _, _, _ = tool
    assert asyncio.run(mod.voice_pill()) == run(mod, "status")


@pytest.mark.parametrize("action", ["open", "", "SHOW me", None])
def test_unknown_action_lists_the_valid_ones(tool, action):
    mod, st, pa, _ = tool
    out = run(mod, action)
    assert "show" in out and "hide" in out and "status" in out
    assert pa.calls == [] and not st.pill_closed()


def test_action_is_case_and_space_tolerant(tool):
    mod, st, _, _ = tool
    run(mod, "  HIDE ")
    assert st.pill_closed()


def test_nothing_raises_when_ensure_raises(tool):
    mod, _, pa, _ = tool
    pa.error = OSError("no powershell")
    out = run(mod, "show")
    assert isinstance(out, str) and "no powershell" in out and "autostart.log" in out


def test_no_answer_names_a_command_only_some_installs_ship(tool, monkeypatch):
    """FELIX's review of v1.2.7: `voicemode-switch` is voicemode-local's own script; FELIX and other
    harnesses do not ship it, so advice to run it is a dead end there."""
    mod, st, pa, _ = tool
    pa.result = False
    outs = []
    for kind, autostart in ((None, True), ("windows", False), ("macos", True), ("windows", True)):
        pa.kind, pa.autostart = kind, autostart
        outs.append(run(mod, "show"))
    monkeypatch.setenv("VOICEMODE_PILL", "false")
    outs += [run(mod, "show"), run(mod, "status"), run(mod, "hide")]
    assert not [o for o in outs if "voicemode-switch" in o]
