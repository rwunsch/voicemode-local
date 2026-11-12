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
    monkeypatch.setattr(st, "barge_mode", lambda home=None: None)
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
    monkeypatch.setattr(st, "barge_mode", lambda home=None: "on")
    st.register_session(os.getpid(), "demo", None, base=base)
    first = st.result_note(os.getpid(), base=base)
    assert "Interrupt by voice is on" in first and "voice_barge" in first
    assert st.result_note(os.getpid(), base=base) == ""


def test_the_first_result_says_when_barge_is_off(base, monkeypatch):
    monkeypatch.setattr(st, "barge_mode", lambda home=None: "off")
    st.register_session(os.getpid(), "demo", None, base=base)
    assert "Interrupt by voice is off" in st.result_note(os.getpid(), base=base)


def test_no_barge_note_where_barge_cannot_run(base, monkeypatch):
    monkeypatch.setattr(st, "barge_mode", lambda home=None: None)
    st.register_session(os.getpid(), "demo", None, base=base)
    assert st.result_note(os.getpid(), base=base) == ""


def test_no_barge_note_for_an_unregistered_session(base, monkeypatch):
    monkeypatch.setattr(st, "barge_mode", lambda home=None: "on")
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


# -- closed marker: a user close sticks until voice restarts from scratch ----

def test_closed_marker_round_trip(base):
    assert not st.pill_closed(base=base)
    st.set_pill_closed(True, base=base)
    assert st.pill_closed(base=base)
    st.set_pill_closed(False, base=base)
    assert not st.pill_closed(base=base)


def test_first_session_after_everyone_left_clears_the_marker(base):
    st.set_pill_closed(True, base=base)
    asyncio_run(st.enter(os.getpid(), "demo", None, base=base))
    assert not st.pill_closed(base=base)


def test_marker_stays_while_another_session_is_live(base):
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        st.register_session(other.pid, "other", None, base=base)
        st.set_pill_closed(True, base=base)
        asyncio_run(st.enter(os.getpid(), "demo", None, base=base))
        assert st.pill_closed(base=base)
    finally:
        other.kill()
        other.wait()


def test_a_later_turn_of_a_session_keeps_the_marker(base):
    asyncio_run(st.enter(os.getpid(), "demo", None, base=base))
    st.set_pill_closed(True, base=base)
    asyncio_run(st.enter(os.getpid(), "demo", None, base=base))
    assert st.pill_closed(base=base)


def test_enter_calls_the_autostart_hook_after_registering(base):
    seen = []
    st.autostart_hook = lambda b: seen.append([s["pid"] for s in st.live_sessions(base=b)])
    try:
        asyncio_run(st.enter(os.getpid(), "demo", None, base=base))
    finally:
        st.autostart_hook = None
    assert seen == [[os.getpid()]]


def test_a_failing_autostart_hook_does_not_cost_the_turn(base):
    def boom(b):
        raise RuntimeError("no window today")
    st.autostart_hook = boom
    try:
        assert asyncio_run(st.enter(os.getpid(), "demo", None, base=base)) is None
    finally:
        st.autostart_hook = None


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


# -- Talos review 2026-11-12: settings that name a program are never read from a file ----

def test_setting_came_from_a_file_reads_every_env_file(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".voicemode").mkdir(parents=True)
    (home / ".voicemode" / "voicemode.env").write_text("VOICEMODE_X_CMD=/bin/evil\n")
    monkeypatch.setattr(st, "upstream_home", lambda: home / ".voicemode")
    monkeypatch.chdir(tmp_path)
    assert st.setting_came_from_a_file("VOICEMODE_X_CMD") is True
    assert st.setting_came_from_a_file("VOICEMODE_OTHER") is False


def test_trusted_env_refuses_a_value_a_file_supplied(monkeypatch):
    monkeypatch.setenv("VOICEMODE_X_CMD", "/bin/evil")
    monkeypatch.setattr(st, "setting_came_from_a_file", lambda name: True)
    assert st.trusted_env("VOICEMODE_X_CMD") == ""
    monkeypatch.setattr(st, "setting_came_from_a_file", lambda name: False)
    assert st.trusted_env("VOICEMODE_X_CMD") == "/bin/evil"


# Talos review, finding 1: a non-holder's pill Stop only writes a stop file; a paused
# enter() must see it without waiting for Resume.
def test_stop_during_pause_returns_without_resume(base, monkeypatch):
    import asyncio
    monkeypatch.setattr(st, "autostart_hook", lambda b: None)
    st.set_paused(True, os.getpid(), base=base)  # a live owner: stays paused

    async def scenario():
        task = asyncio.ensure_future(st.enter(os.getpid(), "demo", None, base=base))
        await asyncio.sleep(0.3)
        assert not task.done()  # still waiting on the pause
        st.request_stop(os.getpid(), base=base)
        return await asyncio.wait_for(task, timeout=1.0)

    assert asyncio.run(scenario()) == st.STOPPED_MESSAGE
    assert st.is_paused(base=base) is True


# Talos review, finding 2: auto listens only while output goes to headphones; on speakers
# "talk over you to stop you" is false.
def test_the_first_result_for_auto_mentions_headphones(base, monkeypatch):
    monkeypatch.setattr(st, "barge_mode", lambda home=None: "auto")
    st.register_session(os.getpid(), "demo", None, base=base)
    note = st.result_note(os.getpid(), base=base)
    assert "headphones" in note and "voice_barge" in note
    assert st.BARGE_ON_NOTE not in note
    assert st.result_note(os.getpid(), base=base) == ""


def test_the_first_result_for_on_is_unchanged(base, monkeypatch):
    monkeypatch.setattr(st, "barge_mode", lambda home=None: "on")
    st.register_session(os.getpid(), "demo", None, base=base)
    assert st.result_note(os.getpid(), base=base) == "\n\n" + st.BARGE_ON_NOTE


# Talos review, finding 3: noting that the barge notice was told must never rewrite the
# session record, or a Stop written meanwhile (the pill's mark_stopped) is wiped.
def test_the_barge_notice_never_wipes_a_concurrent_stop(base, monkeypatch):
    pid = os.getpid()
    st.register_session(pid, "demo", None, base=base)

    def mode_with_a_stop_landing(home=None):
        st.mark_stopped(pid, True, base=base)  # the pill's Stop, between read and write
        return "on"

    monkeypatch.setattr(st, "barge_mode", mode_with_a_stop_landing)
    assert st.BARGE_ON_NOTE in st._barge_note_once(pid, base)
    (s,) = st.live_sessions(base=base)
    assert s["stopped"] is True
    monkeypatch.setattr(st, "barge_mode", lambda home=None: "on")
    assert st._barge_note_once(pid, base) == ""  # still told once


def test_a_new_session_with_a_reused_pid_is_told_again(base, monkeypatch):
    monkeypatch.setattr(st, "barge_mode", lambda home=None: "off")
    st.register_session(os.getpid(), "demo", None, base=base)
    assert st.BARGE_OFF_NOTE in st._barge_note_once(os.getpid(), base)
    st.register_session(os.getpid(), "demo", None, base=base)  # registered from scratch
    monkeypatch.setattr(st, "process_start", lambda pid: "another-start")
    st.register_session(os.getpid(), "demo", None, base=base)
    assert st.BARGE_OFF_NOTE in st._barge_note_once(os.getpid(), base)


# Talos review, finding 4: one failed probe hid the barge switch for the backend's lifetime.
def test_barge_available_retries_a_failed_probe(no_inproc, monkeypatch, tmp_path):
    counter = tmp_path / "count"
    py = tmp_path / "flaky-python"
    py.write_text(f"#!/bin/sh\nif [ -e {counter} ]; then echo x >> {counter}; echo on; "
                  f"else echo x > {counter}; echo 'cannot run: not yet'; fi\n")
    py.chmod(0o755)
    monkeypatch.setattr(st, "barge_interpreter", lambda: str(py))
    monkeypatch.setattr(st, "BARGE_RETRY_S", 0, raising=False)
    st.barge_available()
    _settle()
    st.barge_available()  # the failure is old enough: this call probes again
    _settle()
    assert st.barge_available() is True
    assert len(counter.read_text().split()) == 2
    for _ in range(5):  # True is kept for good
        st.barge_available()
    _settle()
    assert len(counter.read_text().split()) == 2


# Talos review, finding 5: voice_stop's unregister_session leaves the override, so a reused
# pid inherited the voice and language. The override now records the process start.
def test_an_override_for_another_process_with_the_same_pid_is_ignored(base, monkeypatch):
    pid = os.getpid()
    st.set_override(pid, "bf_emma", "English", base=base)
    monkeypatch.setattr(st, "process_start", lambda p: "a-reused-pid")
    assert st.get_override(pid, base=base) == {}
    assert not (base / f"override-{pid}.json").exists()


def test_an_override_survives_unregister_for_the_same_process(base):
    pid = os.getpid()
    st.register_session(pid, "demo", None, base=base)
    st.set_override(pid, "bf_emma", "English", base=base)
    st.unregister_session(pid, base=base)
    assert st.get_override(pid, base=base) == {"voice": "bf_emma", "language": "English"}


@pytest.mark.parametrize("raw,want", [("true", True), ("True", True), (" TRUE ", True), ("false", False), ("yes", False)])
def test_pill_enabled_ignores_case_like_the_mac_window(monkeypatch, raw, want):
    # The Mac wrapper reads VOICEMODE_PILL case-insensitively; autostart must agree, or
    # VOICEMODE_PILL=True would never start the window.
    monkeypatch.setenv("VOICEMODE_PILL", raw)
    assert st.pill_enabled() is want


def test_an_unknown_process_start_keeps_the_override_file(base, monkeypatch):
    # Final review I2: ps timing out under load (Mac) gave None, and the user's chosen voice
    # and language were deleted for good. Unknown is not "another process".
    pid = os.getpid()
    st.set_override(pid, "bf_emma", "German", base=base)
    monkeypatch.setattr(st, "process_start", lambda p: None)
    assert st.get_override(pid, base=base) == {}
    assert (base / f"override-{pid}.json").exists()
    monkeypatch.undo()
    assert st.get_override(pid, base=base) == {"voice": "bf_emma", "language": "German"}


def test_failed_barge_probes_back_off_up_to_ten_minutes():
    # Final review minor: where barge can never run, a probe process every 60 s for the
    # backend's whole life. Each further failure doubles the wait, capped.
    assert [st.barge_retry_interval(n) for n in (1, 2, 3, 4, 5, 6, 20)] == \
        [60.0, 120.0, 240.0, 480.0, 600.0, 600.0, 600.0]
