"""Diagnostics and the watchdog for a pill that went quiet (macOS incident, 2026-11-05).

Linux only: no GUI, no sound. The Mac wrapper runs with a fake backend; pywebview is not imported.
"""
import importlib.util
import os
import sys
import threading
import time
import types
from pathlib import Path

import pytest

PATCHES = Path(__file__).resolve().parent.parent / "patches"
FAKE = Path(__file__).resolve().parent / "pill_fake_backend.py"


def _load(name, filename=None):
    spec = importlib.util.spec_from_file_location(name, PATCHES / (filename or f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


st = _load("voice_pill_state")
vp = _load("voice_pill")
m = _load("voice_pill_mac_diag", "voice_pill_mac.py")


# -- 4. start token does not depend on the caller's zone or locale ------------

def test_mac_start_token_is_asked_in_utc_and_c_locale(monkeypatch):
    calls = []

    def run(cmd, **kw):
        calls.append((cmd, kw))
        return types.SimpleNamespace(stdout="Fri Nov  6 23:16:26 2026\n")

    monkeypatch.setattr(st.sys, "platform", "darwin")
    monkeypatch.setattr(st.subprocess, "run", run)
    monkeypatch.setenv("TZ", "Europe/Berlin")
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    assert st.process_start(os.getpid()) == "Fri Nov  6 23:16:26 2026"
    env = calls[0][1]["env"]
    assert env["TZ"] == "UTC" and env["LC_ALL"] == "C"
    assert env.get("PATH") == os.environ.get("PATH")  # the rest of the environment is kept


# -- 2. every prune is logged with its reason ----------------------------------

@pytest.fixture
def pruned(monkeypatch):
    seen = []
    monkeypatch.setattr(st, "prune_log", seen.append)
    return seen


def test_prune_of_a_changed_start_token_is_logged_with_both_tokens(tmp_path, monkeypatch, pruned):
    base = tmp_path / "pill"
    st.register_session(os.getpid(), "demo", None, base=base)
    stored = st.process_start(os.getpid())
    monkeypatch.setattr(st, "process_start", lambda pid: "OTHER")
    assert st.live_sessions(base=base) == []
    assert len(pruned) == 1
    msg = pruned[0]
    assert str(os.getpid()) in msg and "start token differs" in msg
    assert repr(stored) in msg and "'OTHER'" in msg


def test_prune_of_a_gone_process_is_logged(tmp_path, pruned):
    import subprocess
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    base = tmp_path / "pill"
    st.register_session(os.getpid(), "demo", None, base=base)
    f = next((base / "sessions").glob("*.json"))
    import json
    s = json.loads(f.read_text())
    s["pid"] = p.pid
    f.write_text(json.dumps(s))
    assert st.live_sessions(base=base) == []
    assert len(pruned) == 1 and "process gone" in pruned[0] and str(p.pid) in pruned[0]


def test_prune_of_a_record_without_a_start_token_is_logged(tmp_path, monkeypatch, pruned):
    monkeypatch.setattr(st, "process_start", lambda pid: None)  # ps failed at registration
    base = tmp_path / "pill"
    st.register_session(os.getpid(), "demo", None, base=base)
    monkeypatch.undo()
    monkeypatch.setattr(st, "prune_log", pruned.append)
    assert st.live_sessions(base=base) == []
    assert len(pruned) == 1 and "no start token" in pruned[0]


def test_kept_sessions_log_nothing(tmp_path, pruned):
    base = tmp_path / "pill"
    st.register_session(os.getpid(), "demo", None, base=base)
    assert len(st.live_sessions(base=base)) == 1
    assert pruned == []


# -- 2. the backend says where it is looking when it starts ------------------

def test_backend_start_logs_base_home_socket_tz_pid(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("VOICEMODE_BASE_DIR", str(tmp_path / "b"))
    monkeypatch.setenv("VOICEMODE_CONTROL_SOCKET", str(tmp_path / "c.sock"))
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    monkeypatch.setattr(vp, "run_stdio", lambda ctx: None)
    monkeypatch.setattr(st, "prune_log", None)
    assert vp.main([]) == 0
    err = capsys.readouterr().err
    assert "voice_pill: started" in err
    for want in (f"pid={os.getpid()}", "base=" + str(tmp_path / "b"), "sock=" + str(tmp_path / "c.sock"),
                 "home=", "TZ=Asia/Tokyo"):
        assert want in err, want
    assert callable(st.prune_log)  # prunes are reported from now on


def test_backend_prune_log_writes_to_stderr(capsys):
    vp._log_prune("session 7 pruned: process gone")
    assert "voice_pill: session 7 pruned: process gone" in capsys.readouterr().err


# -- 1. wrapper heartbeat and state-change lines --------------------------------

def test_heartbeat_line_has_every_field():
    line = m.heartbeat_line(last_line_age=3.2, visible=True, dismissed=False, mode="pill",
                            win_visible=True, screens=2)
    for want in ("hb", "last_line_age=3.2", "visible=True", "dismissed=False", "mode=pill",
                 "win_visible=True", "screens=2"):
        assert want in line, want


def test_heartbeat_age_none_before_any_line():
    assert "last_line_age=none" in m.heartbeat_line(None, False, False, "pill", None, None)


def test_state_label_names_the_five_states():
    S = m.WindowState
    assert m.state_label(S(visible=False)) == "hidden"
    assert m.state_label(S(visible=True)) == "shown"
    assert m.state_label(S(visible=True, dismissed=True)) == "dismissed"
    assert m.state_label(S(visible=True, mode="dot")) == "minimized to menu bar"


def test_state_changes_are_logged_once_each(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    app = m.App(True, ["/nonexistent"])
    app.note_state()                      # starting state: nothing to report
    app.state.visible = True
    app.note_state(); app.note_state()    # same state twice: one line
    app.state.mode = "dot"
    app.note_state()
    app.state.mode = "pill"
    app.note_state()
    app.state.dismissed = True
    app.note_state()
    app.state.visible = False
    app.state.dismissed = False
    app.note_state()
    err = capsys.readouterr().err
    lines = [l for l in err.splitlines() if "window:" in l]
    assert [l.split("window: ", 1)[1].split(" ")[0] for l in lines] == [
        "shown", "minimized", "restored", "dismissed", "hidden"]


def test_heartbeat_thread_logs_on_its_interval(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    app = m.App(True, ["/nonexistent"])
    monkeypatch.setattr(m, "HEARTBEAT_S", 0.05)
    app.start_heartbeat()
    time.sleep(0.4)
    app.closing = True
    app.hb_thread.join(2)
    assert not app.hb_thread.is_alive()
    assert capsys.readouterr().err.count(" hb ") >= 2


# -- 3. watchdog: a backend that stays alive and silent is killed and restarted --

def test_silent_backend_is_killed_and_started_again(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    d = tmp_path / "fake"
    d.mkdir()
    (d / "go_silent").write_text("")
    monkeypatch.setattr(m, "BACKEND_SILENCE_S", 0.8)
    monkeypatch.setattr(m, "WATCHDOG_TICK_S", 0.1)
    app = m.App(True, [sys.executable, str(FAKE), str(d)])
    try:
        app.start_backend()
        runs = d / "runs.log"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and (not runs.exists() or len(runs.read_text().splitlines()) < 2):
            time.sleep(0.1)
        assert len(runs.read_text().splitlines()) >= 2, "no second backend was started"
    finally:
        app.closing = True
        if app.proc:
            app.proc.kill()
        app.thread.join(5)
    err = capsys.readouterr().err
    assert "backend silent" in err
    assert "backend exited" in err and "-> retry" in err  # the ordinary restart path, with its backoff


def test_watchdog_limit_is_at_least_three_times_the_backend_cadence():
    assert m.BACKEND_SILENCE_S >= 3 * max(vp.IDLE_INTERVAL_S, vp.TURN_INTERVAL_S)
    assert m.BACKEND_SILENCE_S == 15


def test_talkative_backend_is_left_alone(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    d = tmp_path / "fake"
    d.mkdir()
    monkeypatch.setattr(m, "BACKEND_SILENCE_S", 2.5)
    monkeypatch.setattr(m, "WATCHDOG_TICK_S", 0.1)
    app = m.App(True, [sys.executable, str(FAKE), str(d)])
    try:
        app.start_backend()
        time.sleep(5)
    finally:
        app.closing = True
        if app.proc:
            app.proc.kill()
        app.thread.join(5)
    assert "backend silent" not in capsys.readouterr().err
    assert len((d / "runs.log").read_text().splitlines()) == 1
