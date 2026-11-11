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
        monkeypatch.delenv("VOICEMODE_CONTROL_SOCKET", raising=False)  # takes precedence
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
    monkeypatch.delenv("VOICEMODE_CONTROL_SOCKET", raising=False)
    monkeypatch.setenv("VOICEMODE_CONTROL_SOCKET_PATH", str(tmp_path / "nope.sock"))
    assert client_mod.available() is False


def test_send_never_raises_without_a_listener(client_mod, monkeypatch, tmp_path):
    """A dead control channel must make PTT inert, not crash the key listener."""
    monkeypatch.delenv("VOICEMODE_CONTROL_SOCKET", raising=False)
    monkeypatch.setenv("VOICEMODE_CONTROL_SOCKET_PATH", str(tmp_path / "nope.sock"))
    assert client_mod.send("hold_start") is True  # fire-and-forget
    client_mod.on_action("hold_start")            # must not raise
    client_mod.on_action("hold_release")


def test_empty_command_is_refused(client_mod, server):
    server()
    assert client_mod.send("") is False


# --- action mapping ---------------------------------------------------------

def test_press_sends_nothing(client_mod, server):
    """press is the relay's reachability probe; it must never act."""
    srv = server()
    client_mod.on_action("press")
    assert srv.commands() == []


def test_short_press_is_a_full_stop_back_to_text(client_mod, server):
    """A tap (released within ptt_core.TAP_MAX_S) stops the whole voice turn, and the
    server-owned switch-to-text sentence tells the agent to carry on in text."""
    srv = server()
    client_mod.on_action("short_press")
    _wait(lambda: srv.received)
    assert srv.received == [{"command": "stop", "hint": "switch-to-text"}]


def test_a_tap_never_sends_skip_forward(client_mod, server):
    srv = server()
    for action in ("hold_start", "short_press"):
        client_mod.on_action(action)
    _wait(lambda: len(srv.received) >= 2)
    assert srv.commands() == ["hold_start", "stop"]


def test_down_then_up_maps_through_ptt_core(client_mod, server):
    """The whole rule, key times to control commands: 0.5 s is a stop, 1.2 s is talk."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("vml_ptt_core", REPO / "patches" / "ptt_core.py")
    core = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(core)

    def commands(held_for):
        state = core.PTTKeyState()
        return [client_mod.COMMANDS[a.value] for a in (state.on_press(100.0), state.on_release(100.0 + held_for))]

    assert commands(0.5) == ["hold_start", "stop"]
    assert commands(1.2) == ["hold_start", "hold_end"]
    assert commands(0.8) == ["hold_start", "hold_end"]


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


# --- outcomes and the stdin sender (native Windows listener, task 7) ----------

def test_action_outcome_says_what_happened(client_mod, server, monkeypatch, tmp_path):
    monkeypatch.delenv("VOICEMODE_CONTROL_SOCKET", raising=False)
    monkeypatch.setenv("VOICEMODE_CONTROL_SOCKET_PATH", str(tmp_path / "nope.sock"))
    assert client_mod.action_outcome("short_press") == "no-channel"
    assert client_mod.action_outcome("press") == "none"
    assert client_mod.action_outcome("bogus") == "failed: unknown action 'bogus'"  # visible, not silent
    srv = server()
    assert [client_mod.action_outcome(a) for a in ("short_press", "hold_start", "press", "hold_release")] == [
        "sent", "sent", "none", "sent"]
    _wait(lambda: len(srv.received) >= 3)
    assert srv.commands() == ["stop", "hold_start", "hold_end"]


def test_action_outcome_reports_a_refusal_on_one_line(client_mod, monkeypatch):
    def refuse(path, timeout):
        raise PermissionError(5, "control pipe owner S-1-5-18 is not\nthe current user", "pipe")

    monkeypatch.setattr(client_mod, "_connect", refuse)
    outcome = client_mod.action_outcome("hold_start")
    assert outcome.startswith("refused: ") and "S-1-5-18" in outcome and "\n" not in outcome


@pytest.mark.parametrize("error,word", [(TimeoutError("timed out"), "busy"), (ConnectionRefusedError(), "no-channel"),
                                        (OSError(22, "bad"), "failed"), (RuntimeError("odd"), "failed")])
def test_action_outcome_never_raises(client_mod, monkeypatch, error, word):
    def fail(path, timeout):
        raise error

    monkeypatch.setattr(client_mod, "_connect", fail)
    assert client_mod.action_outcome("short_press").split(":")[0] == word


def test_on_action_uses_the_same_table(client_mod):
    assert client_mod.COMMANDS == {"short_press": "stop", "hold_start": "hold_start",
                                   "hold_release": "hold_end"}


def test_warm_loads_what_the_first_press_needs(client_mod, monkeypatch):
    """The Windows listener sends `warm` once at start, so the first real press does not
    pay for loading the path resolver and the pipe transport (0.97 s measured, 2026-11-07)."""
    import io
    import socket as socket_mod
    monkeypatch.setattr(client_mod, "_STATE", None)
    monkeypatch.setattr(client_mod, "_TRANSPORT", None)
    # An earlier test may have imported the installed voice_mode: the client would use its
    # modules and never load the files beside it. Failed on 962589c in a full run too.
    import sys as sys_mod
    for name in ("voice_mode.voice_pill_state", "voice_mode.control_transport"):
        monkeypatch.delitem(sys_mod.modules, name, raising=False)
    monkeypatch.delattr(socket_mod, "AF_UNIX")  # as on Windows: the pipe transport is used
    monkeypatch.setattr(client_mod, "deliver", lambda command: pytest.fail("warm must send nothing"))
    out = io.StringIO()
    client_mod.serve_lines(io.StringIO("warm\n"), out)
    assert out.getvalue() == "ready\n"
    assert client_mod._STATE is not None and client_mod._TRANSPORT is not None


def test_stdin_mode_answers_one_line_per_action(server, tmp_path):
    import subprocess
    import sys
    srv = server()
    env = {"VOICEMODE_CONTROL_SOCKET": str(srv.path), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    proc = subprocess.Popen([sys.executable, str(CLIENT_PY), "--stdin"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True, env=env)
    try:
        answers = []
        for action in ("short_press", "", "press", "hold_start\r", "hold_release"):
            proc.stdin.write(action + "\n")
            proc.stdin.flush()
            if action.strip():
                answers.append(proc.stdout.readline().strip())  # one answer per action, as it happens
        proc.stdin.close()
        assert proc.wait(10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
    assert answers == ["sent", "none", "sent", "sent"]
    _wait(lambda: len(srv.received) >= 3)
    assert srv.commands() == ["stop", "hold_start", "hold_end"]


def test_stdin_mode_ignores_a_byte_order_mark(server, tmp_path):
    """.NET Framework's redirected stdin starts with a UTF-8 BOM (seen on Windows 2026-11-07:
    the first action of every run was lost). The first action must still be sent."""
    import subprocess
    import sys
    srv = server()
    env = {"VOICEMODE_CONTROL_SOCKET": str(srv.path), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
           "PYTHONIOENCODING": "cp1252"}  # Windows' default when PYTHONUTF8 is unset
    done = subprocess.run([sys.executable, str(CLIENT_PY), "--stdin"], input=b"\xef\xbb\xbfshort_press\r\nhold_start\r\n",
                          capture_output=True, env=env, timeout=10)
    assert done.stdout.decode().split() == ["sent", "sent"], done.stdout + done.stderr
    _wait(lambda: len(srv.received) >= 3)
    assert srv.commands() == ["stop", "hold_start", "hold_end"]  # EOF closes the open hold


def test_eof_during_a_hold_sends_hold_end(client_mod, monkeypatch):
    """The listener killed mid-hold (ptt off, watchdog, kill_tree) must not leave the
    recording running to max_duration: EOF after hold_start sends hold_end."""
    import io
    sent = []
    monkeypatch.setattr(client_mod, "deliver", lambda command: sent.append(command) or "sent")
    out = io.StringIO()
    client_mod.serve_lines(io.StringIO("hold_start\n"), out)
    assert sent == ["hold_start", "hold_end"]


def test_eof_after_a_released_hold_sends_nothing_more(client_mod, monkeypatch):
    import io
    sent = []
    monkeypatch.setattr(client_mod, "deliver", lambda command: sent.append(command) or "sent")
    client_mod.serve_lines(io.StringIO("hold_start\nhold_release\n"), io.StringIO())
    assert sent == ["hold_start", "hold_end"]


def test_stdin_mode_survives_invalid_utf8(server, tmp_path):
    import subprocess
    import sys
    srv = server()
    env = {"VOICEMODE_CONTROL_SOCKET": str(srv.path), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    done = subprocess.run([sys.executable, str(CLIENT_PY), "--stdin"],
                          input=b"\xff\xfe junk\nshort_press\n", capture_output=True, env=env, timeout=10)
    lines = done.stdout.decode().splitlines()
    assert done.returncode == 0, done.stderr
    assert lines[0].startswith("failed:") and lines[1] == "sent", done.stdout + done.stderr
    _wait(lambda: len(srv.received) >= 1)
    assert srv.commands() == ["stop"]


# --- wake an idle session on a hold with no voice turn (2026-11-07) ----------

def _serve_with(client_mod, monkeypatch, outcome, text):
    import io
    events = []
    monkeypatch.setattr(client_mod, "deliver", lambda command: events.append(("deliver", command)) or outcome)
    monkeypatch.setattr(client_mod, "wake_idle_session",
                        lambda: events.append(("wake",)) or (True, "sent"))

    class Out(io.StringIO):
        def write(self, s):
            events.append(("answer", s.strip()))
            return super().write(s)
    out = Out()
    client_mod.serve_lines(io.StringIO(text), out)
    return events, out.getvalue()


def test_a_hold_with_no_channel_wakes_the_last_session_once(client_mod, monkeypatch):
    events, out = _serve_with(client_mod, monkeypatch, "no-channel", "hold_start\nhold_release\n")
    assert out == "no-channel\nno-channel\n"  # the listener's protocol is unchanged
    assert events.count(("wake",)) == 1
    # answered first: the listener waits for the answer, never for the wake
    assert events.index(("answer", "no-channel"), 2) < events.index(("wake",))


def test_a_tap_with_no_channel_does_not_wake(client_mod, monkeypatch):
    events, _ = _serve_with(client_mod, monkeypatch, "no-channel", "hold_start\nshort_press\n")
    assert ("wake",) not in events


def test_a_hold_during_a_voice_turn_does_not_wake(client_mod, monkeypatch):
    events, _ = _serve_with(client_mod, monkeypatch, "sent", "hold_start\nhold_release\n")
    assert ("wake",) not in events


def test_wake_idle_session_uses_session_wake_for_ptt(client_mod, monkeypatch):
    calls = []

    class Fake:
        @staticmethod
        def wake(target_claude_pid=None, reason="ptt"):
            calls.append(reason)
            return False, "busy"
    monkeypatch.setattr(client_mod, "_session_wake", lambda: Fake)
    assert client_mod.wake_idle_session() == (False, "busy")
    assert calls == ["ptt"]


def test_wake_idle_session_never_raises(client_mod, monkeypatch):
    def boom():
        raise ImportError("no session_wake")
    monkeypatch.setattr(client_mod, "_session_wake", boom)
    ok, detail = client_mod.wake_idle_session()
    assert not ok and "failed" in detail


def test_the_real_session_wake_module_loads_beside_the_client(client_mod):
    mod = client_mod._session_wake()
    assert set(mod.MESSAGES) == {"ptt", "wake", "conch"}


# --- an idle hold only wakes: nothing to end (2026-11-07, option 1) ----------

def test_a_hold_started_with_no_channel_sends_no_hold_end(client_mod, monkeypatch):
    events, out = _serve_with(client_mod, monkeypatch, "no-channel", "hold_start\nhold_release\n")
    assert [e for e in events if e[0] == "deliver"] == [("deliver", "hold_start")]
    assert out == "no-channel\nno-channel\n"  # the listener's protocol is unchanged
    assert events.count(("wake",)) == 1


def test_a_hold_started_with_a_channel_still_sends_hold_end(client_mod, monkeypatch):
    events, _ = _serve_with(client_mod, monkeypatch, "sent", "hold_start\nhold_release\n")
    assert [e for e in events if e[0] == "deliver"] == [("deliver", "hold_start"), ("deliver", "hold_end")]


def test_the_next_hold_is_judged_afresh(client_mod, monkeypatch):
    import io
    answers = iter(["no-channel", "sent", "sent"])
    sent = []
    monkeypatch.setattr(client_mod, "deliver", lambda c: sent.append(c) or next(answers))
    monkeypatch.setattr(client_mod, "wake_idle_session", lambda: (True, "x"))
    client_mod.serve_lines(io.StringIO("hold_start\nhold_release\nhold_start\nhold_release\n"), io.StringIO())
    assert sent == ["hold_start", "hold_start", "hold_end"]


def test_wake_idle_session_names_the_session_and_the_chime(client_mod, monkeypatch):
    class Fake:
        last_label = "demo"

        @staticmethod
        def wake(target_claude_pid=None, reason="ptt"):
            return True, "sent"
    monkeypatch.setattr(client_mod, "_session_wake", lambda: Fake)
    assert client_mod.wake_idle_session() == (True, "woke demo; talk after Listening.")
