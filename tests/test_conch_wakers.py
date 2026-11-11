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
    assert cw.deliver(Entry(pid=321, session_id="chat-1")) is False


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
    assert cw.deliver(e, tmp_path) is True
    assert cw.deliver(e, tmp_path) is False
    assert calls == [77]


def test_a_busy_command_does_not_fall_through(clean, tmp_path):
    command, _ = _recording_command(tmp_path, code=cw.EXIT_BUSY)
    clean.setenv(cw.COMMAND_SETTING, json.dumps(command))
    clean.setattr(cw, "claude_pid", lambda pid: 77)
    calls = []
    clean.setattr(sw, "wake", lambda pid, reason: calls.append(pid) or (True, "sent"))
    assert cw.deliver(Entry(pid=321, session_id="A", seq=4)) is False
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


def _age_grant(q, seconds):
    g = json.loads(q.grant_file().read_text())
    from datetime import datetime as _dt, timedelta as _td
    g["granted_at"] = (_dt.now() - _td(seconds=seconds)).isoformat()
    q.grant_file().write_text(json.dumps(g))


def test_a_lone_callback_grantee_is_pinged_on_grant(queue):
    queue.Q.register("A", mode="callback", pid=os.getpid())
    assert queue.Q.grant_next().session_id == "A"
    assert queue.wakes == [(77, "conch")]


def test_a_late_wait_waiter_passes_an_absent_callback_grant(queue):
    queue.Q.register("A", mode="callback", pid=os.getpid())
    queue.Q.grant_next()
    seq_a = [e.seq for e in queue.Q.list() if e.session_id == "A"][0]
    _age_grant(queue, 11)
    queue.Q.register("B", mode="wait", pid=os.getpid())
    assert queue.Q.granted_to() == "B"
    assert [e.seq for e in queue.Q.list() if e.session_id == "A"] == [seq_a]
    import time as _time
    deadline = _time.monotonic() + 3      # the re-ping runs on upstream's daemon thread (block=False)
    while len(queue.wakes) < 2 and _time.monotonic() < deadline:
        _time.sleep(0.01)
    assert len(queue.wakes) == 2          # A pinged again


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
        queue.Q.grant_next()
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
