"""Per-harness wakers for the conch queue's callback mode (patches/conch_wakers.py,
patches/patch_conch_wakers.py). FELIX on Windows, 2026-11-11: a Copilot chat queued with
conch_mode="callback", nothing could wake it, and the queue stood still."""
import importlib.util
import json
import logging
import os
import shutil
import subprocess
import sys
import textwrap
import threading
import types
from pathlib import Path

import pytest

PATCHES = Path(__file__).resolve().parent.parent / "patches"
sys.path.insert(0, str(PATCHES))


def _load(name, as_name=None):
    spec = importlib.util.spec_from_file_location(as_name or name, PATCHES / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[as_name or name] = mod
    spec.loader.exec_module(mod)
    return mod


cw = _load("conch_wakers")
sw = _load("session_wake")
patcher = _load("patch_conch_wakers")


@pytest.fixture
def clean(monkeypatch):
    monkeypatch.delenv(cw.COMMAND_SETTING, raising=False)
    monkeypatch.setattr(cw, "_session_wake", lambda: sw)
    monkeypatch.setattr(cw, "claude_pid", lambda pid: None)
    monkeypatch.setattr(cw, "tmux_available", lambda: False)
    return monkeypatch


Entry = types.SimpleNamespace


# -- waker_for ----------------------------------------------------------------

def test_no_waker_anywhere(clean):
    assert cw.waker_for(1234) is None


def test_the_harness_command_comes_first(clean):
    clean.setenv(cw.COMMAND_SETTING, json.dumps(["felix-wake", "--chat"]))
    clean.setattr(cw, "claude_pid", lambda pid: 99)
    assert cw.waker_for(1234) == "command"


def test_claude_code_then_tmux(clean):
    clean.setattr(cw, "claude_pid", lambda pid: 99)
    assert cw.waker_for(1234) == "claude-code"
    clean.setattr(cw, "claude_pid", lambda pid: None)
    clean.setattr(cw, "tmux_available", lambda: True)
    assert cw.waker_for(1234) == "tmux"


def test_a_failing_lookup_means_no_waker(clean):
    def boom(pid):
        raise RuntimeError("registry unreadable")
    clean.setattr(cw, "claude_pid", boom)
    assert cw.waker_for(1234) is None


@pytest.mark.parametrize("raw,expected", [
    ('["a", "b"]', ["a", "b"]), ("/usr/bin/felix-wake", ["/usr/bin/felix-wake"]),
    ('"x"', ["x"]), ("[]", None), ('[1, 2]', None), ('{"a": 1}', None), ("", None)])
def test_wake_command_parsing(clean, raw, expected):
    clean.setenv(cw.COMMAND_SETTING, raw)
    assert cw.wake_command() == expected


def test_the_command_is_read_from_the_environment_only(clean, tmp_path):
    # Like VOICEMODE_BARGE_CONTROL_COMMAND: a model can write voicemode.env, so a file
    # value must never become a command that runs.
    assert "voicemode.env" not in Path(cw.__file__).read_text().split("def wake_command")[1].split("def ")[0]


# -- claude_pid -----------------------------------------------------------------

def test_claude_code_wake_is_posix_only_and_obeys_the_switch(monkeypatch):
    monkeypatch.setattr(cw, "_session_wake", lambda: sw)
    monkeypatch.setattr(sw, "claude_pid_for", lambda pid: 4242)
    monkeypatch.setattr(sw, "enabled", lambda: True)
    assert cw.claude_pid(1234) == 4242
    monkeypatch.setattr(sw, "enabled", lambda: False)
    assert cw.claude_pid(1234) is None
    monkeypatch.setattr(sw, "enabled", lambda: True)
    monkeypatch.setattr(cw.os, "name", "nt")
    assert cw.claude_pid(1234) is None   # session_wake is unverified on native Windows
    monkeypatch.setattr(cw.os, "name", "posix")
    assert cw.claude_pid(None) is None


# -- deliver ------------------------------------------------------------------

def _recording_command(tmp_path, code=0):
    out = tmp_path / "args.json"
    script = tmp_path / "wake.py"
    script.write_text(f"import json, sys\nopen({str(out)!r}, 'w').write(json.dumps(sys.argv[1:]))\nsys.exit({code})\n")
    return [sys.executable, str(script)], out


def test_deliver_runs_the_command_with_session_and_pid(clean, tmp_path):
    command, out = _recording_command(tmp_path)
    clean.setenv(cw.COMMAND_SETTING, json.dumps(command))
    assert cw.deliver(Entry(pid=321, session_id="chat-1", mode="callback")) is True
    assert json.loads(out.read_text()) == ["chat-1", "321"]


def test_a_failing_command_falls_through_to_claude_code(clean, tmp_path):
    command, _ = _recording_command(tmp_path, code=1)
    clean.setenv(cw.COMMAND_SETTING, json.dumps(command))
    clean.setattr(cw, "claude_pid", lambda pid: 77)
    calls = []
    clean.setattr(sw, "wake", lambda pid, reason: calls.append((pid, reason)) or (True, "sent"))
    assert cw.deliver(Entry(pid=321, session_id="chat-1")) is True
    assert calls == [(77, "conch")]


def test_a_refused_claude_code_wake_is_not_delivered(clean):
    clean.setattr(cw, "claude_pid", lambda pid: 77)
    clean.setattr(sw, "wake", lambda pid, reason: (False, "refused: busy"))
    assert cw.outcome(Entry(pid=321, session_id="chat-1")) == cw.REFUSED   # settled: upstream must not nudge


def test_deliver_never_raises(clean):
    def boom(pid):
        raise RuntimeError("x")
    clean.setattr(cw, "claude_pid", boom)
    assert cw.deliver(Entry(pid=321, session_id="s")) is False


def test_deliver_then_uses_upstream_only_when_ours_failed(clean):
    used = []
    clean.setattr(cw, "deliver", lambda e, state_dir=None: False)
    cw.deliver_then(Entry(pid=5, session_id="s"), used.append)
    cw.deliver_then(Entry(pid=None, session_id="s"), used.append)   # remote: upstream has no local route
    clean.setattr(cw, "deliver", lambda e, state_dir=None: True)
    cw.deliver_then(Entry(pid=6, session_id="s"), used.append)
    assert [e.pid for e in used] == [5]


def test_the_conch_message_is_fixed_and_small():
    text = sw.MESSAGES["conch"]
    assert "converse" in text and len(text.encode()) <= sw.MAX_MESSAGE_BYTES


# -- the patch, on pristine 8.12.0 ---------------------------------------------

def _pristine(tmp_path, rel):
    raw = os.environ.get("VM812_SRC")
    if not raw:
        pytest.skip("VM812_SRC not set (dir holding pristine 8.12.0 sources)")
    src = Path(raw) / Path(rel).name
    if not src.is_file():
        pytest.fail(f"VM812_SRC={raw} has no {Path(rel).name}")
    dest = tmp_path / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    return dest


def _apply(path):
    return subprocess.run([sys.executable, str(PATCHES / "patch_conch_wakers.py"), str(path)],
                          capture_output=True, text=True)


def _converse_mode_block(source):
    start = source.index("    if conch_mode is None:\n")
    end = source.index(patcher.CONVERSE_ADD) + len(patcher.CONVERSE_ADD)
    return textwrap.dedent(source[start:end])


@pytest.mark.parametrize("asked,waker,served", [
    ("callback", None, "wait"), ("callback", "claude-code", "callback"),
    ("wait", None, "wait"), (None, None, "wait"), ("bogus", "tmux", "wait")])
def test_converse_serves_callback_as_wait_without_a_waker(tmp_path, monkeypatch, asked, waker, served):
    target = _pristine(tmp_path, "tools/converse.py")
    assert _apply(target).returncode == 0
    fake = types.ModuleType("voice_mode.conch_wakers")
    fake.waker_for = lambda pid: waker
    pkg = types.ModuleType("voice_mode")
    pkg.conch_wakers = fake
    monkeypatch.setitem(sys.modules, "voice_mode", pkg)
    monkeypatch.setitem(sys.modules, "voice_mode.conch_wakers", fake)
    ns = {"conch_mode": asked, "CONCH_MODE": "wait", "os": os, "logger": logging.getLogger("t")}
    exec(_converse_mode_block(target.read_text()), ns)
    assert ns["resolved_conch_mode"] == served


def _notify_module(tmp_path, monkeypatch, deliver):
    target = _pristine(tmp_path, "conch_notify.py")
    assert _apply(target).returncode == 0
    fake = types.ModuleType("voice_mode.conch_wakers")
    calls = []
    fake.deliver = lambda e: calls.append(("deliver", e.session_id)) or deliver
    fake.deliver_then = lambda e, fallback: (calls.append(("deliver_then", e.session_id)),
                                             None if deliver else fallback(e))
    pkg = types.ModuleType("voice_mode")
    pkg.conch_wakers = fake
    monkeypatch.setitem(sys.modules, "voice_mode", pkg)
    monkeypatch.setitem(sys.modules, "voice_mode.conch_wakers", fake)
    spec = importlib.util.spec_from_file_location("conch_notify_under_test", target)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "_local_nudge", lambda e: calls.append(("tmux", e.session_id)))
    return mod, calls


def test_notify_uses_our_waker_and_skips_tmux_when_it_delivered(tmp_path, monkeypatch):
    mod, calls = _notify_module(tmp_path, monkeypatch, deliver=True)
    mod.notify_granted(Entry(mode="callback", pid=5, session_id="s1", project_path="/p"))
    assert calls == [("deliver", "s1")]


def test_notify_falls_back_to_tmux_when_ours_did_not(tmp_path, monkeypatch):
    mod, calls = _notify_module(tmp_path, monkeypatch, deliver=False)
    mod.notify_granted(Entry(mode="callback", pid=5, session_id="s1", project_path="/p"))
    assert calls == [("deliver", "s1"), ("tmux", "s1")]


def test_notify_on_the_release_path_runs_off_thread(tmp_path, monkeypatch):
    mod, calls = _notify_module(tmp_path, monkeypatch, deliver=False)
    started = []
    monkeypatch.setattr(mod, "_dispatch_async", lambda fn, *a: started.append(fn) or fn(*a))
    mod.notify_granted(Entry(mode="callback", pid=5, session_id="s1", project_path="/p"), block=False)
    assert len(started) == 1 and calls == [("deliver_then", "s1"), ("tmux", "s1")]


def test_notify_leaves_wait_grantees_alone(tmp_path, monkeypatch):
    mod, calls = _notify_module(tmp_path, monkeypatch, deliver=True)
    mod.notify_granted(Entry(mode="wait", pid=5, session_id="s1", project_path="/p"))
    assert calls == []


@pytest.mark.parametrize("rel", ["tools/converse.py", "conch_notify.py"])
def test_patch_is_idempotent_and_keeps_crlf(tmp_path, rel):
    target = _pristine(tmp_path, rel)
    target.write_bytes(target.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    assert _apply(target).returncode == 0
    once = target.read_bytes()
    assert _apply(target).returncode == 0 and target.read_bytes() == once
    assert b"\r\n" in once and b"\n" not in once.replace(b"\r\n", b"")


def test_patch_stops_on_a_missing_anchor(tmp_path):
    target = tmp_path / "conch_notify.py"
    target.write_text("def notify_granted(entry):\n    pass\n")
    done = _apply(target)
    assert done.returncode == 1 and "voice-mode has changed" in done.stderr


# -- policy: grace, wake cap, busy exit (plan 2026-11-11-conch-callback, Task 1) -----------

from datetime import datetime, timedelta


def _grant(age_s, given=False, now=None):
    now = now or datetime(2026, 11, 11, 15, 0, 0)
    g = {"session_id": "A", "seq": 4, "granted_at": (now - timedelta(seconds=age_s)).isoformat()}
    if given:
        g["given"] = True
    return g, now


@pytest.fixture
def policy_env(monkeypatch):
    monkeypatch.delenv(cw.GRACE_SETTING, raising=False)
    monkeypatch.delenv(cw.MAX_WAKES_SETTING, raising=False)
    return monkeypatch


def test_grant_is_absent_after_the_grace_only_for_callback(policy_env):
    g, now = _grant(11)
    assert cw.grant_is_absent(g, Entry(mode="callback"), now=now) is True
    g, now = _grant(9)
    assert cw.grant_is_absent(g, Entry(mode="callback"), now=now) is False
    g, now = _grant(60)
    assert cw.grant_is_absent(g, Entry(mode="wait"), now=now) is False


def test_a_given_grant_holds_until_the_ttl(policy_env):
    g, now = _grant(20, given=True)
    assert cw.grant_is_absent(g, Entry(mode="callback"), now=now, ttl=30.0) is False
    g, now = _grant(31, given=True)
    assert cw.grant_is_absent(g, Entry(mode="callback"), now=now, ttl=30.0) is True


def test_a_grant_without_granted_at_is_never_absent(policy_env):
    assert cw.grant_is_absent({"session_id": "A", "seq": 4}, Entry(mode="callback")) is False
    assert cw.grant_is_absent({"session_id": "A", "granted_at": "garbage"}, Entry(mode="callback")) is False


@pytest.mark.parametrize("raw,expected", [("4", 4.0), ("x", 10.0), ("-1", 10.0), (None, 10.0)])
def test_grace_setting(policy_env, raw, expected):
    if raw is not None:
        policy_env.setenv(cw.GRACE_SETTING, raw)
    assert cw.grace_s() == expected


@pytest.mark.parametrize("raw,expected", [("2", 2), ("0", 0), ("x", 5), (None, 5)])
def test_max_wakes_setting(policy_env, raw, expected):
    if raw is not None:
        policy_env.setenv(cw.MAX_WAKES_SETTING, raw)
    assert cw.max_wakes() == expected


def test_take_wake_caps_per_queued_turn(policy_env, tmp_path):
    assert [cw.take_wake(tmp_path, "A", 4) for _ in range(6)] == [True] * 5 + [False]
    assert cw.take_wake(tmp_path, "A", 9) is True        # a new queued turn starts again
    assert cw.take_wake(tmp_path, "B", 4) is True        # another session has its own count


def test_take_wake_survives_a_broken_state_file(policy_env, tmp_path):
    (tmp_path / "conch.wakes.json").write_text("{not json")
    assert cw.take_wake(tmp_path, "A", 4) is True


def test_deliver_stops_at_the_cap(clean, policy_env, tmp_path):
    policy_env.setenv(cw.MAX_WAKES_SETTING, "1")
    clean.setattr(cw, "claude_pid", lambda pid: 77)
    calls = []
    clean.setattr(sw, "wake", lambda pid, reason: calls.append(pid) or (True, "sent"))
    e = Entry(pid=321, session_id="A", seq=4)
    assert cw.outcome(e, tmp_path) == cw.DELIVERED
    assert cw.outcome(e, tmp_path) == cw.REFUSED
    assert calls == [77]


def test_a_busy_command_does_not_fall_through(clean, tmp_path):
    command, _ = _recording_command(tmp_path, code=cw.EXIT_BUSY)
    clean.setenv(cw.COMMAND_SETTING, json.dumps(command))
    clean.setattr(cw, "claude_pid", lambda pid: 77)
    calls = []
    clean.setattr(sw, "wake", lambda pid, reason: calls.append(pid) or (True, "sent"))
    assert cw.outcome(Entry(pid=321, session_id="A", seq=4)) == cw.REFUSED
    assert calls == []


def test_the_conch_message_lets_the_session_stay_silent():
    text = sw.MESSAGES["conch"]
    assert "nothing" in text and "converse" in text and "\n" not in text
    assert len(text.encode()) <= sw.MAX_MESSAGE_BYTES


# -- upstream insertions in conch_queue.py / conch.py (plan Task 2) ---------------------------
# A real voice_mode package in tmp_path: pristine 8.12.0 conch.py, conch_queue.py,
# conch_notify.py, file_lock.py, patched; our conch_wakers.py; stubs for config,
# the event logger and session_wake (which records each wake).

_STUB_WAKE = '''
WAKES = []
def enabled(home=None):
    return True
def claude_pid_for(voice_pid, cdir=None, parent=None):
    return 77
def wake(target_claude_pid=None, reason="ptt", **kw):
    WAKES.append((target_claude_pid, reason))
    return True, "sent"
'''


@pytest.fixture
def queue(tmp_path, monkeypatch):
    raw = os.environ.get("VM812_SRC")
    if not raw:
        pytest.skip("VM812_SRC not set (dir holding pristine 8.12.0 sources)")
    pkg = tmp_path / "pkg" / "voice_mode"
    (pkg / "utils").mkdir(parents=True)
    for name in ("conch.py", "conch_queue.py", "conch_notify.py", "file_lock.py"):
        shutil.copyfile(Path(raw) / name, pkg / name)
        if name != "file_lock.py":
            assert _apply(pkg / name).returncode == 0, name
    shutil.copyfile(PATCHES / "conch_wakers.py", pkg / "conch_wakers.py")
    (pkg / "__init__.py").write_text("")
    (pkg / "utils" / "__init__.py").write_text("")
    (pkg / "utils" / "event_logger.py").write_text("def get_event_logger():\n    return None\n")
    (pkg / "config.py").write_text("CONCH_LOCK_EXPIRY = 120\nCONCH_HOLD_EXPIRY = 10\nCONCH_GRANT_TTL = 30.0\n")
    (pkg / "session_wake.py").write_text(_STUB_WAKE)
    saved = {k: v for k, v in sys.modules.items() if k == "voice_mode" or k.startswith("voice_mode.")}
    for k in saved:
        del sys.modules[k]
    sys.path.insert(0, str(pkg.parent))
    monkeypatch.delenv(cw.COMMAND_SETTING, raising=False)
    monkeypatch.delenv(cw.GRACE_SETTING, raising=False)
    monkeypatch.delenv(cw.MAX_WAKES_SETTING, raising=False)
    try:
        import voice_mode.conch as conch
        import voice_mode.conch_queue as cq
        import voice_mode.session_wake as stub
        monkeypatch.setattr(conch.Conch, "LOCK_FILE", tmp_path / "home" / "conch")
        (tmp_path / "home").mkdir()
        yield types.SimpleNamespace(Conch=conch.Conch, Q=cq.ConchQueue, wakes=stub.WAKES,
                                    grant_file=lambda: cq.ConchQueue._grant_file())
    finally:
        sys.path.remove(str(pkg.parent))
        for k in [k for k in sys.modules if k == "voice_mode" or k.startswith("voice_mode.")]:
            del sys.modules[k]
        sys.modules.update(saved)


def _wait_for(pred, timeout=3.0):
    import time as _time
    deadline = _time.monotonic() + timeout
    while not pred() and _time.monotonic() < deadline:
        _time.sleep(0.01)
    return pred()


def _age_grant(q, seconds):
    g = json.loads(q.grant_file().read_text())
    from datetime import datetime as _dt, timedelta as _td
    g["granted_at"] = (_dt.now() - _td(seconds=seconds)).isoformat()
    q.grant_file().write_text(json.dumps(g))


def test_a_lone_callback_grantee_is_pinged_on_grant(queue):
    queue.Q.register("A", mode="callback", pid=os.getpid())
    assert queue.Q.grant_next(notify_block=False).session_id == "A"   # the release path
    assert _wait_for(lambda: queue.wakes == [(77, "conch")])


def test_a_late_wait_waiter_passes_an_absent_callback_grant(queue):
    queue.Q.register("A", mode="callback", pid=os.getpid())
    queue.Q.grant_next(notify_block=False)
    assert _wait_for(lambda: len(queue.wakes) == 1)
    seq_a = [e.seq for e in queue.Q.list() if e.session_id == "A"][0]
    _age_grant(queue, 11)
    queue.Q.register("B", mode="wait", pid=os.getpid())
    assert queue.Q.granted_to() == "B"
    assert [e.seq for e in queue.Q.list() if e.session_id == "A"] == [seq_a]
    # No ping on the pass (live 2026-11-11: it said "your turn" while B was about to speak);
    # A is woken at B's release, see test_a_wait_waiter_taking_an_absent_grant_wakes_nobody.
    assert not _wait_for(lambda: len(queue.wakes) > 1, timeout=0.5)


def test_within_the_grace_the_woken_session_keeps_its_turn(queue):
    queue.Q.register("A", mode="callback", pid=os.getpid())
    queue.Q.grant_next()
    _age_grant(queue, 9)
    queue.Q.register("B", mode="wait", pid=os.getpid())
    assert queue.Q.granted_to() == "A"


def test_a_free_mic_is_served_past_an_absent_callback_grant(queue):
    queue.Q.register("A", mode="callback", pid=os.getpid())
    queue.Q.grant_next()
    _age_grant(queue, 11)
    c = queue.Conch(agent_name="c", session_id="C")
    try:
        assert c.try_acquire() is True
    finally:
        c.release()


def test_a_fresh_callback_grant_still_blocks_a_free_mic(queue):
    queue.Q.register("A", mode="callback", pid=os.getpid())
    queue.Q.grant_next()
    c = queue.Conch(agent_name="c", session_id="C")
    assert c.try_acquire() is False


def test_a_give_to_a_callback_session_holds_for_the_ttl(queue):
    queue.Q.register("A", mode="callback", pid=os.getpid())
    queue.Q.register("B", mode="wait", pid=os.getpid())
    assert queue.Q.grant("A") is True
    assert json.loads(queue.grant_file().read_text()).get("given") is True
    _age_grant(queue, 20)
    assert queue.Q.granted_to() == "A"
    _age_grant(queue, 31)
    assert queue.Q.granted_to() == "B"


def test_the_sixth_grant_event_sends_no_wake(queue):
    queue.Q.register("A", mode="callback", pid=os.getpid())
    for _ in range(6):   # six grant events: each time someone else had the floor in between
        queue.Q.clear_grant()
        queue.Q.grant_next(notify_block=False)
        _wait_for(lambda: False, timeout=0.05)
    _wait_for(lambda: len(queue.wakes) >= 5)
    _wait_for(lambda: False, timeout=0.3)
    assert len(queue.wakes) == 5


def test_unpatched_grant_file_is_read_as_not_given_and_never_absent(queue):
    queue.Q.register("A", mode="callback", pid=os.getpid())
    seq = queue.Q.list()[0].seq
    queue.grant_file().write_text(json.dumps({"session_id": "A", "seq": seq}))
    queue.Q.register("B", mode="wait", pid=os.getpid())
    assert queue.Q.granted_to() == "A"
    assert queue.Conch(agent_name="c", session_id="C").try_acquire() is False


@pytest.mark.parametrize("rel", ["conch_queue.py", "conch.py"])
def test_queue_patches_are_idempotent_and_keep_crlf(tmp_path, rel):
    target = _pristine(tmp_path, rel)
    target.write_bytes(target.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    assert _apply(target).returncode == 0
    once = target.read_bytes()
    assert patcher.MARKER.encode() in once
    assert _apply(target).returncode == 0 and target.read_bytes() == once


# -- final review fixes (Important 1-5) ----------------------------------------------------


def test_a_returning_grantee_past_the_grace_takes_its_turn(queue):
    # Important 1: A comes back at 11 s while W waits; A is present and first: A talks.
    queue.Q.register("A", mode="callback", pid=os.getpid())
    queue.Q.grant_next(notify_block=False)
    _age_grant(queue, 11)
    queue.Q.register("W", mode="wait", pid=os.getpid())
    a = queue.Conch(agent_name="a", session_id="A")
    try:
        assert a.try_acquire() is True
    finally:
        a.release()


def test_the_cap_also_stops_upstreams_tmux_nudge(queue, monkeypatch):
    # Important 2: with no waker of ours, upstream's tmux route is capped too.
    import voice_mode.session_wake as stub
    import voice_mode.conch_notify as cn
    monkeypatch.setattr(stub, "claude_pid_for", lambda *a, **k: None)
    nudges = []
    monkeypatch.setattr(cn, "_session_send", lambda target: nudges.append(target) or True)
    queue.Q.register("A", mode="callback", pid=os.getpid())
    for _ in range(7):
        queue.Q.clear_grant()
        queue.Q.grant_next(notify_block=False)
        _wait_for(lambda: False, timeout=0.05)
    _wait_for(lambda: len(nudges) >= 5)
    _wait_for(lambda: False, timeout=0.3)
    assert len(nudges) == 5


def test_a_refused_wake_does_not_fall_back_to_tmux(queue, monkeypatch):
    # Important 2: busy means refused: the session keeps its place, nobody else nudges it.
    import voice_mode.session_wake as stub
    import voice_mode.conch_notify as cn
    monkeypatch.setattr(stub, "wake", lambda *a, **k: (False, "refused: busy"))
    nudges = []
    monkeypatch.setattr(cn, "_session_send", lambda target: nudges.append(target) or True)
    queue.Q.register("A", mode="callback", pid=os.getpid())
    queue.Q.grant_next(notify_block=False)
    _wait_for(lambda: False, timeout=0.3)
    assert nudges == []


def test_bump_wakes_a_callback_head_once(queue):
    # Important 3: `conch bump` calls grant_next() (blocking) and then pings itself.
    queue.Q.register("A", mode="callback", pid=os.getpid())
    queue.Q.grant_next()
    assert queue.wakes == []
    queue.Q.clear_grant()
    queue.Q.grant_next(notify_block=False)
    assert _wait_for(lambda: len(queue.wakes) == 1)


def test_a_kept_callback_grant_is_restamped_and_pinged_on_release(queue):
    # Important 4: C (not queued) passes A's absent grant and releases: A is woken again,
    # and its grace runs from this release.
    queue.Q.register("A", mode="callback", pid=os.getpid())
    queue.Q.grant_next(notify_block=False)
    assert _wait_for(lambda: len(queue.wakes) == 1)
    _age_grant(queue, 11)
    c = queue.Conch(agent_name="c", session_id="C")
    assert c.try_acquire() is True
    c.release()
    assert _wait_for(lambda: len(queue.wakes) == 2)
    g = json.loads(queue.grant_file().read_text())
    assert g["session_id"] == "A"
    assert cw._age_s(g, None) < 3


def test_a_give_counts_from_the_release(queue):
    # Important 5: a give made while H speaks for 35 s holds through the speech (W polling
    # does not move it) and for 30 s after H releases.
    h = queue.Conch(agent_name="h", session_id="H")
    assert h.try_acquire() is True
    queue.Q.register("A", mode="callback", pid=os.getpid())
    queue.Q.register("W", mode="wait", pid=os.getpid())
    assert queue.Q.grant("A") is True
    _age_grant(queue, 35)                     # H has spoken for 35 s since the give
    assert queue.Q.granted_to() == "A"        # W's poll: nothing moves while the mic is held
    h.release()                               # the clock starts now
    assert queue.Q.granted_to() == "A"
    _age_grant(queue, 31)
    assert queue.Q.granted_to() == "W"


def test_a_give_that_has_yielded_is_an_ordinary_grant(queue):
    # Live 2026-11-11 19:10: a give survived every later re-stamp, so each cycle held
    # present callers off for the 30 s TTL instead of the 10 s grace.
    queue.Q.register("A", mode="callback", pid=os.getpid())
    assert queue.Q.grant("A") is True
    _age_grant(queue, 31)                     # the give has run out
    c = queue.Conch(agent_name="c", session_id="C")
    assert c.try_acquire() is True            # a present caller passes it
    c.release()                               # the grant comes back to A, re-stamped
    g = json.loads(queue.grant_file().read_text())
    assert g["session_id"] == "A"
    assert "given" not in g
    _age_grant(queue, 11)                     # past the grace, well short of the TTL
    d = queue.Conch(agent_name="d", session_id="D")
    assert d.try_acquire() is True
    d.release()


def test_a_wait_waiter_taking_an_absent_grant_wakes_nobody(queue):
    # Live 2026-11-11 19:11:32: A was pinged when W took its grant and again 24 ms later at
    # W's release. Only the second is A's turn.
    queue.Q.register("A", mode="callback", pid=os.getpid())
    queue.Q.grant_next(notify_block=False)
    assert _wait_for(lambda: len(queue.wakes) == 1)
    _age_grant(queue, 11)
    queue.Q.register("W", mode="wait", pid=os.getpid())
    assert queue.Q.granted_to() == "W"
    assert not _wait_for(lambda: len(queue.wakes) > 1, timeout=0.5)
    w = queue.Conch(agent_name="w", session_id="W")
    assert w.try_acquire() is True
    w.release()                               # now it is A's turn
    assert _wait_for(lambda: len(queue.wakes) == 2)


def test_a_skipped_callback_waiter_is_not_told_its_turn_has_come(queue):
    # Upstream pings callback waiters it skips for a wait waiter; through our wakers that
    # ping says "your turn has come", which it has not.
    h = queue.Conch(agent_name="h", session_id="H")
    assert h.try_acquire() is True
    queue.Q.register("A", mode="callback", pid=os.getpid())
    queue.Q.register("W", mode="wait", pid=os.getpid())
    h.release()
    assert queue.Q.granted_to() == "W"
    assert not _wait_for(lambda: len(queue.wakes) > 0, timeout=0.5)


def test_a_wait_poll_while_the_mic_is_held_keeps_the_give(queue):
    # Final review 2026-11-11 (Important 1): W's poll during H's long turn stripped the give,
    # so after H's release it held 10 s instead of 30 s.
    h = queue.Conch(agent_name="h", session_id="H")
    assert h.try_acquire() is True
    queue.Q.register("A", mode="callback", pid=os.getpid())
    queue.Q.register("W", mode="wait", pid=os.getpid())
    assert queue.Q.grant("A") is True
    _age_grant(queue, 35)                    # H has spoken 35 s since the give
    w = queue.Conch(agent_name="w", session_id="W")
    assert w.try_acquire() is False          # W's poll while H speaks
    assert json.loads(queue.grant_file().read_text()).get("given") is True
    h.release()
    _age_grant(queue, 11)                    # past the grace, short of the give TTL
    assert queue.Q.granted_to() == "A"


# ---- v1.1.2: take_wake minors from the v1.1.1 final review ----

def test_concurrent_wakes_never_exceed_the_cap(policy_env, tmp_path, monkeypatch):
    # The read-modify-write had no lock: concurrent pings could let the cap run over.
    real_dump = cw.json.dump

    def slow_dump(*a, **k):
        import time as _t
        _t.sleep(0.005)                       # widen the race window
        return real_dump(*a, **k)
    monkeypatch.setattr(cw.json, "dump", slow_dump)
    results = []
    threads = [threading.Thread(target=lambda: results.append(cw.take_wake(tmp_path, "A", 1)))
               for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 5


def test_old_wake_counts_are_pruned(policy_env, tmp_path):
    # conch.wakes.json kept one key per session id forever.
    old = {"S%d" % i: {"seq": 1, "count": 1, "at": 0} for i in range(50)}
    old["legacy"] = {"seq": 1, "count": 2}                # written before "at" existed
    (tmp_path / cw.WAKES_FILE).write_text(json.dumps(old))
    assert cw.take_wake(tmp_path, "A", 1) is True
    data = json.loads((tmp_path / cw.WAKES_FILE).read_text())
    assert set(data) == {"A"}
    assert data["A"]["count"] == 1 and data["A"]["at"] > 0


def test_a_recent_count_survives_pruning(policy_env, tmp_path):
    for _ in range(3):
        cw.take_wake(tmp_path, "B", 7)
    cw.take_wake(tmp_path, "A", 1)
    assert json.loads((tmp_path / cw.WAKES_FILE).read_text())["B"]["count"] == 3


def test_a_replace_refused_once_is_retried(policy_env, tmp_path, monkeypatch):
    # Windows: os.replace fails while another process has the file open; the count was lost.
    real, calls = os.replace, []

    def flaky(src, dst):
        calls.append(1)
        if len(calls) == 1:
            raise PermissionError(13, "Access is denied")
        return real(src, dst)
    monkeypatch.setattr(cw.os, "replace", flaky)
    cw.take_wake(tmp_path, "A", 1)
    assert json.loads((tmp_path / cw.WAKES_FILE).read_text())["A"]["count"] == 1
    assert not list(tmp_path.glob(".wakes-*"))           # no temp file left behind
