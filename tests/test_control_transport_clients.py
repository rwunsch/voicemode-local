"""The repo's own control clients use the pipe transport when AF_UNIX is missing.

ptt_control_client.py and voice_pill_state.py talk to the control channel
directly. On Windows there is no AF_UNIX and no socket file, so they must dial
through control_transport.connect instead, and must not gate on the file
existing. A fake transport stands in for the pipe here.
"""
import importlib.util
import json
import socket
import sys
import types
from pathlib import Path

import pytest

PATCHES = Path(__file__).resolve().parent.parent / "patches"


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, PATCHES / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeConn:
    def __init__(self, reply):
        self.sent = b""
        self.reply = reply
        self.closed = False

    def settimeout(self, t):
        pass

    def sendall(self, data):
        self.sent += data

    def recv(self, n):
        out, self.reply = self.reply[:n], self.reply[n:]
        return out

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FakeTransport(types.ModuleType):
    def __init__(self, reply=b"", fail=None):
        super().__init__("control_transport")
        self.calls, self.conns, self.reply, self.fail = [], [], reply, fail

    def connect(self, path, timeout):
        self.calls.append((Path(path), timeout))
        if self.fail:
            raise self.fail
        c = FakeConn(self.reply)
        self.conns.append(c)
        return c


@pytest.fixture()
def no_af_unix(monkeypatch):
    def install(transport):
        monkeypatch.setitem(sys.modules, "control_transport", transport)
        monkeypatch.delitem(sys.modules, "voice_mode.control_transport", raising=False)
        monkeypatch.delattr(socket, "AF_UNIX", raising=False)
        return transport
    return install


# --- ptt_control_client ------------------------------------------------------

@pytest.fixture()
def ptt():
    return _load("vml_ptt_client_ct", "ptt_control_client.py")


def test_ptt_reads_upstreams_variable(ptt, monkeypatch, tmp_path):
    monkeypatch.setenv("VOICEMODE_CONTROL_SOCKET", str(tmp_path / "up.sock"))
    monkeypatch.setenv("VOICEMODE_CONTROL_SOCKET_PATH", str(tmp_path / "old.sock"))
    assert ptt.socket_path() == tmp_path / "up.sock"


def test_ptt_keeps_the_old_variable_as_fallback(ptt, monkeypatch, tmp_path):
    monkeypatch.delenv("VOICEMODE_CONTROL_SOCKET", raising=False)
    monkeypatch.setenv("VOICEMODE_CONTROL_SOCKET_PATH", str(tmp_path / "old.sock"))
    assert ptt.socket_path() == tmp_path / "old.sock"


def test_ptt_default_path(ptt, monkeypatch):
    monkeypatch.delenv("VOICEMODE_CONTROL_SOCKET", raising=False)
    monkeypatch.delenv("VOICEMODE_CONTROL_SOCKET_PATH", raising=False)
    assert ptt.socket_path() == Path("~/.voicemode/control.sock").expanduser()


def test_ptt_send_uses_the_pipe(ptt, no_af_unix, monkeypatch, tmp_path):
    t = no_af_unix(FakeTransport())
    monkeypatch.setenv("VOICEMODE_CONTROL_SOCKET", str(tmp_path / "control.sock"))
    assert ptt.send("hold_start") is True
    assert t.calls == [(tmp_path / "control.sock", ptt.CONNECT_TIMEOUT)]
    assert json.loads(t.conns[0].sent) == {"command": "hold_start"}
    assert t.conns[0].closed


def test_ptt_reply_through_the_pipe(ptt, no_af_unix, monkeypatch, tmp_path):
    no_af_unix(FakeTransport(reply=b'{"ok": true}\nrest'))
    monkeypatch.setenv("VOICEMODE_CONTROL_SOCKET", str(tmp_path / "control.sock"))
    assert ptt._send_line({"command": "status"}, read_reply=True) == '{"ok": true}'


def test_ptt_available_uses_the_pipe(ptt, no_af_unix, monkeypatch, tmp_path):
    monkeypatch.setenv("VOICEMODE_CONTROL_SOCKET", str(tmp_path / "control.sock"))
    no_af_unix(FakeTransport())
    assert ptt.available() is True


def test_ptt_never_raises_when_the_pipe_is_absent(ptt, no_af_unix, monkeypatch, tmp_path):
    monkeypatch.setenv("VOICEMODE_CONTROL_SOCKET", str(tmp_path / "control.sock"))
    no_af_unix(FakeTransport(fail=FileNotFoundError(2, "no pipe")))
    assert ptt.available() is False
    assert ptt.send("hold_start") is True
    ptt.on_action("hold_release")


# --- voice_pill_state ---------------------------------------------------------

@pytest.fixture()
def vps():
    return _load("vml_vps_ct", "voice_pill_state.py")


def test_pill_status_uses_the_pipe_without_a_socket_file(vps, no_af_unix, tmp_path):
    t = no_af_unix(FakeTransport(reply=b'{"speaking": true}\n'))
    path = tmp_path / "control.sock"
    assert not path.exists()
    assert vps._status(path) == {"speaking": True}
    assert t.calls[0][0] == path
    assert json.loads(t.conns[0].sent) == {"command": "status"}


def test_pill_send_uses_the_pipe_without_a_socket_file(vps, no_af_unix, tmp_path):
    t = no_af_unix(FakeTransport())
    assert vps.send_control(tmp_path / "control.sock", "stop") is True
    assert json.loads(t.conns[0].sent) == {"command": "stop"}


def test_pill_absent_pipe_reads_as_idle(vps, no_af_unix, tmp_path):
    no_af_unix(FakeTransport(fail=FileNotFoundError(2, "no pipe")))
    assert vps._status(tmp_path / "control.sock") == {}
    assert vps.send_control(tmp_path / "control.sock", "stop") is False


def test_pill_owner_mismatch_reads_as_idle(vps, no_af_unix, tmp_path):
    no_af_unix(FakeTransport(fail=PermissionError(5, "pipe owner is not the current user")))
    assert vps._status(tmp_path / "control.sock") == {}
    assert vps.send_control(tmp_path / "control.sock", "stop") is False


def test_pill_posix_never_touches_the_transport(vps, monkeypatch, tmp_path):
    if not hasattr(socket, "AF_UNIX"):
        pytest.skip("POSIX only")

    class Poison(types.ModuleType):
        def __getattr__(self, name):
            raise AssertionError(name)
    monkeypatch.setitem(sys.modules, "control_transport", Poison("control_transport"))
    assert vps._status(tmp_path / "missing.sock") == {}
    assert vps.send_control(tmp_path / "missing.sock", "stop") is False


def test_missing_transport_module_reads_as_idle(vps, ptt, monkeypatch, tmp_path):
    for name in ("control_transport", "voice_mode.control_transport", "voice_mode"):
        monkeypatch.setitem(sys.modules, name, None)  # every import of it fails
    monkeypatch.delattr(socket, "AF_UNIX", raising=False)
    monkeypatch.setenv("VOICEMODE_CONTROL_SOCKET", str(tmp_path / "control.sock"))
    assert vps._status(tmp_path / "control.sock") == {}
    assert vps.send_control(tmp_path / "control.sock", "stop") is False
    assert ptt.available() is False
    assert ptt.send("hold_start") is True
