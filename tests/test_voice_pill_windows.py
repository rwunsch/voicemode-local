"""The pill backend and its state module on real Windows. Windows only; no audio.

Run from WSL with tests/run_windows_transport_tests.sh, which copies this file and
the modules it loads into a Windows temp folder, runs it with Windows Python, and
deletes the folder. On any other OS every test skips. Every state file goes to
pytest's tmp_path (VOICEMODE_BASE_DIR, and USERPROFILE for upstream's files), never
to the user's ~/.voicemode.
"""
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="needs Windows")

PATCHES = Path(__file__).resolve().parent.parent / "patches"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, PATCHES / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


if sys.platform == "win32":
    st = _load("voice_pill_state")
    vp = _load("voice_pill")


@pytest.fixture(autouse=True)
def private_base(monkeypatch, tmp_path):
    monkeypatch.setenv("VOICEMODE_BASE_DIR", str(tmp_path / "vm"))
    for k in ("USERPROFILE", "HOME"):  # upstream's own files: Path.home() / ".voicemode"
        monkeypatch.setenv(k, str(tmp_path / "home"))
    monkeypatch.delenv("VOICEMODE_CONTROL_SOCKET", raising=False)
    return tmp_path / "vm"


def finished_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


# -- liveness ------------------------------------------------------------------

def test_own_process_has_a_stable_start_token():
    a = st.process_start(os.getpid())
    assert a and a == st.process_start(os.getpid())


def test_a_child_start_token_differs_and_vanishes_when_it_exits():
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        tok = st.process_start(p.pid)
        assert tok and tok != st.process_start(os.getpid())
        assert vp.pid_alive(p.pid) is True
    finally:
        p.kill()
        p.wait()
    # The Popen object still holds a handle, so the process object lives on:
    # exited must still read as gone.
    assert st.process_start(p.pid) is None
    assert vp.pid_alive(p.pid) is False


def test_finished_pid_is_not_alive():
    pid = finished_pid()
    assert vp.pid_alive(pid) is False and st.process_start(pid) is None


def test_pid_alive_never_calls_os_kill(monkeypatch):
    def kill(*a):
        raise AssertionError("os.kill(pid, 0) terminates on Windows")
    monkeypatch.setattr(os, "kill", kill)
    assert vp.pid_alive(os.getpid()) is True


def test_a_system_process_we_cannot_open_counts_as_alive():
    # csrss/wininit run as SYSTEM: OpenProcess is refused, but they exist.
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq wininit.exe", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True).stdout
    pid = int(out.split(",")[1].strip('"'))
    assert vp.pid_alive(pid) is True


def test_sessions_keep_the_live_and_drop_the_dead(private_base):
    st.register_session(os.getpid(), "me", None)
    st.register_session(finished_pid(), "ghost", None)
    assert [s["label"] for s in st.live_sessions()] == ["me"]
    assert len(list((private_base / "pill" / "sessions").glob("*.json"))) == 1


def test_pause_owned_by_us_holds_and_a_dead_owner_does_not():
    st.set_paused(True, os.getpid())
    assert st.is_paused() is True
    st.set_paused(True, finished_pid())
    assert st.is_paused() is False


# -- lock ----------------------------------------------------------------------

def test_lock_is_exclusive_and_released_on_close(private_base):
    first = vp.acquire_lock(private_base / "pill")
    assert first is not None
    try:
        assert vp.acquire_lock(private_base / "pill") is None
        code = ("import importlib.util, sys; sys.path.insert(0, %r); "
                "import voice_pill as vp; sys.exit(0 if vp.acquire_lock(%r) is None else 3)"
                % (str(PATCHES), str(private_base / "pill")))
        assert subprocess.run([sys.executable, "-c", code]).returncode == 0
    finally:
        first.close()
    again = vp.acquire_lock(private_base / "pill")
    assert again is not None
    again.close()


# -- atomic write against a reader holding the file open -----------------------

@pytest.fixture
def backend(monkeypatch):
    monkeypatch.setattr(st, "_BACKEND", True)  # the pill backend's longer retry


def test_write_waits_out_a_reader_holding_the_file(private_base, backend):
    path = private_base / "x.json"
    private_base.mkdir(parents=True, exist_ok=True)
    st._write_json(path, {"n": 1})
    reader = open(path, "rb")  # Python opens without FILE_SHARE_DELETE
    threading.Timer(0.15, reader.close).start()
    st._write_json(path, {"n": 2})
    assert json.loads(path.read_text(encoding="utf-8")) == {"n": 2}


def test_server_write_gives_up_fast_on_a_held_file(private_base):
    # The voice server must not stall: two tries, one 25 ms sleep, then the error.
    path = private_base / "x.json"
    private_base.mkdir(parents=True, exist_ok=True)
    st._write_json(path, {"n": 1})
    with open(path, "rb"):
        t0 = time.monotonic()
        with pytest.raises(PermissionError):
            st._write_json(path, {"n": 2})
        assert time.monotonic() - t0 < 0.2
    assert not list(private_base.glob(".tmp-*"))


def test_resume_removes_the_pause_even_while_it_is_being_read(private_base, backend):
    st.set_paused(True, os.getpid())
    reader = open(private_base / "pill" / "paused.json", "rb")
    threading.Timer(0.15, reader.close).start()
    st.set_paused(False, os.getpid())
    assert not (private_base / "pill" / "paused.json").exists()


# -- the backend process: stdin thread, EOF exit -------------------------------

def test_backend_serves_stdin_and_exits_on_eof(private_base):
    env = {**os.environ, "VOICEMODE_BASE_DIR": str(private_base), "PYTHONUTF8": "1"}
    assert Path(env["USERPROFILE"]) == private_base.parent / "home"
    p = subprocess.Popen([sys.executable, str(PATCHES / "voice_pill.py"), "--stdio"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         env=env, text=True, encoding="utf-8")
    try:
        first = json.loads(p.stdout.readline())
        assert first["phase"] == "idle" and first["platform"] == "windows"
        t0 = time.monotonic()
        p.stdin.write(json.dumps({"cmd": "pause"}) + "\n")
        p.stdin.flush()
        paused = None
        for _ in range(3):
            snap = json.loads(p.stdout.readline())
            if snap["phase"] == "paused":
                paused = snap
                break
        assert paused is not None and time.monotonic() - t0 < 0.9
        assert (private_base / "pill" / "paused.json").exists()
        p.stdin.close()
        assert p.wait(timeout=5) == 0
        # Its own pause does not outlive it.
        assert not (private_base / "pill" / "paused.json").exists()
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()
        p.stdout.close()
        p.stderr.close()
