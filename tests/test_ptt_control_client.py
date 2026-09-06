"""Tests for patches/ptt_control_client.py.

Runs against a REAL AF_UNIX socket speaking upstream's newline-delimited JSON
protocol, so the wire format is actually exercised rather than mocked.
"""
import importlib.util
import json
import socket
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CLIENT_PY = REPO / "patches" / "ptt_control_client.py"


@pytest.fixture(scope="module")
def client_mod():
    spec = importlib.util.spec_from_file_location("vml_ptt_client", CLIENT_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeControlServer:
    """Minimal stand-in for upstream's control socket listener."""

    def __init__(self, path: Path, status_payload=None):
        self.path = path
        self.received = []
        self.status_payload = status_payload
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(path))
        self._sock.listen(8)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        self._sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (socket.timeout, OSError):
                continue
            with conn:
                conn.settimeout(0.5)
                try:
                    data = conn.recv(8192).decode("utf-8", "replace")
                except (socket.timeout, OSError):
                    continue
                for line in data.splitlines():
                    if not line.strip():
                        continue
                    try:
                        payload = json.loads(line)
                    except ValueError:
                        continue
                    self.received.append(payload)
                    if payload.get("command") == "status" and self.status_payload is not None:
                        try:
                            conn.sendall((json.dumps(self.status_payload) + "\n").encode())
                        except OSError:
                            pass

    def commands(self):
        return [p.get("command") for p in self.received]

    def close(self):
        self._stop.set()
        self._thread.join(timeout=1)
        self._sock.close()


@pytest.fixture
def server(tmp_path, monkeypatch, client_mod):
    def _make(status_payload=None):
        path = tmp_path / "control.sock"
        srv = FakeControlServer(path, status_payload)
        monkeypatch.setenv("VOICEMODE_CONTROL_SOCKET_PATH", str(path))
        return srv
    made = []
    def factory(status_payload=None):
        s = _make(status_payload); made.append(s); return s
    yield factory
    for s in made:
        s.close()


# --- transport --------------------------------------------------------------

def test_send_writes_newline_delimited_json(client_mod, server):
    srv = server()
    assert client_mod.send("hold_start") is True
    _wait(lambda: srv.received)
    assert srv.received[0] == {"command": "hold_start"}


def test_available_is_true_when_bound(client_mod, server):
    server()
    assert client_mod.available() is True


def test_available_is_false_with_no_socket(client_mod, monkeypatch, tmp_path):
    monkeypatch.setenv("VOICEMODE_CONTROL_SOCKET_PATH", str(tmp_path / "nope.sock"))
    assert client_mod.available() is False


def test_send_never_raises_without_a_listener(client_mod, monkeypatch, tmp_path):
    """A dead control channel must make PTT inert, not crash the key listener."""
    monkeypatch.setenv("VOICEMODE_CONTROL_SOCKET_PATH", str(tmp_path / "nope.sock"))
    assert client_mod.send("hold_start") is True  # fire-and-forget
    client_mod.on_action("hold_start")            # must not raise
    client_mod.on_action("hold_release")


def test_empty_command_is_refused(client_mod, server):
    server()
    assert client_mod.send("") is False


# --- action mapping ---------------------------------------------------------

def test_press_sends_nothing(client_mod, server):
    """PRESS fires on every key-down, before short/hold is known."""
    srv = server()
    client_mod.on_action("press")
    assert srv.commands() == []


def test_short_press_maps_to_skip_forward(client_mod, server):
    srv = server()
    client_mod.on_action("short_press")
    _wait(lambda: srv.received)
    assert srv.commands() == ["skip_forward"]


def test_hold_release_maps_to_hold_end(client_mod, server):
    srv = server()
    client_mod.on_action("hold_release")
    _wait(lambda: srv.received)
    assert srv.commands() == ["hold_end"]


def test_hold_start_sends_exactly_one_command(client_mod, server):
    """No status query, no skip_forward -- the hold does the barge-in itself.

    A client cannot tell whether playback is live: upstream's status reports
    now_playing as the previous COMPLETED utterance, and state is "running"
    whether speaking or listening. So the old query-then-maybe-skip_forward
    sequence was a guess, and every live test mis-timed because of it.
    patch_hold_barges_in makes the playback loops abort on is_holding instead.
    """
    srv = server()
    client_mod.on_action("hold_start")
    _wait(lambda: srv.received)
    assert srv.commands() == ["hold_start"]


def test_hold_start_never_sends_skip_forward(client_mod, server):
    """Unconditional skip_forward is actively harmful with nothing playing:
    it latches STATE_SKIP_FORWARD, which the recording loop reads as
    'end this turn now'."""
    srv = server(status_payload={"state": "running", "now_playing": {"id": 3}})
    client_mod.on_action("hold_start")
    _wait(lambda: srv.received)
    assert "skip_forward" not in srv.commands()


def test_client_no_longer_queries_status(client_mod, server):
    """The status query was load-bearing for a decision it could not inform."""
    srv = server(status_payload={"state": "running"})
    client_mod.on_action("hold_start")
    client_mod.on_action("hold_release")
    _wait(lambda: len(srv.received) >= 2)
    assert "status" not in srv.commands()
    assert not hasattr(client_mod, "is_playing"), \
        "is_playing() cannot work against upstream's status payload; it should be gone"


def _wait(pred, timeout=2.0):
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for the server to receive a command")
