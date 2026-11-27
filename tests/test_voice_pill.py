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
    # The voice list asks local TTS servers: never this machine's real ones from a test.
    monkeypatch.setattr(vp, "_fetch_voices", lambda url: None, raising=False)
    monkeypatch.setattr(vp, "_VOICES_CACHE", {}, raising=False)
    # gather() refreshes that list in a thread; not here unless a test asks (the swr fixture).
    monkeypatch.setattr(vp, "BACKGROUND_REFRESH", False, raising=False)
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
                      "barge", "beeps", "platform", "ui", "play", "wake", "german_available", "german_voices", "ptt",
                      "voices_by_language", "still_listening"}
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


def test_mac_keys_follow_a_live_listener(ctx, monkeypatch, tmp_path):
    # voicemode-mcp starts the Mac listener and records it in run/ptt-listener.pid; FELIX
    # starts none. Show the keys exactly while one runs.
    monkeypatch.delenv("VOICEMODE_PTT_ENABLED", raising=False)
    home = tmp_path / "vm"
    (home / "run").mkdir(parents=True)
    monkeypatch.setattr(vp.st, "upstream_home", lambda: home)
    assert vp.ptt_listening("mac") is False                       # no pid file
    (home / "run" / "ptt-listener.pid").write_text(str(os.getpid()))
    assert vp.ptt_listening("mac") is True                        # a live listener
    (home / "run" / "ptt-listener.pid").write_text("999999999")
    assert vp.ptt_listening("mac") is False                       # a dead one
    (home / "run" / "ptt-listener.pid").write_text(str(os.getpid()))
    monkeypatch.setenv("VOICEMODE_PTT_ENABLED", "false")
    assert vp.ptt_listening("mac") is False                       # switched off wins


def test_the_page_goes_disconnected_on_a_dropped_command():
    # app.js must route model.incomingKind's "dropped" to "no snapshot seen", not a snapshot.
    from pathlib import Path as _P
    app = (_P(__file__).resolve().parent.parent / "ui" / "pill" / "app.js").read_text()
    body = app.split("window.pillReceive = function", 1)[1].split("snapshot = s;", 1)[0]
    assert "M.incomingKind(s)" in body
    assert 'kind === "dropped"' in body and "lastSeen = null" in body


# 2026-11-16 (business-assistant, WSL): the pill listed dm_martin with Piper in the chain but
# no German Kokoro model, so choosing it failed on every server. Only voices that can speak.

def test_dm_martin_is_not_offered_without_the_german_kokoro_model(no_german_hints, monkeypatch, tmp_path):
    _models(tmp_path / "install", german=False)
    monkeypatch.setattr(vp, "_install_dirs", lambda: [tmp_path / "install"])
    monkeypatch.setenv("VOICEMODE_TTS_BASE_URLS", "http://127.0.0.1:8880/v1,http://127.0.0.1:8881/v1")
    assert vp.gather(no_german_hints)["german_voices"] == ["p_de_thorsten", "p_de_eva"]


def test_dm_martin_is_offered_with_the_german_kokoro_model(no_german_hints, monkeypatch, tmp_path):
    _models(tmp_path / "install", german=True)
    monkeypatch.setattr(vp, "_install_dirs", lambda: [tmp_path / "install"])
    assert vp.gather(no_german_hints)["german_voices"] == ["dm_martin"]


def test_german_voices_stay_all_offered_when_nothing_can_be_found(no_german_hints, monkeypatch):
    monkeypatch.setattr(vp, "_install_dirs", lambda: [])
    assert vp.gather(no_german_hints)["german_voices"] == ["dm_martin", "p_de_thorsten", "p_de_eva"]


# 2026-11-16 (Robert): the pill offered only English and German, from lists in model.js,
# while this machine has 76 local voices in 14 languages. voices_by_language asks each local
# TTS server for its voices (Piper proxy / kokoro-onnx shape {"data": [{id, language_name}]},
# Kokoro-FastAPI shape {"voices": [ids]}) and groups them by language.

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@pytest.fixture
def voices_env(tmp_path, monkeypatch):
    for key in ("VOICEMODE_TTS_BASE_URLS", "VOICEMODE_VOICES", "VOICEMODE_TTS_VOICE",
                "VOICEMODE_PILL_LANGUAGES"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(vp, "_VOICES_CACHE", {})
    monkeypatch.setattr(vp, "_install_dirs", lambda: [])
    return tmp_path / "vm"


@pytest.fixture
def voice_servers():
    """Start fake TTS servers answering GET /v1/audio/voices with a given body; all are
    shut down and closed at the end."""
    started = []

    def start(body):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path != "/v1/audio/voices":
                    self.send_response(404)
                    self.end_headers()
                    return
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        started.append(httpd)
        return httpd.server_address[1]

    yield start
    for httpd in started:
        httpd.shutdown()
        httpd.server_close()


def _dead_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_voices_by_language_groups_both_shapes_and_skips_a_dead_server(voices_env, voice_servers, monkeypatch):
    kokoro = voice_servers({"voices": ["af_sky", "bm_george", "ef_dora", "xx_odd", "im_nicola"]})
    piper = voice_servers({"object": "list", "data": [
        {"id": "p_de_thorsten", "language": "de", "language_name": "German"},
        {"id": "p_nl_nathalie", "language": "nl", "language_name": "Dutch"},
        {"id": "af_sky", "language": "en", "language_name": "English"}]})
    monkeypatch.setenv("VOICEMODE_TTS_BASE_URLS", f"http://127.0.0.1:{kokoro}/v1,"
                       f"http://127.0.0.1:{_dead_port()}/v1,http://localhost:{piper}/v1")
    monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{_dead_port()}")   # never used
    monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{_dead_port()}")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    got = vp.voices_by_language(voices_env)
    assert got == {"English": ["af_sky", "bm_george"], "Spanish": ["ef_dora"],
                   "Italian": ["im_nicola"], "German": ["p_de_thorsten"], "Dutch": ["p_nl_nathalie"]}
    assert list(got) == ["English", "Spanish", "Italian", "German", "Dutch"]   # first server first


def test_voices_by_language_asks_only_local_servers(voices_env, monkeypatch):
    asked = []
    monkeypatch.setattr(vp, "_fetch_voices", lambda url: asked.append(url) or {"voices": ["af_sky"]})
    monkeypatch.setenv("VOICEMODE_TTS_BASE_URLS", "https://api.openai.com/v1, http://127.0.0.1:8880/v1,"
                       "http://localhost:8881/v1/,http://10.0.0.5:8880/v1")
    assert vp.voices_by_language(voices_env) == {"English": ["af_sky"]}
    assert asked == ["http://127.0.0.1:8880/v1/audio/voices", "http://localhost:8881/v1/audio/voices"]


def test_voices_by_language_never_raises_on_odd_answers(voices_env, monkeypatch):
    answers = iter([["af_sky"], {"data": [1, {"id": 5}, {"id": "p_xx_who"}, {"language_name": "French"},
                                          {"id": "ff_siwis"}]}, {"voices": "af_sky"}, None])
    monkeypatch.setattr(vp, "_fetch_voices", lambda url: next(answers))
    monkeypatch.setenv("VOICEMODE_TTS_BASE_URLS", ",".join(f"http://127.0.0.1:{p}/v1" for p in (1, 2, 3, 4)))
    assert vp.voices_by_language(voices_env) == {"French": ["ff_siwis"]}

    def boom(url):
        raise RuntimeError("anything")
    monkeypatch.setattr(vp, "_VOICES_CACHE", {})
    monkeypatch.setattr(vp, "_fetch_voices", boom)
    assert vp.voices_by_language(voices_env) == {}


def test_voices_by_language_is_cached_for_a_minute(voices_env, monkeypatch):
    asked = []
    monkeypatch.setattr(vp, "_fetch_voices", lambda url: asked.append(url) or {"voices": ["af_sky"]})
    clock = [1000.0]
    monkeypatch.setattr(vp, "_clock", lambda: clock[0])
    monkeypatch.setenv("VOICEMODE_TTS_BASE_URLS", "http://127.0.0.1:8880/v1")
    assert vp.voices_by_language(voices_env) == {"English": ["af_sky"]}
    clock[0] += 59
    assert vp.voices_by_language(voices_env) == {"English": ["af_sky"]}
    assert len(asked) == 1                                     # within 60 s: not asked again
    clock[0] += 2
    vp.voices_by_language(voices_env)
    assert len(asked) == 2                                     # after 60 s: asked again
    monkeypatch.setenv("VOICEMODE_TTS_BASE_URLS", "http://127.0.0.1:8881/v1")
    vp.voices_by_language(voices_env)
    assert asked[-1] == "http://127.0.0.1:8881/v1/audio/voices"   # a new setting: asked at once


def test_voices_by_language_drops_german_voices_that_cannot_speak(voices_env, monkeypatch, tmp_path):
    _models(tmp_path / "install", german=False)          # English Kokoro only: no dm_martin
    monkeypatch.setattr(vp, "_install_dirs", lambda: [tmp_path / "install"])
    monkeypatch.setattr(vp, "_fetch_voices", lambda url: {"data": [
        {"id": "dm_martin", "language_name": "German"},
        {"id": "p_de_thorsten", "language_name": "German"},
        {"id": "p_de_kerstin", "language_name": "German"},
        {"id": "af_sky", "language_name": "English"}]})
    monkeypatch.setenv("VOICEMODE_TTS_BASE_URLS", "http://127.0.0.1:8880/v1,http://127.0.0.1:8881/v1")
    got = vp.voices_by_language(voices_env)
    # dm_martin needs the German model; a German voice german_voices() does not know is
    # one its server says it speaks, so it stays.
    assert got == {"German": ["p_de_thorsten", "p_de_kerstin"], "English": ["af_sky"]}


def test_snapshot_carries_voices_by_language(ctx, monkeypatch):
    assert snap()["voices_by_language"] == {}
    assert vp.gather(ctx)["voices_by_language"] == {}    # nothing answered
    monkeypatch.setattr(vp, "_fetch_voices", lambda url: {"voices": ["af_sky", "ff_siwis"]})
    monkeypatch.setenv("VOICEMODE_TTS_BASE_URLS", "http://127.0.0.1:8880/v1")
    vp.voices_by_language(ctx.home)       # the refresh the snapshot path leaves to its thread
    assert vp.gather(ctx)["voices_by_language"] == {"English": ["af_sky"], "French": ["ff_siwis"]}


@pytest.mark.parametrize("setting,kept", [
    ("English", ["English"]),
    ("english", ["English"]),
    (" English , french ", ["English", "French"]),
    ("Spanish", ["Spanish"]),
    ("English,Klingon", ["English"]),
])
def test_voices_by_language_follows_voicemode_pill_languages(voices_env, monkeypatch, setting, kept):
    """Talos #1840: FELIX sets VOICEMODE_PILL_LANGUAGES=English, yet every local language
    was offered. When the setting is set only the listed languages remain."""
    monkeypatch.setattr(vp, "_fetch_voices", lambda url: {"voices": ["af_sky", "ef_dora", "ff_siwis"]})
    monkeypatch.setenv("VOICEMODE_TTS_BASE_URLS", "http://127.0.0.1:8880/v1")
    monkeypatch.setenv("VOICEMODE_PILL_LANGUAGES", setting)
    assert list(vp.voices_by_language(voices_env)) == [
        l for l in ("English", "Spanish", "French") if l in kept]


def test_voices_by_language_unset_offers_every_language(voices_env, monkeypatch):
    monkeypatch.setattr(vp, "_fetch_voices", lambda url: {"voices": ["af_sky", "ef_dora", "ff_siwis"]})
    monkeypatch.setenv("VOICEMODE_TTS_BASE_URLS", "http://127.0.0.1:8880/v1")
    assert list(vp.voices_by_language(voices_env)) == ["English", "Spanish", "French"]
    monkeypatch.setenv("VOICEMODE_PILL_LANGUAGES", "  ")   # blank counts as unset, as for German
    assert list(vp.voices_by_language(voices_env)) == ["English", "Spanish", "French"]


# -- voice quality (2026-11-17) --------------------------------------------------------------
# kokoro-onnx-server marks each voice "quality": "good" | "experimental" (Japanese and Chinese:
# round trips n=1 came back as nonsense). The pill hides experimental voices unless the language
# is named in VOICEMODE_PILL_LANGUAGES. Servers that say nothing are classed by the voice id's
# prefix (j, z), Piper's p_<code>_ voices are good. An unknown quality value counts as good.

def _quality_servers(voice_servers, monkeypatch):
    onnx = voice_servers({"object": "list", "data": [
        {"id": "af_sky", "language": "en", "language_name": "English", "quality": "good"},
        {"id": "jf_alpha", "language": "ja", "language_name": "Japanese",
         "quality": "experimental", "quality_note": "n"},
        {"id": "zf_xiaobei", "language": "zh", "language_name": "Chinese", "quality": "experimental"},
        {"id": "hf_alpha", "language": "hi", "language_name": "Hindi", "quality": "strange"}]})
    fastapi = voice_servers({"voices": ["ef_dora", "jm_kumo", "zm_yunxi"]})
    piper = voice_servers({"object": "list", "data": [
        {"id": "p_de_eva", "language_name": "German"}, {"id": "p_ko_hana", "language_name": "Korean"}]})
    monkeypatch.setenv("VOICEMODE_TTS_BASE_URLS", ",".join(
        f"http://127.0.0.1:{p}/v1" for p in (onnx, fastapi, piper)))


def test_pill_hides_experimental_voices_by_default(voices_env, voice_servers, monkeypatch):
    _quality_servers(voice_servers, monkeypatch)
    got = vp.voices_by_language(voices_env)
    assert got == {"English": ["af_sky"], "Hindi": ["hf_alpha"], "Spanish": ["ef_dora"],
                   "German": ["p_de_eva"], "Korean": ["p_ko_hana"]}
    assert "Japanese" not in got and "Chinese" not in got


def test_pill_shows_an_experimental_language_that_is_named_explicitly(voices_env, voice_servers, monkeypatch):
    _quality_servers(voice_servers, monkeypatch)
    monkeypatch.setenv("VOICEMODE_PILL_LANGUAGES", "English, japanese")
    assert vp.voices_by_language(voices_env) == {"English": ["af_sky"], "Japanese": ["jf_alpha", "jm_kumo"]}


def test_pill_hides_ja_and_zh_voices_of_a_server_that_gives_no_quality(voices_env, monkeypatch):
    monkeypatch.setattr(vp, "_fetch_voices", lambda url: {"voices": ["af_sky", "jf_alpha", "zf_xiaobei"]})
    monkeypatch.setenv("VOICEMODE_TTS_BASE_URLS", "http://127.0.0.1:8880/v1")
    assert vp.voices_by_language(voices_env) == {"English": ["af_sky"]}


def test_snapshot_voices_by_language_hides_experimental(ctx, monkeypatch):
    monkeypatch.setattr(vp, "_fetch_voices", lambda url: {"voices": ["af_sky", "jf_alpha"]})
    monkeypatch.setenv("VOICEMODE_TTS_BASE_URLS", "http://127.0.0.1:8880/v1")
    vp.voices_by_language(ctx.home)       # the refresh the snapshot path leaves to its thread
    assert vp.gather(ctx)["voices_by_language"] == {"English": ["af_sky"]}


def test_a_voice_is_given_its_own_language_not_just_english_or_german():
    """2026-11-16: with every local language in the pill, a default voice such as ef_dora showed
    as English, because voice_language knew only English and German."""
    assert vp.voice_language("ef_dora") == "Spanish"
    assert vp.voice_language("p_nl_nathalie") == "Dutch"
    assert vp.voice_language("dm_martin") == "German" and vp.voice_language("p_de_eva") == "German"
    assert vp.voice_language("af_sky") == "English" and vp.voice_language("bm_george") == "English"
    assert vp.voice_language("xx_unknown") == "English" and vp.voice_language(None) == "English"


# -- voice preview (2026-11-17, a tester's feedback, n=1) -------------------------------------
# The page sends {"cmd": "preview_voice", "voice": id}. The backend synthesizes one short
# sentence in the voice's language through the first local TTS server that lists the voice and
# plays it in a child process, never while a voice turn is active. Answers are window messages:
# {"window": {"preview": "loading"|"playing"|"done"|"stopped"|"busy"|"error"|"unavailable"}}.

import io
import wave


def _wav(frames=240, rate=24000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x01\x00" * frames)
    return buf.getvalue()


class FakePlayer:
    """Stands in for the child process that plays the WAV."""

    def __init__(self, rc=0, hold=None):
        self.rc, self.hold, self.got, self.killed, self.returncode = rc, hold, None, False, None

    def communicate(self, data=None, timeout=None):
        self.got = data
        if self.hold is not None:
            self.hold.wait(5)
        self.returncode = -9 if self.killed else self.rc
        return None, None

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        if self.hold is not None:
            self.hold.set()

    def wait(self, timeout=None):
        return self.returncode


@pytest.fixture
def players(monkeypatch):
    made = []

    def start():
        p = FakePlayer()
        made.append(p)
        return p
    monkeypatch.setattr(vp, "_start_player", start)
    monkeypatch.setattr(vp, "_player_unavailable", lambda: None)
    return made


@pytest.fixture
def tts_servers():
    """Fake local TTS servers: GET /v1/audio/voices answers the given body, POST
    /v1/audio/speech records the JSON body and answers a short WAV."""
    started = []

    def start(voices_body, on_speech=None):
        posts = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, data, ctype):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/v1/audio/voices":
                    self._send(200, json.dumps(voices_body).encode(), "application/json")
                else:
                    self._send(404, b"", "text/plain")

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                posts.append({"path": self.path, "ctype": self.headers.get("Content-Type"),
                              "json": json.loads(body)})
                if on_speech:
                    on_speech()
                self._send(200, _wav(), "audio/wav")
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        started.append(httpd)
        return httpd.server_address[1], posts

    yield start
    for httpd in started:
        httpd.shutdown()
        httpd.server_close()


PREVIEW_REAL_FETCH = vp._fetch_voices


@pytest.fixture
def preview_ctx(ctx, monkeypatch):
    """ctx, with the real voice fetch (fake servers only) and no proxy in the way."""
    monkeypatch.setattr(vp, "_fetch_voices", PREVIEW_REAL_FETCH)
    for key in ("VOICEMODE_TTS_BASE_URLS", "http_proxy", "HTTP_PROXY"):
        monkeypatch.delenv(key, raising=False)
    return ctx


def _finish(ctx):
    t = getattr(ctx, "preview_thread", None)
    if t is not None:
        t.join(10)
        assert not t.is_alive()
    out = []
    while not ctx.replies.empty():
        out.append(ctx.replies.get_nowait())
    # "loading" is published first, by _preview itself; the callers look at what came after
    if out and out[0]["window"].get("preview") == "loading":
        out = out[1:]
    return out


def _published(ctx):
    """Everything queued for the page, in order, without waiting for a worker."""
    out = []
    while not ctx.replies.empty():
        out.append(ctx.replies.get_nowait())
    return out


def _states(replies):
    return [r["window"]["preview"] for r in replies]


def _wait_for(made):
    deadline = time.time() + 5
    while not made and time.time() < deadline:
        time.sleep(0.02)
    assert made


def test_preview_sample_is_one_short_sentence_per_language_with_english_fallback():
    en = vp.preview_text("English")
    assert en and en.endswith(".") and len(en) < 80
    assert vp.preview_text("German") != en and "Stimme" in vp.preview_text("German")
    assert vp.preview_text("Spanish") != en and "voz" in vp.preview_text("Spanish")
    for lang in ("French", "Italian", "Portuguese", "Dutch", "Polish", "Russian", "Japanese",
                 "Chinese", "Hindi", "Korean"):
        assert vp.preview_text(lang) not in ("", en), lang
    assert vp.preview_text("Klingon") == en
    assert vp.preview_text(None) == en


@pytest.mark.parametrize("msg", [{"cmd": "preview_voice"}, {"cmd": "preview_voice", "voice": ""},
                                 {"cmd": "preview_voice", "voice": 7}])
def test_preview_without_a_voice_is_an_error(preview_ctx, players, msg):
    reply = vp.handle_command(msg, preview_ctx)
    assert reply["window"]["preview"] == "error"
    assert players == [] and getattr(preview_ctx, "preview_thread", None) is None


def test_preview_of_a_voice_no_local_server_lists_is_an_error(preview_ctx, players, tts_servers, monkeypatch):
    port, posts = tts_servers({"voices": ["af_sky"]})
    monkeypatch.setenv("VOICEMODE_TTS_BASE_URLS", f"http://127.0.0.1:{port}/v1")
    assert vp.handle_command({"cmd": "preview_voice", "voice": "zz_nobody"}, preview_ctx) is None
    assert preview_ctx.replies.get(timeout=5) == {"window": {"preview": "loading", "voice": "zz_nobody"}}
    replies = _finish(preview_ctx)
    assert _states(replies) == ["error"]
    assert "unknown voice" in replies[0]["window"]["detail"]
    assert posts == [] and players == []


def test_loading_is_published_before_the_worker_can_answer(preview_ctx, monkeypatch):
    """Talos #1840: the worker started before the "loading" answer was published, so an
    instant failure was queued first and the later "loading" overwrote it (the page then
    waited 45 s). The worker below fails at once; the order must still be loading, error."""
    monkeypatch.setattr(vp, "_player_unavailable", lambda: None)
    started = threading.Event()

    def fails_instantly(ctx, voice):
        ctx.replies.put(vp._preview_reply("error", voice=voice, detail="boom"))
        with ctx.preview_lock:
            ctx.preview_running = False
        started.set()

    monkeypatch.setattr(vp, "_preview_worker", fails_instantly)
    handled = vp.handle_command({"cmd": "preview_voice", "voice": "af_sky"}, preview_ctx)
    # what run_stdio queues for the command's own return value, after handle_command returns
    queued = [handled] if handled else []
    assert started.wait(5)
    preview_ctx.preview_thread.join(5)
    got = _published(preview_ctx)
    # the page keeps the last state it was sent: it must be the terminal one
    order = _states(got + queued)
    assert order[-1] == "error" and order.index("loading") < order.index("error")


def test_preview_builds_the_tts_request_on_the_first_server_that_lists_the_voice(
        preview_ctx, players, tts_servers, monkeypatch):
    kokoro, kposts = tts_servers({"voices": ["af_sky"]})
    piper, pposts = tts_servers({"data": [{"id": "p_de_thorsten", "language_name": "German"}]})
    piper2, p2posts = tts_servers({"data": [{"id": "p_de_thorsten", "language_name": "German"}]})
    monkeypatch.setenv("VOICEMODE_TTS_BASE_URLS", f"https://api.openai.com/v1,http://127.0.0.1:{kokoro}/v1,"
                       f"http://localhost:{piper}/v1/,http://127.0.0.1:{piper2}/v1")
    assert vp.handle_command({"cmd": "preview_voice", "voice": "p_de_thorsten"}, preview_ctx) is None
    replies = _finish(preview_ctx)
    assert _states(replies) == ["playing", "done"]
    assert kposts == [] and p2posts == []
    assert pposts == [{"path": "/v1/audio/speech", "ctype": "application/json",
                       "json": {"model": "tts-1", "input": vp.preview_text("German"),
                                "voice": "p_de_thorsten", "response_format": "wav"}}]
    assert len(players) == 1 and players[0].got == _wav()


def test_preview_language_falls_back_to_the_voice_id(preview_ctx, players, tts_servers, monkeypatch):
    port, posts = tts_servers({"voices": ["ef_dora"]})
    monkeypatch.setenv("VOICEMODE_TTS_BASE_URLS", f"http://127.0.0.1:{port}/v1")
    vp.handle_command({"cmd": "preview_voice", "voice": "ef_dora"}, preview_ctx)
    _finish(preview_ctx)
    assert posts[0]["json"]["input"] == vp.preview_text("Spanish")


def test_preview_is_busy_while_a_session_holds_the_floor(preview_ctx, players, child):
    write_conch(preview_ctx, child.pid)
    reply = vp.handle_command({"cmd": "preview_voice", "voice": "af_sky"}, preview_ctx)
    assert reply == {"window": {"preview": "busy", "reason": "turn"}}
    assert players == [] and getattr(preview_ctx, "preview_thread", None) is None


@pytest.mark.parametrize("status", [{"speaking": True}, {"listening": True}, {"holding": True}])
def test_preview_is_busy_while_the_server_says_a_turn_is_on(preview_ctx, players, status):
    c = FakeControl(preview_ctx.sock, status=status)
    try:
        reply = vp.handle_command({"cmd": "preview_voice", "voice": "af_sky"}, preview_ctx)
    finally:
        c.close()
    assert reply["window"] == {"preview": "busy", "reason": "turn"}
    assert players == []


def test_preview_is_busy_while_paused(preview_ctx, players):
    st.set_paused(True, os.getpid(), preview_ctx.base)
    reply = vp.handle_command({"cmd": "preview_voice", "voice": "af_sky"}, preview_ctx)
    assert reply["window"] == {"preview": "busy", "reason": "turn"}


def test_one_preview_at_a_time(preview_ctx, tts_servers, monkeypatch):
    hold = threading.Event()
    made = []
    monkeypatch.setattr(vp, "_player_unavailable", lambda: None)
    monkeypatch.setattr(vp, "_start_player", lambda: made.append(FakePlayer(hold=hold)) or made[-1])
    port, posts = tts_servers({"voices": ["af_sky"]})
    monkeypatch.setenv("VOICEMODE_TTS_BASE_URLS", f"http://127.0.0.1:{port}/v1")
    assert vp.handle_command({"cmd": "preview_voice", "voice": "af_sky"}, preview_ctx) is None
    _wait_for(made)
    second = vp.handle_command({"cmd": "preview_voice", "voice": "af_sky"}, preview_ctx)
    assert second["window"] == {"preview": "busy", "reason": "preview"}
    hold.set()
    _finish(preview_ctx)
    assert len(posts) == 1 and len(made) == 1
    # done: the next one may start
    assert vp.handle_command({"cmd": "preview_voice", "voice": "af_sky"}, preview_ctx) is None
    _finish(preview_ctx)


def test_nothing_plays_when_a_turn_starts_while_the_sample_is_made(preview_ctx, players, tts_servers, child, monkeypatch):
    port, posts = tts_servers({"voices": ["af_sky"]}, on_speech=lambda: write_conch(preview_ctx, child.pid))
    monkeypatch.setenv("VOICEMODE_TTS_BASE_URLS", f"http://127.0.0.1:{port}/v1")
    vp.handle_command({"cmd": "preview_voice", "voice": "af_sky"}, preview_ctx)
    replies = _finish(preview_ctx)
    assert len(posts) == 1
    assert _states(replies) == ["busy"] and replies[0]["window"]["reason"] == "turn"
    assert players == []


def test_a_turn_starting_during_playback_cuts_the_preview(preview_ctx, tts_servers, monkeypatch):
    hold = threading.Event()
    made = []
    monkeypatch.setattr(vp, "_player_unavailable", lambda: None)
    monkeypatch.setattr(vp, "_start_player", lambda: made.append(FakePlayer(hold=hold)) or made[-1])
    port, _ = tts_servers({"voices": ["af_sky"]})
    monkeypatch.setenv("VOICEMODE_TTS_BASE_URLS", f"http://127.0.0.1:{port}/v1")
    vp.handle_command({"cmd": "preview_voice", "voice": "af_sky"}, preview_ctx)
    _wait_for(made)
    deadline = time.time() + 5
    while preview_ctx.preview_proc is None and time.time() < deadline:
        time.sleep(0.02)
    vp.guard_preview(preview_ctx, {"phase": "working", "sessions": [{"state": "working"}]})
    assert not made[0].killed                       # between turns: it plays on
    vp.guard_preview(preview_ctx, {"phase": "speaking", "sessions": []})
    assert made[0].killed
    replies = _finish(preview_ctx)
    assert _states(replies) == ["playing", "stopped"]


def test_a_session_taking_the_floor_cuts_the_preview_too(preview_ctx):
    p = FakePlayer()
    preview_ctx.preview_proc = p
    vp.guard_preview(preview_ctx, {"phase": "working", "sessions": [{"state": "talking"}]})
    assert p.killed


def test_stop_preview_on_exit_kills_a_running_player(preview_ctx):
    p = FakePlayer()
    preview_ctx.preview_proc = p
    vp.stop_preview(preview_ctx)
    assert p.killed


def test_preview_says_unavailable_when_nothing_can_play(preview_ctx, monkeypatch):
    monkeypatch.setattr(vp, "_player_unavailable", lambda: "no audio output module (sounddevice)")
    reply = vp.handle_command({"cmd": "preview_voice", "voice": "af_sky"}, preview_ctx)
    assert reply == {"window": {"preview": "unavailable", "detail": "no audio output module (sounddevice)"}}
    assert getattr(preview_ctx, "preview_thread", None) is None


def test_a_failed_synthesis_is_an_error_not_a_hang(preview_ctx, players, monkeypatch):
    monkeypatch.setattr(vp, "preview_owner", lambda voice, home=None: ("http://127.0.0.1:9/v1", "English"))

    def refuse(req):
        raise OSError("connection refused")
    monkeypatch.setattr(vp, "_synthesize", refuse)
    vp.handle_command({"cmd": "preview_voice", "voice": "af_sky"}, preview_ctx)
    replies = _finish(preview_ctx)
    assert _states(replies) == ["error"] and "connection refused" in replies[0]["window"]["detail"]
    assert players == []


def test_a_player_that_fails_is_reported(preview_ctx, tts_servers, monkeypatch):
    monkeypatch.setattr(vp, "_player_unavailable", lambda: None)
    monkeypatch.setattr(vp, "_start_player", lambda: FakePlayer(rc=1))
    port, _ = tts_servers({"voices": ["af_sky"]})
    monkeypatch.setenv("VOICEMODE_TTS_BASE_URLS", f"http://127.0.0.1:{port}/v1")
    vp.handle_command({"cmd": "preview_voice", "voice": "af_sky"}, preview_ctx)
    assert _states(_finish(preview_ctx)) == ["playing", "error"]


def test_play_wav_hands_the_samples_to_sounddevice(monkeypatch):
    calls = []
    fake_sd = types.SimpleNamespace(play=lambda data, rate: calls.append(("play", data.shape, rate)),
                                    wait=lambda: calls.append(("wait",)))
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)
    assert vp.play_wav_bytes(_wav(frames=240, rate=22050)) == 0
    assert calls == [("play", (240, 1), 22050), ("wait",)]


def test_player_unavailable_names_what_is_missing(monkeypatch):
    import importlib.util as iu
    real = iu.find_spec
    monkeypatch.setattr(iu, "find_spec", lambda name, *a: None if name == "sounddevice" else real(name, *a))
    assert "sounddevice" in vp._player_unavailable()


def test_the_stdio_loop_writes_the_preview_answer(tmp_path):
    env = {**os.environ, "HOME": str(tmp_path), "VOICEMODE_BASE_DIR": str(tmp_path / "pill")}
    env.pop("VOICEMODE_CONTROL_SOCKET", None)
    p = subprocess.Popen([sys.executable, str(PATCHES / "voice_pill.py"), "--stdio"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, env=env, text=True)
    try:
        json.loads(p.stdout.readline())
        p.stdin.write(json.dumps({"cmd": "preview_voice", "voice": 5}) + "\n")
        p.stdin.flush()
        got = None
        for _ in range(4):
            line = json.loads(p.stdout.readline())
            if "window" in line:
                got = line
                break
        assert got and got["window"]["preview"] == "error"
        p.stdin.close()
        assert p.wait(timeout=5) == 0
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()


def test_the_play_wav_child_rejects_bad_input_before_touching_audio(tmp_path):
    """The real child entry point: no WAV on stdin gives a non-zero exit, and sounddevice is
    never imported (a shim that would exit 99 stands first on the path)."""
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "sounddevice.py").write_text("raise SystemExit(99)\n")
    env = {**os.environ, "PYTHONPATH": str(shim)}
    out = subprocess.run([sys.executable, str(PATCHES / "voice_pill.py"), "--play-wav"],
                         input=b"not a wav", capture_output=True, env=env, timeout=30)
    assert out.returncode not in (0, 99), out


# -- one language table (2026-11-17, Talos note on FELIX PR #1840) ----------------------------
# voice_pill.py kept its own KOKORO_LANGUAGES / PIPER_LANGUAGES / language_from_id beside
# voice_languages.py's (copied into the package for the unknown-voice fallback). They had drifted:
# the pill read "p_x" and "pizza" as Portuguese and "p_en_amy" as unknown, the fallback the
# other way round. The pill now uses voice_languages; one voice id has one language.

vl = _load("voice_languages")

ID_CASES = ["af_sky", "am_adam", "bf_emma", "bm_george", "ef_dora", "em_alex", "ff_siwis", "hf_alpha",
            "if_sara", "im_nicola", "jf_alpha", "jm_kumo", "pf_dora", "pm_alex", "zf_xiaobei",
            "zm_yunxi", "df_x", "dm_martin", "p_de_thorsten", "p_de_eva", "p_nl_nathalie",
            "p_pl_gosia", "p_ru_dmitri", "p_ko_hana", "p_en_amy", "p_xx_who", "p_x", "p_", "p",
            "pizza", "afoo", "nova", "xf_x", "xx_unknown", "a", "af", "", None, 5, b"af_sky", ["af_sky"]]


@pytest.mark.parametrize("voice", ID_CASES, ids=repr)
def test_the_pill_and_the_fallback_read_a_voice_id_the_same(voice):
    assert vp.language_from_id(voice) == vl.language_from_id(voice)


def test_the_pill_holds_no_language_table_of_its_own():
    assert not hasattr(vp, "KOKORO_LANGUAGES") and not hasattr(vp, "PIPER_LANGUAGES")
    assert vp.language_from_id.__module__ == "voice_languages"


@pytest.mark.parametrize("voice,quality", [
    ("jf_alpha", "experimental"), ("jm_kumo", "experimental"), ("zf_xiaobei", "experimental"),
    ("af_sky", "good"), ("ef_dora", "good"), ("p_ko_hana", "good"), ("p_de_eva", "good"),
    ("pizza", "good"), ("jizz", "good"), ("zebra", "good")])
def test_voice_quality_from_the_id_builds_on_the_shared_language(voice, quality):
    assert vp._id_quality(voice) == quality


# -- the voice list never blocks the command loop (2026-11-17, Talos note on FELIX PR #1840) ----
# gather() ran voices_by_language in the command loop thread: with a local TTS endpoint
# stalled, a snapshot (and so Stop / Pause behind it) waited up to VOICES_TIMEOUT_S per endpoint.
# Now stale-while-revalidate: gather() answers from the cache at once ({} before the first
# refresh) and ONE background daemon thread refreshes it when it is older than VOICES_CACHE_S.

@pytest.fixture
def swr(ctx, monkeypatch):
    """A ctx whose gather() may start the refresh thread; the thread is awaited at the end."""
    monkeypatch.setattr(vp, "BACKGROUND_REFRESH", True, raising=False)
    monkeypatch.setattr(vp, "_REFRESH_THREAD", None, raising=False)
    monkeypatch.setattr(vp, "_REFRESH_WARNED", False, raising=False)
    monkeypatch.setenv("VOICEMODE_TTS_BASE_URLS", "http://127.0.0.1:8880/v1")
    release = threading.Event()
    yield ctx, release
    release.set()
    t = vp._REFRESH_THREAD
    if t is not None:
        t.join(5)


def _wait_refresh():
    t = vp._REFRESH_THREAD
    if t is not None:
        t.join(5)
        assert not t.is_alive()


def test_gather_does_not_wait_for_a_stalled_endpoint(swr, monkeypatch):
    ctx, release = swr
    monkeypatch.setattr(vp, "_fetch_voices",
                        lambda url: release.wait(3) and {"voices": ["af_sky", "ff_siwis"]})
    t0 = time.monotonic()
    first = vp.gather(ctx)
    assert time.monotonic() - t0 < 0.1
    assert first["voices_by_language"] == {}               # nothing known yet
    release.set()
    _wait_refresh()
    assert vp.gather(ctx)["voices_by_language"] == {"English": ["af_sky"], "French": ["ff_siwis"]}


def test_a_stale_list_is_served_at_once_while_it_is_refreshed(swr, monkeypatch):
    ctx, release = swr
    monkeypatch.setattr(vp, "_fetch_voices", lambda url: {"voices": ["af_sky"]})
    clock = [1000.0]
    monkeypatch.setattr(vp, "_clock", lambda: clock[0])
    vp.gather(ctx)
    _wait_refresh()
    assert vp.gather(ctx)["voices_by_language"] == {"English": ["af_sky"]}
    clock[0] += 61
    monkeypatch.setattr(vp, "_fetch_voices", lambda url: release.wait(3) and {"voices": ["ef_dora"]})
    t0 = time.monotonic()
    assert vp.gather(ctx)["voices_by_language"] == {"English": ["af_sky"]}   # the old one, now
    assert time.monotonic() - t0 < 0.1
    release.set()
    _wait_refresh()
    assert vp.gather(ctx)["voices_by_language"] == {"Spanish": ["ef_dora"]}


def test_quick_gathers_start_one_refresh_thread(swr, monkeypatch):
    ctx, release = swr
    monkeypatch.setattr(vp, "_fetch_voices", lambda url: release.wait(3) and {"voices": []})
    started = []
    real = threading.Thread

    class Spy(real):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            started.append(self)
    monkeypatch.setattr(vp.threading, "Thread", Spy)
    for _ in range(5):
        vp.gather(ctx)
    assert len(started) == 1 and started[0].daemon is True


def test_a_failing_refresh_keeps_the_old_list_and_says_so_once(swr, monkeypatch, capsys):
    ctx, release = swr
    clock = [1000.0]
    monkeypatch.setattr(vp, "_clock", lambda: clock[0])
    monkeypatch.setattr(vp, "_fetch_voices", lambda url: {"voices": ["af_sky"]})
    vp.gather(ctx)
    _wait_refresh()

    def boom(url):
        raise OSError("endpoint down")
    monkeypatch.setattr(vp, "_fetch_voices", boom)
    for _ in range(2):                       # an endpoint that is down: every refresh finds nothing
        clock[0] += 61
        vp.gather(ctx)
        _wait_refresh()
        assert vp.gather(ctx)["voices_by_language"] == {"English": ["af_sky"]}

    def broken(raw):
        raise RuntimeError("discovery broke")
    monkeypatch.setattr(vp, "_ask_servers", broken)   # a refresh that itself raises
    for _ in range(2):
        clock[0] += 61
        vp.gather(ctx)
        _wait_refresh()
        assert vp.gather(ctx)["voices_by_language"] == {"English": ["af_sky"]}
    assert capsys.readouterr().err.count("voice list refresh failed") == 1


def test_the_refresh_thread_is_a_daemon_so_shutdown_does_not_wait_for_it(swr, monkeypatch):
    ctx, release = swr
    monkeypatch.setattr(vp, "_fetch_voices", lambda url: release.wait(30))
    vp.gather(ctx)
    assert vp._REFRESH_THREAD.is_alive() and vp._REFRESH_THREAD.daemon is True


def test_gather_starts_no_thread_unless_background_refresh_is_on(ctx, monkeypatch):
    monkeypatch.setattr(vp, "_REFRESH_THREAD", None, raising=False)
    monkeypatch.setenv("VOICEMODE_TTS_BASE_URLS", "http://127.0.0.1:8880/v1")
    vp.gather(ctx)
    assert vp._REFRESH_THREAD is None


def test_the_preview_does_its_network_io_off_the_command_thread(ctx, monkeypatch):
    """preview_owner fetches voice lists; _preview only starts the worker thread that calls it."""
    names = []
    monkeypatch.setattr(vp, "preview_owner", lambda voice, home=None: names.append(
        threading.current_thread().name) or (None, None))
    monkeypatch.setattr(vp, "_player_unavailable", lambda: None)
    monkeypatch.setattr(vp, "_turn_active", lambda c: False)
    assert vp._preview(ctx, "af_sky") is None
    ctx.preview_thread.join(5)
    assert names == ["voice-pill-preview"]


# FELIX #2095 (2026-11-27): the voice server's loop was stuck for minutes while its control
# socket (a thread) kept answering "speaking", so the pill showed Speaking with no audio.
# The status reply now carries loop_lag_s (patch_audio_deadline).
@pytest.mark.parametrize("status,paused,phase", [
    ({"speaking": True, "loop_lag_s": 45.0}, False, "not_responding"),
    ({"listening": True, "loop_lag_s": 11.0}, False, "not_responding"),
    ({"speaking": True, "loop_lag_s": 45.0}, True, "not_responding"),
    ({"speaking": True, "loop_lag_s": 9.5}, False, "speaking"),   # a slow moment, not a hang
    ({"speaking": True, "loop_lag_s": None}, False, "speaking"),  # watchdog not started yet
    ({"speaking": True}, False, "speaking"),                      # older server: no field
    ({"speaking": True, "loop_lag_s": "x"}, False, "speaking"),
])
def test_a_stuck_server_shows_not_responding(status, paused, phase):
    assert snap(status=status, paused=paused, sessions=[sess(1)])["phase"] == phase


# FELIX #2094, UX review 2026-11-27: the pill's Done sends "done". It ends YOUR turn
# (skip_forward ends the recording and transcribes it), so it is forwarded only while the
# server says it is listening: a Done that lands just after listening ended must not skip
# the assistant's next reply.
def test_done_while_listening_sends_skip_forward(ctx, control, monkeypatch):
    monkeypatch.setattr(st, "_status", lambda path: {"listening": True, "speaking": False})
    vp.handle_command({"cmd": "done"}, ctx)
    time.sleep(0.1)
    assert control.received == ["skip_forward"]


@pytest.mark.parametrize("status", [{"listening": False, "speaking": True}, {}, {"listening": True, "speaking": True}])
def test_done_when_not_listening_does_nothing(ctx, control, monkeypatch, status):
    monkeypatch.setattr(st, "_status", lambda path: status)
    vp.handle_command({"cmd": "done"}, ctx)
    time.sleep(0.1)
    assert control.received == []


def test_done_with_no_socket_is_harmless(ctx):
    vp.handle_command({"cmd": "done"}, ctx)


# FELIX #2107: a turn that keeps listening gets a note in the pill after N seconds ("still
# listening, press Done"); never a sound (it would land in the recording) and never an end.
@pytest.mark.parametrize("status, after, still", [
    ({"listening": True, "listen_started_at": 1000.0}, 30.0, True),     # 31 s
    ({"listening": True, "listen_started_at": 1010.0}, 30.0, False),    # 21 s
    ({"listening": True, "listen_started_at": 1000.0}, 0.0, False),     # 0 = off
    ({"speaking": True, "listen_started_at": 1000.0}, 30.0, False),     # not listening
    ({"listening": True}, 30.0, False),                                 # older server: no field
    ({"listening": True, "listen_started_at": "x"}, 30.0, False),
])
def test_still_listening_after_n_seconds(status, after, still):
    s = snap(status=status, sessions=[sess(1)], still_after_s=after, now=1031.0)
    assert s["still_listening"] is still


def test_still_listening_setting(monkeypatch, tmp_path):
    monkeypatch.setenv("VOICEMODE_PILL_STILL_LISTENING_S", "45")
    assert vp.still_after(tmp_path) == 45.0
    for bad in ("x", "-1", "nan", "inf"):
        monkeypatch.setenv("VOICEMODE_PILL_STILL_LISTENING_S", bad)
        assert vp.still_after(tmp_path) == 30.0
    monkeypatch.setenv("VOICEMODE_PILL_STILL_LISTENING_S", "0")
    assert vp.still_after(tmp_path) == 0.0
