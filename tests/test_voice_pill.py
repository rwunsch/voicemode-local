"""Voice pill backend: snapshots from state files + conch files, and commands.

State goes to tmp_path only. The control socket is a fake in a short /tmp dir
(AF_UNIX paths are limited to ~104 bytes). Nothing here opens the mic or plays sound.
"""
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

import pytest

PATCHES = Path(__file__).resolve().parent.parent / "patches"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, PATCHES / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


st = _load("voice_pill_state")
vp = _load("voice_pill")


# -- fixtures ---------------------------------------------------------------

@pytest.fixture
def sock_dir():
    d = tempfile.mkdtemp(prefix="vp-")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def ctx(tmp_path, sock_dir, monkeypatch):
    # Settings are read from the environment first. Importing the installed voice_mode
    # elsewhere in the run loads the real voicemode.env into os.environ: keep it out.
    for key in ("VOICEMODE_VOICE_BARGE", "VOICEMODE_AUDIO_FEEDBACK", "VOICEMODE_PILL",
                "VOICEMODE_BARGE_CONTROL_COMMAND", "VOICEMODE_BASE_DIR",
                "VOICEMODE_PILL_LANGUAGES", "VOICEMODE_PTT_ENABLED"):
        monkeypatch.delenv(key, raising=False)
    # German follows the models installed: none found here, so it stays offered, as before.
    monkeypatch.setattr(vp, "_install_dirs", lambda: [])
    return vp.Context(base=tmp_path / "pill", home=tmp_path / "vm",
                      sock=sock_dir / "control.sock", switch=None)


class FakeControl:
    """A control socket that records command lines and answers `status`."""

    def __init__(self, path, status=None):
        self.path, self.status, self.received = path, status or {}, []
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(str(path))
        self._srv.listen(8)
        self._t = threading.Thread(target=self._serve, daemon=True)
        self._t.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            with conn:
                line = conn.makefile("rb").readline()
                msg = json.loads(line)
                if msg.get("command") == "status":
                    conn.sendall((json.dumps(self.status) + "\n").encode())
                else:
                    self.received.append(msg["command"])

    def close(self):
        self._srv.close()


@pytest.fixture
def control(ctx):
    c = FakeControl(ctx.sock)
    yield c
    c.close()


@pytest.fixture
def child():
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    yield p
    p.kill()
    p.wait()


def write_conch(ctx, pid, held=False, expires=None, project="/work/felix", voice="af_river"):
    ctx.home.mkdir(parents=True, exist_ok=True)
    (ctx.home / "conch").write_text(json.dumps({
        "pid": pid, "agent": "claude", "session_id": None, "project_path": project,
        "voice": voice, "acquired": "2026-11-05T10:00:00.000000", "held": held,
        "expires": expires}, indent=2))


def write_waiter(ctx, seq, session, pid, expires=None):
    d = ctx.home / "conch.queue.d"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{seq:06d}-{session}.json").write_text(json.dumps({
        "session_id": session, "seq": seq, "agent": "claude", "project_path": "/w",
        "voice": None, "pid": pid, "mode": "wait",
        "requested_at": "2026-11-05T10:00:00", "expires": expires}))


def snap(**kw):
    args = dict(status={}, sessions=[], conch={"holder": None, "waiters": []})
    args.update(kw)
    return vp.build_snapshot(**args)


def sess(pid, label="s", started_at=1.0, voice="af_heart"):
    return {"pid": pid, "label": label, "voice": voice, "started_at": started_at,
            "last_voice_at": None}


# -- snapshot rules ---------------------------------------------------------

@pytest.mark.parametrize("status,paused,sessions,phase", [
    ({"speaking": True, "listening": True}, True, [sess(1)], "paused"),
    ({"speaking": True, "listening": True}, False, [sess(1)], "speaking"),
    # push-to-talk key down (2026-11-07): the hold is what the user is doing right now
    ({"holding": True, "speaking": True, "listening": True}, False, [sess(1)], "holding"),
    ({"holding": True}, True, [sess(1)], "paused"),
    ({"listening": True}, False, [sess(1)], "listening"),
    ({}, False, [sess(1)], "working"),
    ({}, False, [], "idle"),
])
def test_phase_precedence(status, paused, sessions, phase):
    assert snap(status=status, paused=paused, sessions=sessions)["phase"] == phase


def test_visible_with_session_or_recent_ptt():
    assert snap()["visible"] is False
    assert snap(sessions=[sess(1)])["visible"] is True
    assert snap(ptt=True)["visible"] is True


def test_closed_hides_even_new_sessions_and_ptt():
    # (Robert, 2026-11-10) a user close sticks until voice restarts from scratch
    assert snap(sessions=[sess(1)], closed=True)["visible"] is False
    assert snap(sessions=[sess(1), sess(2)], ptt=True, closed=True)["visible"] is False
    assert snap(sessions=[sess(1)], closed=False)["visible"] is True


def test_dismiss_writes_the_closed_marker_and_a_new_session_does_not_lift_it(ctx, child):
    st.register_session(os.getpid(), "a", None, ctx.base)
    assert vp.gather(ctx)["visible"] is True
    vp.handle_command({"cmd": "dismiss"}, ctx)
    assert st.pill_closed(ctx.base)
    assert vp.gather(ctx)["visible"] is False
    st.register_session(child.pid, "b", None, ctx.base)
    assert vp.gather(ctx)["visible"] is False


def _run(ctx, monkeypatch, start_grace, quit_grace, keep_open_s=None):
    """Run run_stdio with an open stdin; return (exit value, lines written)."""
    import io
    monkeypatch.setattr(vp, "START_GRACE_S", start_grace)
    monkeypatch.setattr(vp, "QUIT_GRACE_S", quit_grace)
    monkeypatch.setattr(vp, "IDLE_INTERVAL_S", 0.05)
    r, w = os.pipe()
    out = io.StringIO()
    result = {}
    t = threading.Thread(target=lambda: result.update(v=vp.run_stdio(ctx, stdin_fd=r, out=out)),
                         daemon=True)
    t.start()
    t.join(keep_open_s if keep_open_s is not None else 5)
    alive = t.is_alive()
    os.close(w)
    t.join(5)
    os.close(r)
    return (None if alive else result.get("v")), [json.loads(x) for x in out.getvalue().splitlines()]


def test_backend_quits_once_no_session_is_left(ctx, monkeypatch):
    st.set_pill_closed(True, ctx.base)
    value, lines = _run(ctx, monkeypatch, start_grace=0.0, quit_grace=0.2)
    assert value == vp.EXIT_DONE
    assert lines[-1] == {"quit": True}
    assert not st.pill_closed(ctx.base)   # nobody left: voice restarts from scratch


def test_backend_stays_while_a_session_is_live(ctx, monkeypatch):
    st.register_session(os.getpid(), "a", None, ctx.base)
    value, lines = _run(ctx, monkeypatch, start_grace=0.0, quit_grace=0.2, keep_open_s=1.0)
    assert value is None and {"quit": True} not in lines


def test_backend_never_quits_inside_its_start_grace(ctx, monkeypatch):
    value, lines = _run(ctx, monkeypatch, start_grace=30.0, quit_grace=0.0, keep_open_s=1.0)
    assert value is None and {"quit": True} not in lines


def test_main_exits_with_exit_done_when_it_quits(ctx, monkeypatch):
    monkeypatch.setattr(vp, "Context", lambda: ctx)
    monkeypatch.setattr(vp, "run_stdio", lambda c: vp.EXIT_DONE)
    assert vp.main([]) == vp.EXIT_DONE


def test_floor_follows_conch_holder_and_sessions_get_states():
    s = snap(sessions=[sess(10, "a"), sess(11, "b"), sess(12, "c")],
             conch={"holder": {"pid": 10, "label": "x", "voice": None},
                    "waiters": [{"pid": 11, "label": "b", "seq": 3}]})
    assert s["floor"] == {"pid": 10, "label": "a"}
    assert {x["pid"]: (x["state"], x["position"]) for x in s["sessions"]} == {
        10: ("talking", None), 11: ("waiting", 1), 12: ("working", None)}


def test_floor_falls_back_to_last_holder():
    last = {"pid": 11, "label": "b"}
    s = snap(sessions=[sess(10, "a"), sess(11, "b")], last_floor=last)
    assert s["floor"] == {"pid": 11, "label": "b"}
    assert {x["pid"]: x["state"] for x in s["sessions"]} == {10: "working", 11: "working"}
    # a holder that is gone is not resurrected
    assert snap(sessions=[sess(10, "a")], last_floor=last)["floor"] is None


def test_voice_and_language_come_from_floor_override():
    s = snap(sessions=[sess(10, voice="af_heart")],
             conch={"holder": {"pid": 10, "label": "a", "voice": None}, "waiters": []},
             overrides={10: {"voice": "bm_george", "language": "German"}})
    assert (s["voice"], s["language"]) == ("bm_george", "German")
    s = snap(sessions=[sess(10, voice="af_heart")],
             conch={"holder": {"pid": 10, "label": "a", "voice": None}, "waiters": []})
    assert (s["voice"], s["language"]) == ("af_heart", "English")  # derived from the voice


def test_language_is_derived_from_the_voice_when_no_override():
    for voice, lang in (("dm_martin", "German"), ("p_de_thorsten", "German"), ("p_de_eva", "German"),
                        ("am_puck", "English"), ("bf_emma", "English"), ("weird", "English")):
        s = snap(sessions=[sess(10, voice=voice)],
                 conch={"holder": {"pid": 10, "label": "a", "voice": None}, "waiters": []})
        assert s["language"] == lang, voice


def test_override_language_beats_the_derived_one():
    s = snap(sessions=[sess(10, voice="af_heart")],
             conch={"holder": {"pid": 10, "label": "a", "voice": None}, "waiters": []},
             overrides={10: {"language": "German"}})
    assert s["language"] == "German"


def test_no_floor_shows_the_default_voice_and_english():
    s = snap(default_voice="am_puck")
    assert (s["floor"], s["voice"], s["language"]) == (None, "am_puck", "English")
    s = snap(default_voice="dm_martin")
    assert (s["voice"], s["language"]) == ("dm_martin", "German")


def test_floor_without_a_voice_falls_back_to_the_default_voice():
    s = snap(sessions=[sess(10, voice=None)], last_floor={"pid": 10, "label": "a"}, default_voice="am_puck")
    assert s["voice"] == "am_puck"


def test_default_voice_reads_first_of_voices_then_tts_voice_then_am_puck(tmp_path, monkeypatch):
    for k in ("VOICEMODE_VOICES", "VOICEMODE_TTS_VOICE"):
        monkeypatch.delenv(k, raising=False)
    assert st.default_voice(tmp_path) == "am_puck"
    (tmp_path / "voicemode.env").write_text("VOICEMODE_TTS_VOICE=bf_emma\n")
    assert st.default_voice(tmp_path) == "bf_emma"
    (tmp_path / "voicemode.env").write_text("VOICEMODE_TTS_VOICE=bf_emma\nVOICEMODE_VOICES=am_adam, af_sky\n")
    assert st.default_voice(tmp_path) == "am_adam"


def test_gather_without_sessions_shows_the_configured_default(ctx, monkeypatch):
    for k in ("VOICEMODE_VOICES", "VOICEMODE_TTS_VOICE"):
        monkeypatch.delenv(k, raising=False)
    ctx.home.mkdir(parents=True, exist_ok=True)
    (ctx.home / "voicemode.env").write_text("VOICEMODE_VOICES=bm_george,af_sky\n")
    s = vp.gather(ctx)
    assert (s["voice"], s["language"]) == ("bm_george", "English")


def test_snapshot_shape_and_json_serialisable():
    s = snap(sessions=[sess(1)], ui={"docked": True}, platform="mac",
             barge={"enabled": True, "available": False, "count": 2})
    assert json.loads(json.dumps(s)) == s
    assert set(s) == {"visible", "phase", "floor", "sessions", "voice", "language",
                      "barge", "beeps", "platform", "ui", "play", "wake", "german_available", "ptt"}
    assert s["play"] is None and s["wake"] is None
    assert s["platform"] == "mac" and s["ui"] == {"docked": True}
    assert s["barge"] == {"enabled": True, "available": False, "count": 2}


# -- conch files (formats copied from conch.py / conch_queue.py) -------------

def test_read_conch_empty(ctx):
    assert vp.read_conch(ctx.home) == {"holder": None, "waiters": []}


def test_read_conch_live_holder_and_ordered_waiters(ctx, child):
    write_conch(ctx, child.pid)
    write_waiter(ctx, 17, "bbb", os.getpid())
    write_waiter(ctx, 9, "aaa", child.pid)
    c = vp.read_conch(ctx.home)
    assert c["holder"]["pid"] == child.pid and c["holder"]["label"] == "felix"
    assert c["holder"]["voice"] == "af_river"
    assert [w["pid"] for w in c["waiters"]] == [child.pid, os.getpid()]


def test_read_conch_drops_dead_holder_and_dead_waiters(ctx):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    write_conch(ctx, dead.pid)
    write_waiter(ctx, 1, "x", dead.pid)
    write_waiter(ctx, 2, "remote", None, expires="2000-01-01T00:00:00+00:00")
    write_waiter(ctx, 3, "remote2", None, expires="2999-01-01T00:00:00+00:00")
    c = vp.read_conch(ctx.home)
    assert c["holder"] is None
    assert [w["seq"] for w in c["waiters"]] == [3]


def test_read_conch_expired_hold_is_not_a_holder(ctx, child):
    write_conch(ctx, child.pid, held=True, expires="2000-01-01T00:00:00")
    assert vp.read_conch(ctx.home)["holder"] is None
    write_conch(ctx, child.pid, held=True, expires="2999-01-01T00:00:00")
    assert vp.read_conch(ctx.home)["holder"]["pid"] == child.pid


def test_read_conch_corrupt_files_read_as_empty(ctx):
    ctx.home.mkdir(parents=True)
    (ctx.home / "conch").write_text("{not json")
    (ctx.home / "conch.queue.d").mkdir()
    (ctx.home / "conch.queue.d" / "000001-x.json").write_text("garbage")
    assert vp.read_conch(ctx.home) == {"holder": None, "waiters": []}


def test_gather_remembers_last_holder(ctx, child):
    st.register_session(child.pid, "kid", None, ctx.base)
    write_conch(ctx, child.pid)
    assert vp.gather(ctx)["floor"] == {"pid": child.pid, "label": "kid"}
    (ctx.home / "conch").unlink()
    assert vp.gather(ctx)["floor"] == {"pid": child.pid, "label": "kid"}


# -- commands ---------------------------------------------------------------

def two_sessions(ctx, child):
    st.register_session(os.getpid(), "me", None, ctx.base)
    st.register_session(child.pid, "floor", None, ctx.base)
    write_conch(ctx, child.pid)


def test_stop_only_targets_floor_session(ctx, child):
    two_sessions(ctx, child)
    vp.handle_command({"cmd": "stop"}, ctx)
    assert st.take_stop(child.pid, ctx.base) is True      # flag set for the floor
    assert st.take_stop(os.getpid(), ctx.base) is False   # the other one untouched
    stopped = {s["pid"]: s["stopped"] for s in st.live_sessions(ctx.base)}
    assert stopped == {child.pid: True, os.getpid(): None}  # stays listed, only the floor marked


def test_stop_with_live_socket_sets_no_stop_flag(ctx, child, control):
    two_sessions(ctx, child)
    vp.handle_command({"cmd": "stop"}, ctx)
    time.sleep(0.1)
    assert control.received == ["stop"]
    assert st.take_stop(child.pid, ctx.base) is False
    assert {s["pid"] for s in st.live_sessions(ctx.base)} == {child.pid, os.getpid()}


def test_stop_with_no_floor_does_nothing(ctx):
    st.register_session(os.getpid(), "me", None, ctx.base)
    vp.handle_command({"cmd": "stop"}, ctx)
    assert st.take_stop(os.getpid(), ctx.base) is False
    assert len(st.live_sessions(ctx.base)) == 1


def test_interrupt_with_no_socket_is_harmless(ctx):
    vp.handle_command({"cmd": "interrupt"}, ctx)  # no socket file, must not raise


def test_interrupt_sends_skip_forward(ctx, control):
    vp.handle_command({"cmd": "interrupt"}, ctx)
    time.sleep(0.1)
    assert control.received == ["skip_forward"]


def test_pause_records_this_pid_as_owner(ctx, control):
    vp.handle_command({"cmd": "pause"}, ctx)
    time.sleep(0.1)
    owner = json.loads((ctx.base / "paused.json").read_text())["owner"]
    assert owner == os.getpid() and st.is_paused(ctx.base)
    assert vp.gather(ctx)["phase"] == "paused"
    vp.handle_command({"cmd": "resume"}, ctx)
    time.sleep(0.1)
    assert not st.is_paused(ctx.base)
    assert control.received == ["pause", "resume"]


def test_pause_without_socket_still_pauses(ctx):
    vp.handle_command({"cmd": "pause"}, ctx)
    assert st.is_paused(ctx.base)


@pytest.mark.parametrize("msg", [
    {"cmd": "explode"}, {"cmd": None}, {}, {"cmd": "set_barge"}, {"cmd": "set_voice", "voice": 5},
    {"cmd": "set_language"}, {"cmd": "save_ui", "ui": "x"}, "not a dict", None, 7,
])
def test_unknown_command_is_ignored(ctx, msg):
    vp.handle_command(msg, ctx)
    assert not (ctx.base / "paused.json").exists()


def test_set_voice_and_language_apply_to_floor_only(ctx, child):
    two_sessions(ctx, child)
    vp.handle_command({"cmd": "set_voice", "voice": "bm_george"}, ctx)
    vp.handle_command({"cmd": "set_language", "language": "German"}, ctx)
    # German never keeps an English voice: the voice moves with the language
    assert st.get_override(child.pid, ctx.base) == {"voice": "dm_martin", "language": "German"}
    assert st.get_override(os.getpid(), ctx.base) == {}
    s = vp.gather(ctx)
    assert (s["voice"], s["language"]) == ("dm_martin", "German")
    vp.handle_command({"cmd": "set_language", "language": ""}, ctx)
    assert st.get_override(child.pid, ctx.base) == {"voice": "dm_martin"}


def test_set_language_keeps_a_voice_that_already_fits(ctx, child):
    two_sessions(ctx, child)
    vp.handle_command({"cmd": "set_voice", "voice": "p_de_eva"}, ctx)
    vp.handle_command({"cmd": "set_language", "language": "German"}, ctx)
    assert st.get_override(child.pid, ctx.base) == {"voice": "p_de_eva", "language": "German"}


def test_set_language_english_sets_the_configured_default_voice(ctx, child, monkeypatch):
    monkeypatch.delenv("VOICEMODE_VOICES", raising=False)
    monkeypatch.delenv("VOICEMODE_TTS_VOICE", raising=False)
    ctx.home.mkdir(parents=True, exist_ok=True)
    (ctx.home / "voicemode.env").write_text("VOICEMODE_VOICES=bm_george,af_sky\n")
    two_sessions(ctx, child)
    vp.handle_command({"cmd": "set_voice", "voice": "dm_martin"}, ctx)
    vp.handle_command({"cmd": "set_language", "language": "English"}, ctx)
    assert st.get_override(child.pid, ctx.base) == {"voice": "bm_george", "language": "English"}


def test_set_language_english_without_configuration_uses_am_puck(ctx, child, monkeypatch):
    monkeypatch.delenv("VOICEMODE_VOICES", raising=False)
    monkeypatch.delenv("VOICEMODE_TTS_VOICE", raising=False)
    two_sessions(ctx, child)
    vp.handle_command({"cmd": "set_voice", "voice": "p_de_thorsten"}, ctx)
    vp.handle_command({"cmd": "set_language", "language": "English"}, ctx)
    assert st.get_override(child.pid, ctx.base) == {"voice": "am_puck", "language": "English"}


def test_set_language_english_keeps_an_english_voice(ctx, child):
    two_sessions(ctx, child)
    vp.handle_command({"cmd": "set_voice", "voice": "bf_emma"}, ctx)
    vp.handle_command({"cmd": "set_language", "language": "English"}, ctx)
    assert st.get_override(child.pid, ctx.base) == {"voice": "bf_emma", "language": "English"}


def test_save_ui_round_trips_into_snapshot(ctx):
    vp.handle_command({"cmd": "save_ui", "ui": {"docked": True, "x": 3}}, ctx)
    assert vp.gather(ctx)["ui"] == {"docked": True, "x": 3}


# -- barge ------------------------------------------------------------------

def test_barge_available_follows_capture_backend(ctx, monkeypatch):
    st._barge_probe_reset()
    monkeypatch.setattr(st, "capture_backend", lambda: None)
    monkeypatch.setattr(st, "barge_interpreter", lambda: None)
    assert vp.gather(ctx)["barge"]["available"] is False
    monkeypatch.setattr(st, "capture_backend", lambda: "parec")
    assert vp.gather(ctx)["barge"]["available"] is True


def test_barge_enabled_and_count(ctx):
    ctx.home.mkdir(parents=True)
    (ctx.home / "voicemode.env").write_text(
        "VOICEMODE_VOICE_BARGE=false\nOTHER=1\nVOICEMODE_VOICE_BARGE=true\n")
    st.record_interruption(time.time(), ctx.base)
    st.record_interruption(time.time(), ctx.base)
    st.record_interruption(time.time() - 3 * 86400, ctx.base)
    b = vp.gather(ctx)["barge"]
    assert b["enabled"] is True and b["count"] == 2


def test_set_barge_runs_switch_off_the_caller_thread(ctx, tmp_path):
    marker, args = tmp_path / "done", tmp_path / "args"
    switch = tmp_path / "voicemode-switch"
    switch.write_text(f'#!/bin/sh\necho "$@" > "{args}"\nsleep 1\ntouch "{marker}"\n')
    switch.chmod(0o755)
    ctx.switch = switch
    t0 = time.monotonic()
    vp.handle_command({"cmd": "set_barge", "on": True}, ctx)
    assert time.monotonic() - t0 < 0.5 and not marker.exists()
    deadline = time.time() + 6
    while not marker.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert marker.exists() and args.read_text().strip() == "barge on"


def test_backend_pulls_in_nothing_heavy():
    code = ("import sys; sys.path.insert(0, %r); import voice_pill; "
            "bad = [m for m in ('numpy','voice_mode','onnxruntime','psutil') if m in sys.modules]; "
            "print(bad)" % str(PATCHES))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.stdout.strip() == "[]", out.stderr


# -- process-level ----------------------------------------------------------

def test_second_instance_exits(tmp_path):
    first = vp.acquire_lock(tmp_path / "pill")
    assert first is not None
    assert vp.acquire_lock(tmp_path / "pill") is None
    first.close()
    again = vp.acquire_lock(tmp_path / "pill")
    assert again is not None
    again.close()


def test_main_returns_1_when_lock_is_taken(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    held = vp.acquire_lock(st.default_base())
    assert vp.main(["--stdio"]) == 1
    held.close()


def test_stdio_loop_emits_snapshots_and_exits_on_eof(tmp_path):
    env = {**os.environ, "HOME": str(tmp_path)}
    env.pop("VOICEMODE_CONTROL_SOCKET", None)
    p = subprocess.Popen([sys.executable, str(PATCHES / "voice_pill.py"), "--stdio"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, env=env, text=True)
    try:
        first = json.loads(p.stdout.readline())
        assert first["phase"] == "idle" and first["visible"] is False
        t0 = time.monotonic()
        p.stdin.write(json.dumps({"cmd": "pause"}) + "\n")
        p.stdin.flush()
        # the snapshot after a command must not wait for the 1 s idle tick
        paused = None
        for _ in range(3):
            snap_ = json.loads(p.stdout.readline())
            if snap_["phase"] == "paused":
                paused = snap_
                break
        assert paused is not None and time.monotonic() - t0 < 0.9
        p.stdin.close()
        assert p.wait(timeout=5) == 0
    finally:
        if p.poll() is None:
            p.kill()
    # the pause it owned must not outlive it
    assert not (tmp_path / ".voicemode" / "pill" / "paused.json").exists()


# -- helpers moved into the stdlib state module -------------------------------

def test_state_socket_helpers(sock_dir, monkeypatch):
    p = sock_dir / "c.sock"
    assert st._status(p) == {}
    assert st.send_control(p, "pause") is False
    c = FakeControl(p, status={"speaking": True})
    try:
        assert st._status(p) == {"speaking": True}
        assert st.send_control(p, "stop") is True
        time.sleep(0.1)
        assert c.received == ["stop"]
    finally:
        c.close()
    monkeypatch.setenv("VOICEMODE_CONTROL_SOCKET", str(p))
    assert st.control_socket_path() == p


def test_barge_control_lives_in_state_and_rejects_bad_action():
    assert "must be on, off, auto or status" in st.barge_control("on; rm -rf /")


# -- exit while paused: the server-side pause must be released too -----------

def _paused_backend(tmp_path, sock_path):
    env = {**os.environ, "HOME": str(tmp_path), "VOICEMODE_CONTROL_SOCKET": str(sock_path)}
    p = subprocess.Popen([sys.executable, str(PATCHES / "voice_pill.py"), "--stdio"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, env=env, text=True)
    json.loads(p.stdout.readline())
    p.stdin.write(json.dumps({"cmd": "pause"}) + "\n")
    p.stdin.flush()
    for _ in range(3):
        if json.loads(p.stdout.readline())["phase"] == "paused":
            break
    return p


def _wait_received(control, n, timeout=3):
    end = time.time() + timeout
    while len(control.received) < n and time.time() < end:
        time.sleep(0.05)


def test_eof_while_paused_sends_resume_to_socket(tmp_path, sock_dir):
    c = FakeControl(sock_dir / "c.sock")
    try:
        p = _paused_backend(tmp_path, sock_dir / "c.sock")
        p.stdin.close()
        assert p.wait(timeout=5) == 0
        _wait_received(c, 2)
        assert c.received == ["pause", "resume"]
    finally:
        c.close()
        if p.poll() is None:
            p.kill()
    assert not (tmp_path / ".voicemode" / "pill" / "paused.json").exists()


def test_sigterm_while_paused_releases_like_eof(tmp_path, sock_dir):
    import signal
    c = FakeControl(sock_dir / "c.sock")
    try:
        p = _paused_backend(tmp_path, sock_dir / "c.sock")
        p.send_signal(signal.SIGTERM)
        assert p.wait(timeout=5) == 0
        _wait_received(c, 2)
        assert c.received == ["pause", "resume"]
    finally:
        c.close()
        if p.poll() is None:
            p.kill()
    assert not (tmp_path / ".voicemode" / "pill" / "paused.json").exists()


def test_exit_when_not_paused_sends_no_resume(tmp_path, sock_dir):
    c = FakeControl(sock_dir / "c.sock")
    env = {**os.environ, "HOME": str(tmp_path), "VOICEMODE_CONTROL_SOCKET": str(sock_dir / "c.sock")}
    try:
        p = subprocess.Popen([sys.executable, str(PATCHES / "voice_pill.py"), "--stdio"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env, text=True)
        p.stdout.readline()
        p.stdin.close()
        assert p.wait(timeout=5) == 0
        time.sleep(0.2)
        assert c.received == []
    finally:
        c.close()


# Stopping one session keeps the pill: it closes when the last server exits, or on the close button
def test_stopped_session_keeps_the_pill_visible_and_shows_stopped(ctx, child):
    st.register_session(child.pid, "floor", None, ctx.base)
    write_conch(ctx, child.pid)
    vp.gather(ctx)  # remembers the floor
    vp.handle_command({"cmd": "stop"}, ctx)
    (ctx.home / ".voicemode" / "conch").unlink(missing_ok=True)
    s = vp.build_snapshot({}, st.live_sessions(ctx.base), {}, last_floor={"pid": child.pid, "label": "floor"})
    assert s["visible"] is True and s["phase"] == "stopped"
    assert s["floor"]["pid"] == child.pid
    assert [r["state"] for r in s["sessions"]] == ["stopped"]


def test_a_live_turn_on_another_session_is_not_shown_as_stopped():
    sessions = [{"pid": 1, "label": "a", "stopped": True}, {"pid": 2, "label": "b"}]
    s = vp.build_snapshot({"speaking": True}, sessions, {"holder": {"pid": 2, "label": "b"}, "waiters": []})
    assert s["phase"] == "speaking"
    assert [r["state"] for r in s["sessions"]] == ["stopped", "talking"]


def test_set_beeps_is_saved_and_reported(ctx):
    vp.handle_command({"cmd": "set_beeps", "on": False}, ctx)
    assert vp.gather(ctx)["beeps"] is False
    vp.handle_command({"cmd": "set_beeps", "on": True}, ctx)
    assert vp.gather(ctx)["beeps"] is True


def test_set_beeps_ignores_a_non_boolean(ctx):
    vp.handle_command({"cmd": "set_beeps", "on": "off"}, ctx)
    assert not (ctx.base / "beeps.json").exists()


@pytest.mark.parametrize("env,expected", [
    ("", True), ("VOICEMODE_AUDIO_FEEDBACK=false\n", False), ("VOICEMODE_AUDIO_FEEDBACK=true\n", True),
    ("# VOICEMODE_AUDIO_FEEDBACK=false\n", True), ("VOICEMODE_AUDIO_FEEDBACK=false\nVOICEMODE_AUDIO_FEEDBACK=on\n", True),
])
def test_beeps_follow_the_env_until_chosen(ctx, env, expected):
    ctx.home.mkdir(parents=True, exist_ok=True)
    (ctx.home / "voicemode.env").write_text(env)
    assert vp.gather(ctx)["beeps"] is expected


def test_a_failed_barge_toggle_reaches_the_log(ctx, tmp_path, capfd):
    switch = tmp_path / "voicemode-switch"
    switch.write_text('#!/bin/sh\necho "no model" >&2\nexit 3\n')
    switch.chmod(0o755)
    ctx.switch = switch
    vp.handle_command({"cmd": "set_barge", "on": True}, ctx)
    ctx.barge_thread.join(5)
    out, err = capfd.readouterr()
    assert "no model" in err and "no model" not in out  # stdout is the protocol


def test_a_held_key_polls_at_the_turn_rate(monkeypatch):
    import io
    monkeypatch.setattr(vp, "gather", lambda ctx: {"phase": "holding"})
    assert vp._emit(None, io.StringIO()) == vp.TURN_INTERVAL_S


# -- Play: wake the last voice session (session_wake) --------------------------

class FakeWake:
    """Stands in for session_wake: never touches a real Claude Code session."""

    def __init__(self, target=("demo", 4242), result=(True, "sent"), on=True):
        self.target, self.result, self.on = target, result, on
        self.calls, self.status = [], "idle"

    def enabled(self, home=None):
        return self.on

    def last_voice_session(self, **kw):
        if self.target is None:
            return None
        label, pid = self.target
        return types.SimpleNamespace(voice_pid=1, claude_pid=pid, label=label)

    def wake(self, target_claude_pid=None, reason="ptt"):
        self.calls.append((target_claude_pid, reason))
        return self.result

    def registry_status(self, pid, cdir=None):
        return self.status


@pytest.fixture
def fake_wake(monkeypatch):
    fw = FakeWake()
    monkeypatch.setattr(vp, "sw", fw)
    return fw


def test_snapshot_carries_play_and_wake():
    s = snap(play={"label": "demo"}, wake={"text": "Woke demo", "ok": True})
    assert s["play"] == {"label": "demo"} and s["wake"] == {"text": "Woke demo", "ok": True}


def test_gather_offers_play_for_the_last_voice_session(ctx, fake_wake):
    assert vp.gather(ctx)["play"] == {"label": "demo"}


def test_gather_offers_no_play_without_a_session(ctx, fake_wake):
    fake_wake.target = None
    assert vp.gather(ctx)["play"] is None


def test_gather_offers_no_play_when_the_setting_is_off(ctx, fake_wake):
    fake_wake.on = False
    assert vp.gather(ctx)["play"] is None


def test_wake_wakes_the_last_voice_session_with_reason_wake(ctx, fake_wake):
    vp.handle_command({"cmd": "wake"}, ctx)
    assert fake_wake.calls == [(4242, "wake")]
    assert vp.gather(ctx)["wake"] == {"text": 'Waking demo\u2026 listen for "Listening."', "ok": True}


def test_old_play_command_still_wakes(ctx, fake_wake):
    vp.handle_command({"cmd": "play"}, ctx)
    assert fake_wake.calls == [(4242, "wake")]


def test_play_refused_says_why(ctx, fake_wake):
    fake_wake.result = (False, "busy")
    vp.handle_command({"cmd": "play"}, ctx)
    assert vp.gather(ctx)["wake"] == {"text": "Can't wake demo: busy", "ok": False}


def test_play_without_a_session(ctx, fake_wake):
    fake_wake.target = None
    vp.handle_command({"cmd": "play"}, ctx)
    assert fake_wake.calls == []
    assert vp.gather(ctx)["wake"]["text"] == "No voice session to wake"


def test_no_answer_when_the_session_stays_idle_for_5_s(ctx, fake_wake):
    vp.handle_command({"cmd": "wake"}, ctx)
    t0 = ctx.wake["at"]
    assert vp.wake_view(ctx, t0 + 4.9) == {"text": 'Waking demo\u2026 listen for "Listening."', "ok": True}
    assert vp.wake_view(ctx, t0 + 5.1) == {"text": "No answer from demo", "ok": False}
    assert vp.wake_view(ctx, t0 + 30) is None


def test_busy_within_5_s_is_an_answer(ctx, fake_wake):
    vp.handle_command({"cmd": "wake"}, ctx)
    t0 = ctx.wake["at"]
    assert vp.wake_view(ctx, t0 + 1)["text"].startswith("Waking demo")
    fake_wake.status = "busy"
    assert vp.wake_view(ctx, t0 + 2)["text"] == "Woke demo"
    fake_wake.status = "idle"  # the turn may end again; it did answer
    assert vp.wake_view(ctx, t0 + 6)["text"] == "Woke demo"
    assert vp.wake_view(ctx, t0 + 30) is None


def test_play_without_session_wake_installed_is_harmless(ctx, monkeypatch):
    monkeypatch.setattr(vp, "sw", None)
    vp.handle_command({"cmd": "play"}, ctx)
    s = vp.gather(ctx)
    assert s["play"] is None


# Talos review, finding 6: an "off" sent while an earlier "on" still ran was dropped.
def test_set_barge_off_during_a_running_on_is_applied_after_it(ctx, tmp_path):
    log = tmp_path / "calls"
    switch = tmp_path / "voicemode-switch"
    switch.write_text(f'#!/bin/sh\nsleep 1\necho "$@" >> "{log}"\n')
    switch.chmod(0o755)
    ctx.switch = switch
    vp.handle_command({"cmd": "set_barge", "on": True}, ctx)
    time.sleep(0.2)
    vp.handle_command({"cmd": "set_barge", "on": False}, ctx)
    ctx.barge_thread.join(10)
    assert not ctx.barge_thread.is_alive()
    assert log.read_text().split("\n")[:-1] == ["barge on", "barge off"]


def test_set_barge_runs_only_the_last_wish_after_the_running_one(ctx, tmp_path):
    log = tmp_path / "calls"
    switch = tmp_path / "voicemode-switch"
    switch.write_text(f'#!/bin/sh\nsleep 1\necho "$@" >> "{log}"\n')
    switch.chmod(0o755)
    ctx.switch = switch
    vp.handle_command({"cmd": "set_barge", "on": True}, ctx)
    time.sleep(0.2)
    vp.handle_command({"cmd": "set_barge", "on": False}, ctx)
    vp.handle_command({"cmd": "set_barge", "on": True}, ctx)  # back to on: nothing more to do
    ctx.barge_thread.join(10)
    assert log.read_text().split("\n")[:-1] == ["barge on"]


# Talos review, finding 7: German was offered where only English Kokoro is installed (FELIX:
# the native server answers 400 "dm_martin needs the German model"), breaking TTS.

@pytest.fixture
def no_german_hints(ctx, monkeypatch):
    for key in ("VOICEMODE_PILL_LANGUAGES", "VOICEMODE_VOICES", "VOICEMODE_TTS_VOICE",
                "VOICEMODE_TTS_BASE_URLS"):
        monkeypatch.delenv(key, raising=False)
    return ctx


def _models(root, german):
    (root / "models" / "kokoro").mkdir(parents=True)
    (root / "models" / "kokoro" / "kokoro-v1.0.onnx").write_text("")
    if german:
        de = root / "models" / "kokoro-de-martin"
        de.mkdir()
        (de / "kokoro-martin.onnx").write_text("")
        (de / "voices-martin.npz").write_text("")


def test_german_follows_the_pill_languages_setting(no_german_hints, monkeypatch):
    ctx = no_german_hints
    monkeypatch.setenv("VOICEMODE_PILL_LANGUAGES", "English")
    assert vp.gather(ctx)["german_available"] is False
    monkeypatch.setenv("VOICEMODE_PILL_LANGUAGES", "English, German")
    assert vp.gather(ctx)["german_available"] is True


def test_german_is_unavailable_where_only_english_kokoro_is_installed(no_german_hints, monkeypatch, tmp_path):
    _models(tmp_path / "install", german=False)
    monkeypatch.setattr(vp, "_install_dirs", lambda: [tmp_path / "install"])
    assert vp.gather(no_german_hints)["german_available"] is False


def test_german_is_available_with_the_german_kokoro_model(no_german_hints, monkeypatch, tmp_path):
    _models(tmp_path / "install", german=True)
    monkeypatch.setattr(vp, "_install_dirs", lambda: [tmp_path / "install"])
    assert vp.gather(no_german_hints)["german_available"] is True


def test_german_is_available_with_piper_or_a_configured_german_voice(no_german_hints, monkeypatch, tmp_path):
    _models(tmp_path / "install", german=False)
    monkeypatch.setattr(vp, "_install_dirs", lambda: [tmp_path / "install"])
    monkeypatch.setenv("VOICEMODE_TTS_BASE_URLS", "http://127.0.0.1:8880/v1,http://127.0.0.1:8881/v1")
    assert vp.gather(no_german_hints)["german_available"] is True
    monkeypatch.delenv("VOICEMODE_TTS_BASE_URLS")
    monkeypatch.setenv("VOICEMODE_VOICES", "af_sky,dm_martin")
    assert vp.gather(no_german_hints)["german_available"] is True


def test_german_stays_offered_when_nothing_can_be_found(no_german_hints, monkeypatch):
    monkeypatch.setattr(vp, "_install_dirs", lambda: [])
    assert vp.gather(no_german_hints)["german_available"] is True  # unsure: as before


def test_set_language_german_is_refused_when_german_is_unavailable(no_german_hints, child, monkeypatch):
    ctx = no_german_hints
    monkeypatch.setenv("VOICEMODE_PILL_LANGUAGES", "English")
    two_sessions(ctx, child)
    vp.handle_command({"cmd": "set_voice", "voice": "bf_emma"}, ctx)
    vp.handle_command({"cmd": "set_language", "language": "German"}, ctx)
    assert st.get_override(child.pid, ctx.base) == {"voice": "bf_emma"}


# Talos review, finding 8: push-to-talk keys were shown where no listener runs.
def test_ptt_follows_the_setting_else_the_platform(ctx, monkeypatch):
    monkeypatch.delenv("VOICEMODE_PTT_ENABLED", raising=False)
    for platform, want in (("windows", True), ("linux", True), ("mac", False)):
        monkeypatch.setattr(vp, "detect_platform", lambda p=platform: p)
        assert vp.gather(ctx)["ptt"] is want, platform
    monkeypatch.setattr(vp, "detect_platform", lambda: "windows")
    monkeypatch.setenv("VOICEMODE_PTT_ENABLED", "false")
    assert vp.gather(ctx)["ptt"] is False
    monkeypatch.setattr(vp, "detect_platform", lambda: "mac")
    monkeypatch.setenv("VOICEMODE_PTT_ENABLED", "true")
    assert vp.gather(ctx)["ptt"] is True
