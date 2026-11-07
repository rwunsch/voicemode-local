"""Shared state files for the voice pill: sessions, stop flags, pause, overrides."""
import importlib.util
import os
import stat
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parent.parent / "patches" / "voice_pill_state.py"
spec = importlib.util.spec_from_file_location("voice_pill_state", MODULE)
st = importlib.util.module_from_spec(spec)
sys.modules["voice_pill_state"] = st
spec.loader.exec_module(st)


@pytest.fixture
def base(tmp_path):
    return tmp_path / "pill"


def dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_live_session_is_listed(base):
    st.register_session(os.getpid(), "demo", "af_sky", base=base)
    (s,) = st.live_sessions(base=base)
    assert (s["pid"], s["label"], s["voice"]) == (os.getpid(), "demo", "af_sky")
    assert {"started_at", "last_voice_at"} <= set(s)


def test_touch_updates_last_voice_and_voice(base):
    st.register_session(os.getpid(), "demo", None, base=base)
    st.touch_session(os.getpid(), "bm_daniel", base=base)
    (s,) = st.live_sessions(base=base)
    assert s["voice"] == "bm_daniel" and s["last_voice_at"] is not None


def test_unregister_removes_session(base):
    st.register_session(os.getpid(), "demo", None, base=base)
    st.unregister_session(os.getpid(), base=base)
    assert st.live_sessions(base=base) == []


def test_dead_pid_session_is_removed(base):
    pid = dead_pid()
    st.register_session(pid, "ghost", None, base=base)
    assert st.live_sessions(base=base) == []
    assert not list((base / "sessions").glob("*.json"))


def test_reused_pid_with_different_start_time_is_removed(base, monkeypatch):
    st.register_session(os.getpid(), "old", None, base=base)
    monkeypatch.setattr(st, "process_start", lambda pid: "a-different-start")
    assert st.live_sessions(base=base) == []
    assert not list((base / "sessions").glob("*.json"))


def test_take_stop_is_consumed_once(base):
    assert st.take_stop(7, base=base) is False
    st.request_stop(7, base=base)
    assert st.take_stop(7, base=base) is True
    assert st.take_stop(7, base=base) is False


def test_paused_with_live_owner_is_paused(base):
    st.set_paused(True, os.getpid(), base=base)
    assert st.is_paused(base=base) is True
    st.set_paused(False, os.getpid(), base=base)
    assert st.is_paused(base=base) is False


def test_paused_with_dead_owner_is_not_paused(base):
    st.set_paused(True, dead_pid(), base=base)
    assert st.is_paused(base=base) is False


def test_paused_with_reused_owner_pid_is_not_paused(base, monkeypatch):
    st.set_paused(True, os.getpid(), base=base)
    monkeypatch.setattr(st, "process_start", lambda pid: "other")
    assert st.is_paused(base=base) is False


def test_override_round_trips_and_clears(base):
    assert st.get_override(3, base=base) == {}
    st.set_override(3, "af_bella", "de", base=base)
    assert st.get_override(3, base=base) == {"voice": "af_bella", "language": "de"}
    st.set_override(3, None, None, base=base)
    assert st.get_override(3, base=base) == {}


def test_interruptions_counted_since_time(base):
    for t in (10.0, 20.0, 30.0):
        st.record_interruption(t, base=base)
    assert st.interruptions_since(15.0, base=base) == 2
    assert st.interruptions_since(100.0, base=base) == 0


def test_ptt_recent_expires_after_120s(base):
    assert st.ptt_recent(base=base) is False
    st.touch_ptt(base=base)
    assert st.ptt_recent(base=base) is True
    old = time.time() - 121
    os.utime(base / "ptt", (old, old))
    assert st.ptt_recent(base=base) is False
    assert st.ptt_recent(window_s=300, base=base) is True


def test_ui_state_round_trips(base):
    assert st.load_ui(base=base) == {}
    ui = {"x": 10, "y": 20, "collapsed": True}
    st.save_ui(ui, base=base)
    assert st.load_ui(base=base) == ui


def test_corrupt_files_read_as_empty(base):
    st.register_session(os.getpid(), "demo", None, base=base)
    (base / "sessions" / "999999.json").write_text("{half")
    st.set_override(3, "v", None, base=base)
    (base / "override-3.json").write_text("not json")
    (base / "paused.json").write_text("\x00\x01")
    (base / "ui.json").write_text("[1, 2]")
    (base / "interruptions.log").write_text("{")
    assert [s["pid"] for s in st.live_sessions(base=base)] == [os.getpid()]
    assert st.get_override(3, base=base) == {}
    assert st.is_paused(base=base) is False
    assert st.load_ui(base=base) == {}
    assert st.interruptions_since(0, base=base) == 0
    st.record_interruption(5.0, base=base)  # recovers by overwriting
    assert st.interruptions_since(0, base=base) == 1


def test_state_dir_is_0700(base):
    st.touch_ptt(base=base)
    assert stat.S_IMODE(base.stat().st_mode) == 0o700


def test_default_base_is_under_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert st.default_base() == tmp_path / ".voicemode" / "pill"


def _fake_mac_ps(monkeypatch, stdout):
    """Make process_start take its non-Linux path; returns the list of spawns."""
    spawns = []

    def run(cmd, **_):
        spawns.append(cmd)
        return types.SimpleNamespace(stdout=stdout)

    monkeypatch.setattr(st.sys, "platform", "darwin")
    monkeypatch.setattr(st.subprocess, "run", run)
    return spawns


def test_dead_pause_owner_costs_one_ps_spawn_only(base, monkeypatch):
    st.set_paused(True, dead_pid(), base=base)
    spawns = _fake_mac_ps(monkeypatch, "")
    assert st.is_paused(base=base) is False
    assert len(spawns) == 1
    assert st.is_paused(base=base) is False
    assert len(spawns) == 1
    assert not (base / "paused.json").exists()


def test_reused_pause_owner_costs_one_ps_spawn_only(base, monkeypatch):
    st.set_paused(True, os.getpid(), base=base)
    spawns = _fake_mac_ps(monkeypatch, "a different start time")
    assert st.is_paused(base=base) is False
    assert st.is_paused(base=base) is False
    assert len(spawns) == 1
    assert not (base / "paused.json").exists()


def test_live_pause_owner_keeps_the_pause_file(base):
    st.set_paused(True, os.getpid(), base=base)
    assert st.is_paused(base=base) is True
    assert (base / "paused.json").exists()


def test_stopped_session_tells_the_agent_until_it_comes_back(base, monkeypatch):
    monkeypatch.setattr(st, "barge_state", lambda home=None: None)
    st.register_session(os.getpid(), "demo", None, base=base)
    assert st.result_note(os.getpid(), base=base) == ""
    st.mark_stopped(os.getpid(), True, base=base)
    assert st.STOPPED_MESSAGE in st.result_note(os.getpid(), base=base)
    st.mark_stopped(os.getpid(), False, base=base)
    assert st.result_note(os.getpid(), base=base) == ""


def test_marking_an_unknown_session_creates_nothing(base):
    st.mark_stopped(424242, True, base=base)
    assert st.live_sessions(base=base) == []


def test_beeps_have_no_choice_until_one_is_made(base):
    assert st.beeps_choice(base) is None
    assert st.effective_beeps(None, base) is None
    assert st.effective_beeps(True, base) is True


def test_beeps_off_wins_over_everything(base):
    st.set_beeps(False, base)
    assert st.beeps_choice(base) is False
    assert st.effective_beeps(None, base) is False
    assert st.effective_beeps(True, base) is False


def test_beeps_on_wins_over_the_env_but_not_an_explicit_silence(base):
    st.set_beeps(True, base)
    assert st.effective_beeps(None, base) is True
    assert st.effective_beeps(False, base) is False  # a voice cut skips its chime on purpose


def test_a_bad_beeps_file_means_no_choice(base):
    Path(base).mkdir(parents=True, exist_ok=True)
    (Path(base) / "beeps.json").write_text('{"on": "yes"}')
    assert st.beeps_choice(base) is None


def test_barge_finds_the_switch_when_the_venv_is_not_in_the_repo(monkeypatch):
    # The Mac pill venv and test venvs live outside the repo; the toggle silently did nothing there.
    monkeypatch.setattr(sys, "prefix", "/nonexistent/venv")
    assert st.default_switch() == MODULE.resolve().parent.parent / "voicemode-switch"
    assert st.default_switch().exists()


def test_barge_still_finds_the_switch_next_to_a_repo_venv(monkeypatch, tmp_path):
    monkeypatch.setattr(st, "__file__", str(tmp_path / "site-packages" / "voice_mode" / "voice_pill_state.py"))
    monkeypatch.setattr(sys, "prefix", str(tmp_path / ".venv"))
    (tmp_path / "voicemode-switch").write_text("")
    assert st.default_switch() == tmp_path / "voicemode-switch"



# Robert, 2026-11-05: at voice start, say once whether interrupt by voice is on, and that it can
# be switched, like the Beeps switch.
def test_the_first_result_says_once_that_barge_is_on(base, monkeypatch):
    monkeypatch.setattr(st, "barge_state", lambda home=None: True)
    st.register_session(os.getpid(), "demo", None, base=base)
    first = st.result_note(os.getpid(), base=base)
    assert "Interrupt by voice is on" in first and "voice_barge" in first
    assert st.result_note(os.getpid(), base=base) == ""


def test_the_first_result_says_when_barge_is_off(base, monkeypatch):
    monkeypatch.setattr(st, "barge_state", lambda home=None: False)
    st.register_session(os.getpid(), "demo", None, base=base)
    assert "Interrupt by voice is off" in st.result_note(os.getpid(), base=base)


def test_no_barge_note_where_barge_cannot_run(base, monkeypatch):
    monkeypatch.setattr(st, "barge_state", lambda home=None: None)
    st.register_session(os.getpid(), "demo", None, base=base)
    assert st.result_note(os.getpid(), base=base) == ""


def test_no_barge_note_for_an_unregistered_session(base, monkeypatch):
    monkeypatch.setattr(st, "barge_state", lambda home=None: True)
    assert st.result_note(os.getpid(), base=base) == ""


def test_barge_state_reads_the_last_setting_and_needs_a_mic_reader(tmp_path, monkeypatch):
    monkeypatch.delenv("VOICEMODE_VOICE_BARGE", raising=False)
    (tmp_path / "voicemode.env").write_text(
        "VOICEMODE_VOICE_BARGE=false\n# VOICEMODE_VOICE_BARGE=false\nVOICEMODE_VOICE_BARGE=true\n")
    monkeypatch.setattr(st.shutil, "which", lambda name: "/usr/bin/parec")
    assert st.barge_state(home=tmp_path) is True
    (tmp_path / "voicemode.env").write_text("")
    assert st.barge_state(home=tmp_path) is False
    monkeypatch.setattr(st.shutil, "which", lambda name: None)
    monkeypatch.setattr(st.importlib.util, "find_spec", lambda name: None)
    assert st.barge_state(home=tmp_path) is None  # neither parec nor sounddevice


def test_barge_state_falls_back_to_the_environment_when_the_file_lacks_the_key(tmp_path, monkeypatch):
    """FELIX passes VOICEMODE_VOICE_BARGE in the server's environment, not in a file."""
    (tmp_path / "voicemode.env").write_text("VOICEMODE_TTS_VOICE=am_puck\n")
    monkeypatch.setattr(st.shutil, "which", lambda name: "/usr/bin/parec")
    monkeypatch.setenv("VOICEMODE_VOICE_BARGE", "true")
    assert st.barge_state(home=tmp_path) is True
    monkeypatch.setenv("VOICEMODE_VOICE_BARGE", "false")
    assert st.barge_state(home=tmp_path) is False


def test_barge_state_file_still_wins_over_the_environment(tmp_path, monkeypatch):
    """The switch writes the file while the server runs; its env is from start-up."""
    (tmp_path / "voicemode.env").write_text("VOICEMODE_VOICE_BARGE=false\n")
    monkeypatch.setattr(st.shutil, "which", lambda name: "/usr/bin/parec")
    monkeypatch.setenv("VOICEMODE_VOICE_BARGE", "true")
    assert st.barge_state(home=tmp_path) is False


def test_barge_state_reads_upstreams_home_not_the_base_dir(tmp_path, monkeypatch):
    """Upstream loads voicemode.env from Path.home()/.voicemode whatever VOICEMODE_BASE_DIR
    says; FELIX gives each runtime its own HOME, so that is the file to read."""
    home = tmp_path / "runtime-home"
    (home / ".voicemode").mkdir(parents=True)
    (home / ".voicemode" / "voicemode.env").write_text("VOICEMODE_VOICE_BARGE=true\n")
    base = tmp_path / "base"
    base.mkdir()
    (base / "voicemode.env").write_text("VOICEMODE_VOICE_BARGE=false\n")
    monkeypatch.setattr(st.shutil, "which", lambda name: "/usr/bin/parec")
    monkeypatch.delenv("VOICEMODE_VOICE_BARGE", raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("VOICEMODE_BASE_DIR", str(base))
    assert st.barge_state() is True


# -- barge_available: can the trigger's interpreter hear the mic? -------------

def _fake_python(tmp_path, line, sleep=0, counter=None):
    p = tmp_path / "fakepy"
    body = "#!/bin/sh\n"
    if counter:
        body += f"echo x >> {counter}\n"
    if sleep:
        body += f"sleep {sleep}\n"
    body += f"echo '{line}'\n"
    p.write_text(body)
    p.chmod(0o755)
    return p


def _settle(timeout=15):
    t = st._BARGE_PROBE.get("thread")
    if t:
        t.join(timeout)


@pytest.fixture
def no_inproc(monkeypatch):
    st._barge_probe_reset()
    monkeypatch.setattr(st, "capture_backend", lambda: None)
    yield
    st._barge_probe_reset()


def test_barge_available_true_in_process(monkeypatch):
    st._barge_probe_reset()
    monkeypatch.setattr(st, "capture_backend", lambda: "sounddevice")
    monkeypatch.setattr(st, "barge_interpreter", lambda: pytest.fail("must not probe"))
    assert st.barge_available() is True


@pytest.mark.parametrize("line,expected", [("on", True), ("off", True),
                                           ("cannot run: no mic reader", False)])
def test_barge_available_asks_the_interpreter(no_inproc, monkeypatch, tmp_path, line, expected):
    monkeypatch.setattr(st, "barge_interpreter", lambda: str(_fake_python(tmp_path, line)))
    st.barge_available()  # first call may still be unknown
    _settle()
    assert st.barge_available() is expected


def test_barge_available_unknown_yet_is_false_and_cached(no_inproc, monkeypatch, tmp_path):
    counter = tmp_path / "count"
    py = _fake_python(tmp_path, "on", sleep=1, counter=counter)
    monkeypatch.setattr(st, "barge_interpreter", lambda: str(py))
    assert st.barge_available() is False  # probe still running
    for _ in range(5):
        st.barge_available()
    _settle()
    assert st.barge_available() is True
    for _ in range(5):
        st.barge_available()
    assert len(counter.read_text().split()) == 1  # one subprocess for the lifetime


def test_barge_available_no_interpreter(no_inproc, monkeypatch):
    monkeypatch.setattr(st, "barge_interpreter", lambda: None)
    assert st.barge_available() is False


def test_barge_available_timeout_is_unavailable(no_inproc, monkeypatch, tmp_path):
    monkeypatch.setattr(st, "BARGE_PROBE_TIMEOUT", 0.5)
    monkeypatch.setattr(st, "barge_interpreter", lambda: str(_fake_python(tmp_path, "on", sleep=5)))
    st.barge_available()
    _settle()
    assert st.barge_available() is False


def test_barge_interpreter_env_then_switch_venv(monkeypatch, tmp_path):
    monkeypatch.delenv("VOICEMODE_BARGE_PYTHON", raising=False)
    monkeypatch.setattr(st, "_python_setting_came_from_a_file", lambda: False)
    switch = tmp_path / "voicemode-switch"
    switch.write_text("")
    monkeypatch.setattr(st, "default_switch", lambda: switch)
    assert st.barge_interpreter() is None
    exe = tmp_path / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    assert st.barge_interpreter() == str(exe)
    explicit = tmp_path / "mine"
    explicit.write_text("")
    monkeypatch.setenv("VOICEMODE_BARGE_PYTHON", str(explicit))
    assert st.barge_interpreter() == str(explicit)


def test_barge_interpreter_refuses_setting_from_a_file(monkeypatch, tmp_path):
    monkeypatch.setenv("VOICEMODE_BARGE_PYTHON", str(tmp_path / "evil"))
    monkeypatch.setattr(st, "_python_setting_came_from_a_file", lambda: True)
    switch = tmp_path / "voicemode-switch"
    switch.write_text("")
    monkeypatch.setattr(st, "default_switch", lambda: switch)
    assert st.barge_interpreter() is None  # no venv beside the switch either
