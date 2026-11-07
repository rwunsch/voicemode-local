"""Tests for patches/session_wake.py: waking an idle Claude Code session.

Everything is fake: a registry and key file in a temp CLAUDE_CONFIG_DIR, and a
unix socket server in a short temp dir that records what it receives. Nothing
here reaches a real Claude Code session (conftest also points CLAUDE_CONFIG_DIR
at a temp dir for every test). The registry pid is this test process's own pid,
so the process-start check sees a real, live process.
"""
import importlib.util
import json
import logging
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

PATCHES = Path(__file__).resolve().parent.parent / "patches"
TOKEN = "tok-SECRET-0123456789abcdef"


def _load():
    spec = importlib.util.spec_from_file_location("vml_session_wake", PATCHES / "session_wake.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def sw():
    return _load()


class FakePeer:
    """Stands in for a Claude Code messaging socket: records every byte received."""

    def __init__(self, path: Path):
        self.path, self.data = path, b""
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(str(path))
        os.chmod(path, 0o600)
        self._srv.listen(4)
        self._done = threading.Event()
        self._t = threading.Thread(target=self._serve, daemon=True)
        self._t.start()

    def _serve(self):
        try:
            conn, _ = self._srv.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(2)
            while True:
                try:
                    chunk = conn.recv(4096)
                except OSError:
                    break
                if not chunk:
                    break
                self.data += chunk
        self._done.set()

    def lines(self, timeout=3.0):
        self._done.wait(timeout)
        return self.data.decode("utf-8").split("\n")

    def close(self):
        self._srv.close()


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A fake claude dir with one idle session (this process) and a listening socket."""
    cdir = tmp_path / "claude"
    (cdir / "sessions").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cdir))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("VOICEMODE_SESSION_WAKE", raising=False)
    sockdir = Path(tempfile.mkdtemp(prefix="sw-"))
    os.chmod(sockdir, 0o700)
    peer = FakePeer(sockdir / "p.sock")

    class W:
        pass
    w = W()
    w.cdir, w.peer, w.pid, w.home = cdir, peer, os.getpid(), tmp_path / "home"

    def write(status="idle", proto=1, sock=str(peer.path), proc_start="auto", key=True,
              key_mode=0o600, key_start="same"):
        st = _load()._pill_state()
        start = st.process_start(w.pid) if proc_start == "auto" else proc_start
        reg = {"pid": w.pid, "status": status, "peerProtocol": proto, "procStart": start,
               "version": "2.1.319", "messagingSocketPath": sock, "name": "my-session",
               "cwd": "/work/demo"}
        (cdir / "sessions" / f"{w.pid}.json").write_text(json.dumps(reg))
        for old in (cdir / "sessions").glob(f"{w.pid}.*.key"):
            old.unlink()
        if key:
            kp = cdir / "sessions" / f"{w.pid}.abc123.key"
            kp.write_text(json.dumps({"peerToken": TOKEN,
                                      "procStart": start if key_start == "same" else key_start}))
            os.chmod(kp, key_mode)
    w.write = write
    write()
    yield w
    peer.close()
    shutil.rmtree(sockdir, ignore_errors=True)


def _log_text(w):
    p = w.home / ".voicemode" / "logs" / "session-wake.log"
    return p.read_text(encoding="utf-8") if p.exists() else ""


# -- the happy path: exactly two lines, token never logged --------------------

@pytest.mark.parametrize("reason", ["ptt", "play"])
def test_wake_sends_exactly_auth_then_user(sw, world, reason, caplog):
    caplog.set_level(logging.DEBUG)
    ok, detail = sw.wake(world.pid, reason)
    assert ok, detail
    lines = world.peer.lines()
    assert lines[-1] == "", "every line ends with a newline"
    assert [json.loads(x) for x in lines[:-1]] == [
        {"type": "auth", "token": TOKEN},
        {"type": "user", "message": {"role": "user", "content": sw.MESSAGES[reason]}},
    ]
    assert TOKEN not in detail
    assert TOKEN not in caplog.text
    log = _log_text(world)
    assert "ok=True" in log and reason in log and TOKEN not in log


def test_messages_are_fixed_and_short(sw):
    assert set(sw.MESSAGES) == {"ptt", "play"}
    assert "skip_tts=true" in sw.MESSAGES["ptt"] and "wait_for_response=true" in sw.MESSAGES["ptt"]
    assert "greeting" in sw.MESSAGES["play"]
    # converse refuses a call without `message` (voice-mode 8.12; seen live 2026-11-07)
    assert 'message="Listening."' in sw.MESSAGES["ptt"]
    assert "as the message" in sw.MESSAGES["play"]
    for text in sw.MESSAGES.values():
        assert len(text.encode("utf-8")) <= sw.MAX_MESSAGE_BYTES
        assert "\n" not in text


def test_no_caller_supplied_text(sw):
    import inspect
    params = set(inspect.signature(sw.wake).parameters)
    assert not params & {"text", "message", "content"}


def test_unknown_reason_is_refused(sw, world):
    ok, detail = sw.wake(world.pid, "hello")
    assert not ok and "reason" in detail
    assert world.peer.data == b""


# -- refusals: nothing is sent -------------------------------------------------

@pytest.mark.parametrize("status,word", [("busy", "busy"), ("waiting", "permission"),
                                         ("shell", "shell"), (None, "status")])
def test_refuses_unless_idle(sw, world, status, word):
    world.write(status=status)
    ok, detail = sw.wake(world.pid, "ptt")
    assert not ok and word in detail
    time.sleep(0.1)
    assert world.peer.data == b""


def test_refuses_missing_key(sw, world):
    world.write(key=False)
    ok, detail = sw.wake(world.pid, "ptt")
    assert not ok and "key" in detail
    assert world.peer.data == b""


def test_refuses_key_readable_by_others(sw, world):
    world.write(key_mode=0o644)
    ok, detail = sw.wake(world.pid, "ptt")
    assert not ok and "key" in detail
    assert world.peer.data == b""


def test_refuses_key_from_another_process_start(sw, world):
    world.write(key_start="999")
    ok, detail = sw.wake(world.pid, "ptt")
    assert not ok and "key" in detail


def test_refuses_protocol_mismatch(sw, world):
    world.write(proto=2)
    ok, detail = sw.wake(world.pid, "ptt")
    assert not ok and "protocol" in detail
    assert world.peer.data == b""


def test_refuses_stale_registry_entry(sw, world):
    world.write(proc_start="1")
    ok, detail = sw.wake(world.pid, "ptt")
    assert not ok and "stale" in detail
    assert world.peer.data == b""


def test_refuses_missing_registry(sw, world):
    ok, detail = sw.wake(world.pid + 999999, "ptt")
    assert not ok and "registry" in detail


def test_refuses_socket_with_loose_mode(sw, world):
    os.chmod(world.peer.path, 0o666)
    ok, detail = sw.wake(world.pid, "ptt")
    assert not ok and "socket" in detail
    assert world.peer.data == b""


def test_refuses_socket_owned_by_someone_else(sw, world, monkeypatch):
    real = os.getuid()
    monkeypatch.setattr(sw.os, "getuid", lambda: real + 1)
    ok, detail = sw.wake(world.pid, "ptt")
    assert not ok and ("owner" in detail or "owned" in detail)
    assert world.peer.data == b""


def test_refuses_a_path_that_is_not_a_socket(sw, world, tmp_path):
    f = tmp_path / "plain"
    f.write_text("")
    os.chmod(f, 0o600)
    world.write(sock=str(f))
    ok, detail = sw.wake(world.pid, "ptt")
    assert not ok and "socket" in detail


def test_refuses_missing_socket(sw, world, tmp_path):
    world.write(sock=str(tmp_path / "gone.sock"))
    ok, detail = sw.wake(world.pid, "ptt")
    assert not ok and "socket" in detail


@pytest.mark.parametrize("value", ["off", "false", "0", "OFF"])
def test_setting_off_refuses(sw, world, monkeypatch, value):
    monkeypatch.setenv("VOICEMODE_SESSION_WAKE", value)
    assert sw.enabled() is False
    ok, detail = sw.wake(world.pid, "ptt")
    assert not ok and "VOICEMODE_SESSION_WAKE" in detail
    assert world.peer.data == b""


def test_setting_from_voicemode_env_file(sw, world):
    d = world.home / ".voicemode"
    d.mkdir(parents=True, exist_ok=True)
    (d / "voicemode.env").write_text("VOICEMODE_SESSION_WAKE=off\n")
    assert sw.enabled() is False


def test_setting_defaults_on(sw, world):
    assert sw.enabled() is True


# -- rate limit ----------------------------------------------------------------

def test_rate_limited_once_per_five_seconds(sw, world, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(sw, "_now", lambda: clock[0])
    assert sw.wake(world.pid, "ptt")[0]
    world.peer.lines()
    world.peer.close()
    world.peer = FakePeer(world.peer.path.parent / "q.sock")
    world.write(sock=str(world.peer.path))
    clock[0] += 4.9
    ok, detail = sw.wake(world.pid, "ptt")
    assert not ok and "rate" in detail
    clock[0] += 0.2
    assert sw.wake(world.pid, "ptt")[0]


def test_a_refusal_does_not_start_the_rate_limit(sw, world, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(sw, "_now", lambda: clock[0])
    world.write(status="busy")
    assert not sw.wake(world.pid, "ptt")[0]
    world.write(status="idle")
    assert sw.wake(world.pid, "ptt")[0]


# -- never raises ------------------------------------------------------------

def test_wake_never_raises(sw, world, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kaput " + TOKEN)
    monkeypatch.setattr(sw, "_send_unix", boom)
    ok, detail = sw.wake(world.pid, "ptt")
    assert not ok and TOKEN not in detail and TOKEN not in _log_text(world)


# -- finding the last voice session -----------------------------------------

def _events(home, pids):
    d = home / ".voicemode" / "logs" / "events"
    d.mkdir(parents=True, exist_ok=True)
    rows = [json.dumps({"event_type": "CONCH_ACQUIRE", "data": {"pid": p}}) for p in pids]
    (d / "voicemode_events_2026-11-07.jsonl").write_text("\n".join(rows) + "\n")


def test_conch_holder_pids_newest_first(sw, world):
    _events(world.home, [11, 22, 11, 33])
    assert sw.conch_holder_pids() == [33, 11, 22]


def test_last_voice_session_maps_server_pid_to_claude_pid(sw, world, monkeypatch, tmp_path):
    # voice server 777 (alive) -> parent 555 -> parent = this process (the "claude" pid)
    _events(world.home, [888, 777])
    parents = {777: 555, 555: world.pid, 888: 1}
    monkeypatch.setattr(sw._pill_state(), "pid_exists", lambda pid: pid in (777, 888))
    t = sw.last_voice_session(parent=parents.get, sessions=[{"pid": 777, "label": "demo"}])
    assert (t.voice_pid, t.claude_pid, t.label) == (777, world.pid, "demo")


def test_last_voice_session_label_falls_back_to_registry_name(sw, world, monkeypatch):
    _events(world.home, [777])
    monkeypatch.setattr(sw._pill_state(), "pid_exists", lambda pid: True)
    t = sw.last_voice_session(parent={777: world.pid}.get, sessions=[])
    assert t.label == "my-session"


def test_last_voice_session_none_without_a_mapping(sw, world, monkeypatch):
    _events(world.home, [777])
    monkeypatch.setattr(sw._pill_state(), "pid_exists", lambda pid: True)
    assert sw.last_voice_session(parent=lambda pid: None, sessions=[]) is None


def test_wake_without_target_uses_the_last_voice_session(sw, world, monkeypatch):
    monkeypatch.setattr(sw, "last_voice_session",
                        lambda **kw: sw.Target(voice_pid=1, claude_pid=world.pid, label="demo"))
    ok, detail = sw.wake(reason="ptt")
    assert ok, detail
    assert "demo" in _log_text(world)


def test_wake_without_any_session(sw, world, monkeypatch):
    monkeypatch.setattr(sw, "last_voice_session", lambda **kw: None)
    ok, detail = sw.wake(reason="ptt")
    assert not ok and "no voice session" in detail


def test_parent_pid_of_this_process(sw):
    if not sys.platform.startswith("linux"):
        pytest.skip("reads /proc")
    assert sw.parent_pid(os.getpid()) == os.getppid()


# -- Windows named pipe (UNVERIFIED on a real Windows Claude Code) ------------

class FakePipeConn:
    def __init__(self):
        self.data = b""
        self.closed = False

    def settimeout(self, t):
        pass

    def sendall(self, b):
        self.data += b

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def test_pipe_path_goes_through_the_owner_checked_pipe_connector(sw, world, monkeypatch):
    conn = FakePipeConn()
    seen = []
    monkeypatch.setattr(sw, "_connect_pipe", lambda name, timeout: seen.append(name) or conn)
    monkeypatch.setattr(sw, "_check_proc_start", lambda pid, reg: None)  # Windows: no /proc
    world.write(sock=r"\\.\pipe\claude-peer-1234")
    ok, detail = sw.wake(world.pid, "ptt")
    assert ok, detail
    assert seen == [r"\\.\pipe\claude-peer-1234"]
    lines = conn.data.decode().split("\n")
    assert [json.loads(x)["type"] for x in lines[:-1]] == ["auth", "user"]
    assert conn.closed


def test_pipe_refusal_sends_nothing(sw, world, monkeypatch):
    def refuse(name, timeout):
        raise PermissionError(5, "pipe owner is not the current user")
    monkeypatch.setattr(sw, "_connect_pipe", refuse)
    world.write(sock=r"\\.\pipe\claude-peer-1234")
    ok, detail = sw.wake(world.pid, "ptt")
    assert not ok and "refused" in detail


def test_is_pipe(sw):
    assert sw.is_pipe(r"\\.\pipe\x") and sw.is_pipe("//./pipe/x")
    assert not sw.is_pipe("/tmp/cc-socks-1000/1.sock")
