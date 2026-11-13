"""Tests for vml_supervisor.procs and vml_supervisor.ports."""
import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import pytest  # noqa: E402

from test_supervisor_profile import make_profile  # noqa: E402

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal mask")


def file_profile(tmp_path, **over):
    from vml_supervisor.profile import PortOverride
    return make_profile(port_override=PortOverride("file", tmp_path / "ports.json"), **over)


@pytest.mark.parametrize("bad", [True, "19000", 80, 70000, 8880])
def test_override_validation_refuses_what_is_not_a_usable_port(tmp_path, bad):
    from vml_supervisor.ports import port_candidates
    (tmp_path / "ports.json").write_text(json.dumps({"stt": bad}))
    cands, problem = port_candidates(file_profile(tmp_path), "stt")
    assert problem is not None and problem.code == "port_override_invalid"
    assert cands == list(range(9010, 9020))  # the range is still offered, not silently dropped


def test_override_validation_accepts_a_valid_port_and_puts_it_first(tmp_path):
    from vml_supervisor.ports import port_candidates
    (tmp_path / "ports.json").write_text(json.dumps({"stt": 19000}))
    cands, problem = port_candidates(file_profile(tmp_path), "stt")
    assert problem is None and cands == [19000, *range(9010, 9020)]
    cands, problem = port_candidates(file_profile(tmp_path), "tts")  # no key for tts
    assert problem is None and cands == list(range(8890, 8900))


def test_a_profile_reserved_port_is_refused(tmp_path):
    from vml_supervisor.ports import port_candidates
    (tmp_path / "ports.json").write_text(json.dumps({"tts": 1234}))
    _, problem = port_candidates(file_profile(tmp_path), "tts")
    assert problem is not None and problem.code == "port_override_invalid"


def test_override_file_missing_is_not_an_error_and_a_non_object_is(tmp_path):
    from vml_supervisor.ports import port_candidates
    cands, problem = port_candidates(file_profile(tmp_path), "tts")
    assert problem is None and cands == list(range(8890, 8900))
    (tmp_path / "ports.json").write_text("[]")
    cands, problem = port_candidates(file_profile(tmp_path), "tts")
    assert problem is not None and problem.code == "port_override_invalid" and cands == list(range(8890, 8900))
    (tmp_path / "ports.json").write_text("{not json")
    _, problem = port_candidates(file_profile(tmp_path), "tts")
    assert problem is not None and problem.code == "port_override_invalid"


def test_env_override(monkeypatch):
    from vml_supervisor.ports import port_candidates
    p = make_profile()  # env override, keys TTS_PORT / STT_PORT
    monkeypatch.delenv("TTS_PORT", raising=False)
    assert port_candidates(p, "tts") == (list(range(8890, 8900)), None)
    monkeypatch.setenv("TTS_PORT", "19001")
    assert port_candidates(p, "tts") == ([19001, *range(8890, 8900)], None)
    for bad in ("abc", "80", "8880", "70000", "1.5"):
        monkeypatch.setenv("TTS_PORT", bad)
        cands, problem = port_candidates(p, "tts")
        assert problem.code == "port_override_invalid" and cands == list(range(8890, 8900))


def test_override_none_means_no_override():
    from vml_supervisor.ports import port_candidates
    from vml_supervisor.profile import PortOverride
    p = make_profile(port_override=PortOverride("none"))
    assert port_candidates(p, "stt") == (list(range(9010, 9020)), None)


def test_a_taken_port_is_left_alone():
    from vml_supervisor.ports import choose_port
    p = make_profile()
    busy = socket.socket()
    busy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    busy.bind(("127.0.0.1", 8890))
    busy.listen()
    try:
        assert choose_port(p, "tts", set()) == 8891
    finally:
        busy.close()


def test_choose_port_returns_none_when_nothing_is_free(monkeypatch):
    from vml_supervisor import ports
    monkeypatch.setattr(ports, "port_free", lambda port: False)
    assert ports.choose_port(make_profile(), "tts", set()) is None


def test_one_port_is_never_chosen_for_two_services(tmp_path, monkeypatch):
    from vml_supervisor import ports
    monkeypatch.setattr(ports, "port_free", lambda port: True)  # TTS has not bound it yet
    (tmp_path / "ports.json").write_text(json.dumps({"tts": 9010}))  # TTS asks for STT's first port
    p = file_profile(tmp_path)
    taken: set[int] = set()
    tts = ports.choose_port(p, "tts", taken)
    taken.add(tts)
    stt = ports.choose_port(p, "stt", taken)
    assert tts == 9010 and stt == 9011


def test_owned_needs_the_marker_and_the_port():
    from vml_supervisor.procs import owned
    cmd = {1: "python /x/tts.py --host 127.0.0.1 --port 8890"}.get
    assert owned({"pid": 1, "marker": "tts.py", "port": 8890}, command_of=cmd)
    assert not owned({"pid": 1, "marker": "other.py", "port": 8890}, command_of=cmd)
    assert not owned({"pid": 1, "marker": "tts.py", "port": 8891}, command_of=cmd)
    assert owned({"pid": 1, "marker": "tts.py", "port": None}, command_of=cmd)
    assert not owned({"pid": 2, "marker": "tts.py", "port": 8890}, command_of=cmd)  # gone


@pytest.mark.parametrize("entry", [None, {}, "x", [], {"pid": "5", "marker": "m"}, {"pid": True, "marker": "m"},
                                   {"pid": 5.0, "marker": "m"}, {"pid": 0, "marker": "m"},
                                   {"pid": -3, "marker": "m"}, {"pid": None, "marker": "m"}])
def test_owned_gives_false_for_non_int_pids_and_bad_entries(entry):
    from vml_supervisor.procs import owned
    assert owned(entry, command_of=lambda pid: "m --port 1") is False


def test_owned_without_a_marker_is_false():
    from vml_supervisor.procs import owned
    assert not owned({"pid": 1}, command_of=lambda pid: "anything")


def test_process_command_reads_a_live_process_and_none_for_a_gone_one():
    from vml_supervisor.procs import process_command
    assert "pytest" in (process_command(os.getpid()) or "") or process_command(os.getpid())
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.1)"])
    child.wait()
    assert process_command(child.pid) is None


def test_port_free_and_listener_pids():
    from vml_supervisor.procs import listener_pids, port_free
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.listen()
    try:
        assert port_free(port) is False
        if sys.platform != "win32" and (os.path.exists("/usr/bin/lsof") or os.path.exists("/usr/sbin/lsof")):
            assert os.getpid() in listener_pids(port)
    finally:
        s.close()
    assert port_free(port) is True


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX TIME_WAIT semantics")
def test_a_port_left_in_time_wait_by_a_dead_service_is_free():
    """Task 7, 2026-11-13: a service that served a request and was killed leaves its side of the
    connection in TIME_WAIT for about a minute. The service binds with SO_REUSEADDR (HTTPServer,
    uvicorn), so it can start there again; port_free must agree, or the watchdog never restarts
    a dead service on its recorded port."""
    from vml_supervisor.procs import port_free
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    port = server.getsockname()[1]
    server.listen()
    client = socket.create_connection(("127.0.0.1", port))
    accepted, _ = server.accept()
    accepted.close()   # the server side closes first: its port is in TIME_WAIT
    time.sleep(0.1)
    client.close()
    server.close()
    assert port_free(port) is True


def test_process_tree_and_kill_tree():
    from vml_supervisor.procs import kill_tree, process_tree
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert child.pid in process_tree(child.pid)
        kill_tree(child.pid, 5)
        assert child.wait(timeout=5) is not None
    finally:
        if child.poll() is None:
            child.kill()


@posix_only
def test_a_child_has_stop_signals_unblocked():
    """Started while stop signals are held, a child must still obey SIGTERM (FELIX, 2026-11-04)."""
    from vml_supervisor.procs import stop_signals_held, unblock_stop_signals
    with stop_signals_held():
        assert signal.SIGTERM in signal.pthread_sigmask(signal.SIG_BLOCK, set())
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                 preexec_fn=unblock_stop_signals)
    # released on exit
    assert signal.SIGTERM not in signal.pthread_sigmask(signal.SIG_BLOCK, set())
    try:
        child.terminate()
        assert child.wait(timeout=5) == -signal.SIGTERM
    finally:
        if child.poll() is None:
            child.kill()


def test_options():
    from vml_supervisor.procs import background_options, connector_options
    if sys.platform == "win32":
        assert connector_options()["creationflags"] & subprocess.CREATE_NO_WINDOW
        assert background_options()["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert connector_options() == {}
        assert background_options() == {"start_new_session": True}


def test_the_package_imports_without_psutil():
    # In a fresh interpreter: reloading procs here would leave the modules that imported its
    # functions by name holding the old copies, for every test after this one.
    repo = str(Path(__file__).resolve().parent.parent)
    code = ("import sys; sys.modules['psutil'] = None; sys.path.insert(0, sys.argv[1]); "
            "from vml_supervisor import procs, services, helpers, host; "
            "assert callable(procs.process_command); print('ok')")
    done = subprocess.run([sys.executable, "-c", code, repo], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0 and done.stdout.strip() == "ok", done.stderr


def _gone(pid):
    from vml_supervisor import procs
    return not procs._alive(pid)


@posix_only
def test_kill_tree_takes_the_grandchildren_too(tmp_path):
    from vml_supervisor.procs import kill_tree
    marker = tmp_path / "pids"
    child = subprocess.Popen(
        ["sh", "-c", f"sleep 60 & echo $! > {marker}; sleep 60"], start_new_session=True)
    for _ in range(100):
        if marker.exists() and marker.read_text().strip():
            break
        time.sleep(0.05)
    grandchild = int(marker.read_text())
    try:
        assert kill_tree(child.pid, 5) is None
        child.wait(timeout=5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not _gone(grandchild):
            time.sleep(0.05)
        assert _gone(grandchild) and _gone(child.pid)
    finally:
        for pid in (grandchild, child.pid):
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)


@posix_only
def test_kill_tree_of_a_non_leader_signals_only_that_pid():
    from vml_supervisor.procs import kill_tree
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])  # shares our group
    try:
        assert kill_tree(child.pid, 5) is None
        child.wait(timeout=5)
        assert os.getpgid(0) == os.getpgrp()  # we were not signalled
    finally:
        if child.poll() is None:
            child.kill()


@posix_only
def test_kill_tree_reports_what_survives(monkeypatch):
    from vml_supervisor import procs
    child = subprocess.Popen(
        [sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"],
        start_new_session=True)
    time.sleep(0.3)
    real_kill, real_killpg = os.kill, os.killpg

    def no_sigkill(pid, sig):
        if sig != signal.SIGKILL:
            real_kill(pid, sig)

    def no_sigkillpg(pg, sig):
        if sig != signal.SIGKILL:
            real_killpg(pg, sig)

    monkeypatch.setattr(procs.os, "kill", no_sigkill)
    monkeypatch.setattr(procs.os, "killpg", no_sigkillpg)
    try:
        result = procs.kill_tree(child.pid, 0.3)
        assert result is not None and result.code == "kill_incomplete"
    finally:
        monkeypatch.undo()
        child.kill()
        child.wait()


def test_require_psutil_on_windows_without_psutil(monkeypatch):
    from vml_supervisor import procs
    monkeypatch.setattr(procs, "WINDOWS", True)
    monkeypatch.setattr(procs, "_psutil", lambda: None)
    result = procs.require_psutil()
    assert result is not None and result.code == "psutil_missing"
    kill = procs.kill_tree(12345, 1)
    assert kill is not None and kill.code == "psutil_missing"
    monkeypatch.setattr(procs, "_psutil", lambda: object())
    assert procs.require_psutil() is None


def test_require_psutil_is_none_off_windows(monkeypatch):
    from vml_supervisor import procs
    monkeypatch.setattr(procs, "WINDOWS", False)
    monkeypatch.setattr(procs, "_psutil", lambda: None)
    assert procs.require_psutil() is None
