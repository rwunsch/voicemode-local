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
