"""ptt-listener-windows.ps1's native mode against real Windows named pipes. Windows only.

Run by tests/run_windows_transport_tests.sh. A fake control server built from the
transport's own server code (patches/control_transport.py) listens on a real pipe;
the listener's send function is called through -SendAction, the same function the
keys call, and hands each action to its sender (patches/ptt_control_client.py
--stdin, this Python). No key is pressed or synthesised, no window is touched, no
audio. The relay-mode check uses a TCP server on an ephemeral loopback port.
"""
import importlib.util
import os
import shutil
import socket
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="real named pipes need Windows")

REPO = Path(__file__).resolve().parent.parent
PS1 = REPO / "ptt-listener-windows.ps1"
MODULE = REPO / "patches" / "control_transport.py"
spec = importlib.util.spec_from_file_location("vml_ct_ptt_windows", MODULE)
ct = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ct)

LOW = 0x1000
CLIENT = REPO / "patches" / "ptt_control_client.py"


def _listener(*args, timeout=60, env=None):
    return subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(PS1), *args],
                          capture_output=True, text=True, timeout=timeout, env=env)


def _native(sock, log, *actions, sender=None):
    """The listener in native mode, its sender pointed at sock through upstream's variable."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("VOICEMODE_")}
    env["VOICEMODE_CONTROL_SOCKET"] = str(sock)
    return _listener("-SenderExe", sys.executable,
                     "-SenderArgs", subprocess.list2cmdline([str(sender or CLIENT), "--stdin"]),
                     "-LogPath", str(log), "-SendAction", ",".join(actions), env=env)


def _log(path):
    return path.read_text(encoding="utf-8-sig") if path.exists() else ""


class FakeControl:
    """voice-mode's control server, reduced to: accept, read one line per client, record it."""

    def __init__(self, sock):
        self.server = ct.create_server(sock)
        self.server.settimeout(0.3)
        self.lines, self.stopping = [], False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stopping:
            try:
                conn, _ = self.server.accept()
            except (socket.timeout, OSError):
                continue
            with conn:
                conn.settimeout(2)
                data = b""
                try:
                    while b"\n" not in data:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                except OSError:
                    pass
                self.lines.append(data)

    def wait_for(self, count, seconds=10):
        deadline = time.monotonic() + seconds
        while len(self.lines) < count and time.monotonic() < deadline:
            time.sleep(0.05)
        return self.lines

    def close(self):
        self.stopping = True
        self.server.close()
        self.thread.join(5)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def test_the_listener_sends_ptt_control_clients_commands_to_the_pipe(tmp_path):
    sock, log = tmp_path / "control.sock", tmp_path / "ptt.log"
    with FakeControl(sock) as server:
        done = _native(sock, log, "short_press", "hold_start", "press", "hold_release")
        assert done.returncode == 0, done.stdout + done.stderr
        lines = server.wait_for(3)
        time.sleep(0.5)  # press must not arrive late either
    assert server.lines == [b'{"command": "stop", "hint": "switch-to-text"}\n', b'{"command": "hold_start"}\n',
                            b'{"command": "hold_end"}\n'], (lines, _log(log))
    text = _log(log)
    assert text.count("sent=True") == 3 and "no control pipe" not in text, text


def test_no_pipe_is_a_no_op_logged_once(tmp_path):
    sock, log = tmp_path / "control.sock", tmp_path / "ptt.log"
    started = time.monotonic()
    done = _native(sock, log, "short_press", "hold_start", "hold_release")
    assert done.returncode == 0 and done.stderr == "", done.stdout + done.stderr
    text = _log(log)
    assert text.count("no control pipe") == 1 and text.count("sent=False") == 3, text
    assert time.monotonic() - started < 30


# A server in a second process (the Low-integrity copy of Python runs it).
SERVER = textwrap.dedent("""
    import importlib.util, socket, sys
    spec = importlib.util.spec_from_file_location("ct", sys.argv[1])
    ct = importlib.util.module_from_spec(spec); spec.loader.exec_module(ct)
    srv = ct.create_server(sys.argv[2]); srv.settimeout(20)
    print("READY", flush=True)
    data = b""
    try:
        conn, _ = srv.accept()
        conn.settimeout(3)
        while b"\\n" not in data:
            c = conn.recv(4096)
            if not c: break
            data += c
    except OSError:
        pass
    print("RECEIVED", repr(data), flush=True)
""")


@pytest.fixture()
def low_python(tmp_path):
    """A copy of python.exe labelled Low integrity (as in test_control_transport_windows.py)."""
    d = tmp_path / "lowpy"
    d.mkdir()
    for f in ("python.exe", "python3.dll", f"python{sys.version_info[0]}{sys.version_info[1]}.dll",
              "vcruntime140.dll", "vcruntime140_1.dll"):
        src = Path(sys.base_prefix) / f
        if src.exists():
            shutil.copy(src, d / f)
    exe = d / "python.exe"
    r = subprocess.run(["icacls", str(exe), "/setintegritylevel", "low"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    env = dict(os.environ, PYTHONHOME=sys.base_prefix, PYTHONDONTWRITEBYTECODE="1")
    probe = subprocess.run([str(exe), "-c", textwrap.dedent("""
        import importlib.util, sys
        spec = importlib.util.spec_from_file_location("ct", sys.argv[1])
        ct = importlib.util.module_from_spec(spec); spec.loader.exec_module(ct)
        print(ct.current_integrity_rid())
    """), str(MODULE)], capture_output=True, text=True, env=env)
    assert probe.returncode == 0, probe.stderr
    assert int(probe.stdout.strip()) == LOW, "the copy did not run at Low integrity"
    return exe, env


def test_a_low_integrity_squatter_of_the_same_user_is_refused(tmp_path, low_python):
    exe, env = low_python
    sock, log = tmp_path / "control.sock", tmp_path / "ptt.log"
    p = subprocess.Popen([str(exe), "-c", SERVER, str(MODULE), str(sock)], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, env=env)
    try:
        assert p.stdout.readline().strip() == "READY"
        done = _native(sock, log, "short_press")
        assert done.returncode == 0, done.stdout + done.stderr
    finally:
        out, _ = p.communicate(timeout=30)
    text = _log(log)
    assert "RECEIVED b''" in out, out
    assert "refused" in text and "integrity 0x1000" in text and "short_press sent=False" in text, text


# The sender as shipped, except that it believes it runs as SYSTEM: our own pipe must
# then be refused, before anything is written.
FOREIGN_SENDER = textwrap.dedent("""
    import importlib.util, sys
    spec = importlib.util.spec_from_file_location("ptt_cc", sys.argv[1])
    client = importlib.util.module_from_spec(spec); spec.loader.exec_module(client)
    client._control_transport().current_user_sid = lambda: "S-1-5-18"
    sys.exit(client.main(["--stdin"]))
""")


def test_a_pipe_owned_by_someone_else_is_refused_before_writing(tmp_path):
    sock, log, sender = tmp_path / "control.sock", tmp_path / "ptt.log", tmp_path / "foreign_sender.py"
    sender.write_text(FOREIGN_SENDER, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("VOICEMODE_")}
    env["VOICEMODE_CONTROL_SOCKET"] = str(sock)
    with FakeControl(sock) as server:
        done = _listener("-SenderExe", sys.executable,
                         "-SenderArgs", subprocess.list2cmdline([str(sender), str(CLIENT)]),
                         "-LogPath", str(log), "-SendAction", "short_press,hold_start", env=env)
        assert done.returncode == 0, done.stdout + done.stderr
        server.wait_for(1, seconds=3)
    text = _log(log)
    assert text.count("control pipe refused:") == 1 and "is not the current user S-1-5-18" in text, text
    assert "short_press sent=False" in text and "hold_start sent=False" in text
    assert all(line == b"" for line in server.lines), server.lines


def test_relay_mode_still_sends_actions_over_tcp(tmp_path):
    log = tmp_path / "ptt.log"
    received = []
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        srv.settimeout(15)
        port = srv.getsockname()[1]

        def serve():
            for _ in range(2):
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                with conn:
                    conn.settimeout(3)
                    received.append(conn.recv(4096))

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        done = _listener("-Port", str(port), "-LogPath", str(log), "-SendAction", "short_press,press")
        t.join(20)
    assert done.returncode == 0, done.stdout + done.stderr
    assert received == [b'{"action": "short_press"}\n', b'{"action": "press"}\n'], received
