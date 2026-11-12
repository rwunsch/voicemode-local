"""Tests for patches/ptt_relay.py — the WSL2 Windows->control-socket hop."""
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def relay():
    spec = importlib.util.spec_from_file_location(
        "vml_ptt_relay", REPO / "patches" / "ptt_relay.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("line,expected", [
    ('{"action": "hold_start"}', "hold_start"),
    ('{"type": "hold_release"}', "hold_release"),       # old ptt_ipc field name
    ("short_press", "short_press"),                      # bare name
    ("  hold_start  \n", "hold_start"),                  # whitespace tolerated
    ('{"action": "press"}', "press"),
])
def test_valid_actions_parse(relay, line, expected):
    assert relay.parse_action(line) == expected


@pytest.mark.parametrize("line", [
    "", "   ", "\n",
    "hold_sideways",                                     # not in the allowlist
    '{"action": "skip_forward"}',                        # a control command, not a PTT action
    '{"action": "rm -rf /"}',
    "{not json",
    '["hold_start"]',                                    # not an object
    '{"action": 3}',
    "x" * 2000,                                          # over MAX_LINE
])
def test_invalid_input_is_rejected(relay, line):
    assert relay.parse_action(line) is None


def test_allowlist_matches_ptt_core(relay):
    """The relay must accept exactly the actions ptt_core can emit."""
    spec = importlib.util.spec_from_file_location(
        "vml_ptt_core", REPO / "patches" / "ptt_core.py")
    core = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(core)
    assert relay.VALID_ACTIONS == {a.value for a in core.PTTAction}


def test_binds_loopback_by_default(relay):
    """Nothing off-box drives the microphone unless explicitly opted in."""
    import inspect
    src = inspect.getsource(relay.serve)
    assert '"VOICEMODE_PTT_HOST", "127.0.0.1"' in src, \
        "the default bind must stay loopback"


# --- wake an idle session on a hold with no voice turn (2026-11-07) ----------

class FakeClient:
    def __init__(self, outcome):
        self.outcome, self.sent, self.woke = outcome, [], 0

    def action_outcome(self, action):
        self.sent.append(action)
        return self.outcome

    def wake_idle_session(self):
        self.woke += 1
        return True, "sent"


@pytest.mark.parametrize("action,outcome,woke", [
    ("hold_release", "no-channel", 1),
    ("hold_release", "sent", 0),
    ("hold_start", "no-channel", 0),
    ("short_press", "no-channel", 0),
])
def test_relay_wakes_only_on_a_hold_with_no_channel(relay, action, outcome, woke):
    client = FakeClient(outcome)
    relay.forward(action, client)
    assert client.sent == [action] and client.woke == woke


def test_relay_forward_never_raises(relay):
    class Broken(FakeClient):
        def wake_idle_session(self):
            raise RuntimeError("x")
    relay.forward("hold_release", Broken("no-channel"))


def test_relay_does_not_send_hold_end_for_a_hold_that_started_with_no_channel(relay):
    client = FakeClient("no-channel")
    relay.forward("hold_start", client)
    relay.forward("hold_release", client)
    assert client.sent == ["hold_start"] and client.woke == 1


def test_relay_still_sends_hold_end_after_a_hold_with_a_channel(relay):
    client = FakeClient("sent")
    relay.forward("hold_start", client)
    relay.forward("hold_release", client)
    assert client.sent == ["hold_start", "hold_release"] and client.woke == 0


# --- Talos review 2026-11-12: bounded lines, bounded connections, a token -----

import os
import re
import socket
import stat
import threading
import time

TOKEN = "ab" * 32


class Running:
    """A real relay on an ephemeral loopback port, forwarding to a FakeClient."""

    def __init__(self, relay, outcome="sent", token=TOKEN, max_conns=None):
        self.client = FakeClient(outcome)
        kw = {} if max_conns is None else {"max_conns": max_conns}
        self.srv = relay.make_server("127.0.0.1", 0, token=token, client=self.client, **kw)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def connect(self):
        return socket.create_connection(("127.0.0.1", self.port), timeout=5)

    def send(self, *lines):
        with self.connect() as s:
            s.sendall(b"".join(l.encode() + b"\n" for l in lines))

    def wait_sent(self, n, within=3.0):
        end = time.monotonic() + within
        while len(self.client.sent) < n and time.monotonic() < end:
            time.sleep(0.02)
        return self.client.sent

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def running(relay):
    started = []

    def start(**kw):
        r = Running(relay, **kw)
        started.append(r)
        return r
    yield start
    for r in started:
        r.close()


def closed_within(sock, seconds):
    """True if the peer closes ``sock`` within ``seconds`` (EOF or reset)."""
    sock.settimeout(seconds)
    try:
        while True:
            if sock.recv(4096) == b"":
                return True
    except (ConnectionResetError, BrokenPipeError):
        return True
    except socket.timeout:
        return False


def test_a_line_after_the_right_token_is_forwarded(running):
    r = running()
    r.send("token " + TOKEN, '{"action": "hold_start"}')
    assert r.wait_sent(1) == ["hold_start"]


@pytest.mark.parametrize("first", [None, "token " + "cd" * 32, "token", "hold_start"])
def test_a_missing_or_wrong_token_closes_and_forwards_nothing(running, first):
    r = running()
    with r.connect() as s:
        lines = ([first] if first else []) + ['{"action": "hold_start"}']
        s.sendall(b"".join(l.encode() + b"\n" for l in lines))
        assert closed_within(s, 1.0)
    time.sleep(0.2)
    assert r.client.sent == []


def test_an_over_long_line_closes_the_connection_at_once(running):
    """64 KB with no newline used to be buffered whole (300 MB grew the relay to
    306 MB). It must close at once, well before the 2 s idle timeout, and a new
    connection must still be served."""
    r = running()
    with r.connect() as s:
        s.sendall(("token " + TOKEN + "\n").encode())
        try:
            s.sendall(b"x" * 65536)
        except (ConnectionResetError, BrokenPipeError):
            pass
        assert closed_within(s, 1.0)
    r.send("token " + TOKEN, "short_press")
    assert r.wait_sent(1) == ["short_press"]


def test_an_over_long_first_line_closes_before_the_token(running):
    r = running()
    with r.connect() as s:
        try:
            s.sendall(b"x" * 65536)
        except (ConnectionResetError, BrokenPipeError):
            pass
        assert closed_within(s, 1.0)


def test_connections_beyond_the_limit_are_closed_at_once(running):
    r = running(max_conns=2)
    held = [r.connect(), r.connect()]
    try:
        time.sleep(0.2)
        with r.connect() as extra:
            assert closed_within(extra, 1.0)
    finally:
        for s in held:
            s.close()
    time.sleep(0.2)
    r.send("token " + TOKEN, "hold_start")
    assert r.wait_sent(1) == ["hold_start"]


def test_the_default_connection_limit_is_small(relay):
    assert 1 <= relay.MAX_CONNECTIONS <= 16


def test_hold_state_carries_across_connections(running):
    """The Windows listener opens one connection per action, so a hold that began
    with no voice turn must still be recognised when its release arrives on a new
    connection: it wakes, and no hold_end is sent."""
    r = running(outcome="no-channel")
    r.send("token " + TOKEN, "hold_start")
    assert r.wait_sent(1) == ["hold_start"]
    r.send("token " + TOKEN, "hold_release")
    end = time.monotonic() + 3
    while r.client.woke == 0 and time.monotonic() < end:
        time.sleep(0.02)
    assert r.client.sent == ["hold_start"] and r.client.woke == 1


def test_the_token_file_is_created_once_with_mode_600(relay, tmp_path):
    path = tmp_path / "sub" / "ptt-relay.token"
    first = relay.load_or_create_token(path)
    assert len(first) == 64 and int(first, 16) >= 0
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert path.read_text().strip() == first
    assert relay.load_or_create_token(path) == first


@pytest.mark.parametrize("damaged", ["not a token", "AB" * 32, "ab cd " + "ef" * 29])
def test_a_damaged_token_file_is_replaced(relay, tmp_path, damaged):
    """Only what the Windows listener accepts (^[0-9a-f]{64}$) is reused: upper case,
    or hex with spaces (which bytes.fromhex allows), is replaced."""
    path = tmp_path / "ptt-relay.token"
    path.write_text(damaged)
    token = relay.load_or_create_token(path)
    assert token != damaged
    assert re.fullmatch(r"[0-9a-f]{64}", token) and path.read_text().strip() == token
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_serve_uses_the_token_file_by_default(relay):
    assert str(relay.DEFAULT_TOKEN_PATH).endswith(os.path.join(".voicemode", "ptt-relay.token"))
