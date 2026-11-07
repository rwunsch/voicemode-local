"""control_transport.py against real Windows named pipes. Windows only; no audio.

Run from WSL with tests/run_windows_transport_tests.sh, which copies this file
and patches/control_transport.py into a Windows temp folder, runs it with
Windows Python, and deletes the folder. On any other OS every test skips.

The Low-integrity tests copy python.exe into a temp folder and label the copy
Low with icacls, so the process it starts runs at Low integrity as the same
user -- the squatter the client must refuse.
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

MODULE = Path(__file__).resolve().parent.parent / "patches" / "control_transport.py"
spec = importlib.util.spec_from_file_location("vml_ct_windows", MODULE)
ct = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ct)

LOW, MEDIUM = 0x1000, 0x2000

# A minimal server for a second process: bind, accept one client, answer a
# status line, report what it received, exit.
SERVER = textwrap.dedent("""
    import importlib.util, socket, sys
    spec = importlib.util.spec_from_file_location("ct", sys.argv[1])
    ct = importlib.util.module_from_spec(spec); spec.loader.exec_module(ct)
    srv = ct.create_server(sys.argv[2]); srv.settimeout(20)
    print("READY", flush=True)
    try:
        conn, _ = srv.accept()
    except socket.timeout:
        print("RECEIVED b''", flush=True); sys.exit(0)
    conn.settimeout(3); data = b""
    try:
        while b"\\n" not in data:
            c = conn.recv(4096)
            if not c: break
            data += c
    except OSError:
        pass
    if b'"status"' in data:
        conn.sendall(b'{"ok": true, "state": "running"}\\n')
    print("RECEIVED", repr(data), flush=True)
    try:
        while conn.recv(4096): pass
    except OSError:
        pass
""")

# A client for a second process: connect, send one line, report the outcome.
CLIENT = textwrap.dedent("""
    import importlib.util, sys
    spec = importlib.util.spec_from_file_location("ct", sys.argv[1])
    ct = importlib.util.module_from_spec(spec); spec.loader.exec_module(ct)
    try:
        c = ct.connect(sys.argv[2], 2.0); c.sendall(b'{"command":"stop"}\\n'); c.close()
        print("SENT", flush=True)
    except OSError as e:
        print("REFUSED", type(e).__name__, getattr(e, "winerror", None), flush=True)
""")


@pytest.fixture()
def sock(tmp_path):
    return tmp_path / "control.sock"


def _spawn(exe, code, *args, env=None):
    p = subprocess.Popen([str(exe), "-c", code, str(MODULE), *map(str, args)],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    return p


def _wait_ready(p):
    lines = []
    for line in p.stdout:
        lines.append(line)
        if line.strip() == "READY":
            return lines
    raise AssertionError("server never became ready: " + "".join(lines))


@pytest.fixture()
def low_python(tmp_path):
    """A copy of python.exe labelled Low integrity, plus the env it needs."""
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


# --- the basics ---------------------------------------------------------------

def test_this_process_is_medium_or_higher():
    assert ct.current_integrity_rid() >= MEDIUM


def test_two_process_status_exchange(sock):
    p = _spawn(sys.executable, SERVER, sock)
    _wait_ready(p)
    with ct.connect(sock, 2.0) as c:
        c.sendall(b'{"command":"status"}\n')
        buf = b""
        while b"\n" not in buf:
            chunk = c.recv(4096)
            assert chunk, "server closed before answering"
            buf += chunk
    assert buf == b'{"ok": true, "state": "running"}\n'
    out, _ = p.communicate(timeout=10)
    assert "RECEIVED b'{\"command\":\"status\"}\\n'" in out


def test_second_server_is_refused_in_process_and_across_processes(sock):
    p = _spawn(sys.executable, SERVER, sock)
    _wait_ready(p)
    try:
        with pytest.raises(PermissionError) as e:
            ct.create_server(sock)
        assert e.value.winerror == 5
    finally:
        p.kill()
        p.wait()
    with ct.create_server(sock):
        with pytest.raises(PermissionError):
            ct.create_server(sock)


def test_dacl_owner_and_label_read_back(sock):
    me = ct.current_user_sid()
    with ct.create_server(sock) as srv:
        for handle_owner in (srv, ct.connect(sock, 2.0)):
            sddl = ct.pipe_security_sddl(handle_owner)
            assert ct.sddl_is_owner_only(sddl, me), sddl
            assert ct.sddl_label_rid(sddl) == min(ct.current_integrity_rid(), MEDIUM), sddl
        handle_owner.close()


def test_no_server_is_file_not_found(sock):
    with pytest.raises(FileNotFoundError):
        ct.connect(sock, 0.5)


def test_name_is_free_again_after_close(sock):
    ct.create_server(sock).close()
    ct.create_server(sock).close()


# --- lifecycle and timeouts ----------------------------------------------------

def test_accept_times_out(sock):
    with ct.create_server(sock) as srv:
        srv.settimeout(0.2)
        t = time.monotonic()
        with pytest.raises(socket.timeout):
            srv.accept()
        assert 0.15 <= time.monotonic() - t < 1.0


def test_close_during_accept_unblocks_it(sock):
    srv = ct.create_server(sock)
    srv.settimeout(None)
    result = {}

    def run():
        try:
            srv.accept()
            result["r"] = "accepted"
        except OSError as e:
            result["r"] = type(e).__name__
    th = threading.Thread(target=run)
    th.start()
    time.sleep(0.3)
    t = time.monotonic()
    srv.close()
    th.join(3)
    assert not th.is_alive() and result["r"] == "OSError"
    assert time.monotonic() - t < 1.0


def test_recv_timeout(sock):
    with ct.create_server(sock) as srv:
        c = ct.connect(sock, 2.0)
        c.settimeout(0.3)
        t = time.monotonic()
        with pytest.raises(socket.timeout):
            c.recv(10)
        assert 0.25 <= time.monotonic() - t < 1.5
        c.close()


def test_send_timeout_when_nobody_reads(sock):
    with ct.create_server(sock) as srv:
        srv.settimeout(2)
        c = ct.connect(sock, 2.0)
        conn, _ = srv.accept()  # accepted, never read
        c.settimeout(0.3)
        t = time.monotonic()
        with pytest.raises(socket.timeout):
            c.sendall(b"x" * (4 * 1024 * 1024))
        assert time.monotonic() - t < 3.0
        c.close()
        conn.close()


def test_client_that_never_sends(sock):
    with ct.create_server(sock) as srv:
        srv.settimeout(2)
        c = ct.connect(sock, 2.0)
        conn, _ = srv.accept()
        conn.settimeout(0.3)
        with pytest.raises(socket.timeout):
            conn.recv(4096)
        c.close()
        assert conn.recv(4096) == b""
        with pytest.raises(BrokenPipeError):
            conn.sendall(b"late\n")
        conn.close()


def test_fire_and_forget_then_close_is_still_read(sock):
    with ct.create_server(sock) as srv:
        srv.settimeout(2)
        c = ct.connect(sock, 2.0)
        c.sendall(b'{"command":"stop"}')
        c.close()
        conn, _ = srv.accept()
        conn.settimeout(1)
        assert conn.recv(4096) == b'{"command":"stop"}'
        assert conn.recv(4096) == b""
        conn.close()


def test_connect_times_out_while_every_instance_is_busy(sock):
    with ct.create_server(sock):
        first = ct.connect(sock, 1.0)
        t = time.monotonic()
        with pytest.raises(socket.timeout):
            ct.connect(sock, 0.4)
        assert time.monotonic() - t < 1.5
        first.close()


# --- who may be trusted ----------------------------------------------------------

def test_owner_mismatch_is_refused_before_sending(sock, monkeypatch):
    p = _spawn(sys.executable, SERVER, sock)
    _wait_ready(p)
    monkeypatch.setattr(ct, "current_user_sid", lambda: "S-1-5-18")
    with pytest.raises(PermissionError):
        ct.connect(sock, 2.0)
    out, _ = p.communicate(timeout=30)
    assert "RECEIVED b''" in out


def test_low_integrity_squatter_is_refused(sock, low_python):
    exe, env = low_python
    p = _spawn(exe, SERVER, sock, env=env)
    _wait_ready(p)
    try:
        with pytest.raises(PermissionError):
            ct.connect(sock, 2.0)
    finally:
        out, _ = p.communicate(timeout=30)
    assert "RECEIVED b''" in out, out


def test_low_integrity_client_cannot_write_to_our_server(sock, low_python):
    exe, env = low_python
    with ct.create_server(sock) as srv:
        srv.settimeout(0.2)
        r = subprocess.run([str(exe), "-c", CLIENT, str(MODULE), str(sock)],
                           capture_output=True, text=True, env=env, timeout=30)
        assert r.stdout.startswith("REFUSED"), r.stdout + r.stderr
        with pytest.raises(socket.timeout):
            srv.accept()
