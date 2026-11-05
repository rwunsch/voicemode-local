"""Portability of the pill backend, its state module and the barge trigger.

Base-dir resolver, environment-first settings, Windows branches (process identity,
lock, stdin, retrying file I/O, signals) and the configurable barge command. The
Windows branches run here with the platform monkeypatched; the real Win32 calls are
covered by tests/test_voice_pill_windows.py. No mic, no audio, no real ~/.voicemode.
"""
import importlib.util
import io
import json
import logging
import os
import signal
import sys
import threading
import time
import types
from pathlib import Path

import pytest

PATCHES = Path(__file__).resolve().parent.parent / "patches"


def _load(name, filename=None):
    spec = importlib.util.spec_from_file_location(name, PATCHES / (filename or f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


st = _load("voice_pill_state")
vp = _load("voice_pill")
vb = _load("voice_barge")
ct = _load("vml_ct_portable", "control_transport.py")

SETTINGS = ("VOICEMODE_BASE_DIR", "VOICEMODE_CONTROL_SOCKET", "VOICEMODE_VOICE_BARGE",
            "VOICEMODE_AUDIO_FEEDBACK", "VOICEMODE_PILL", "VOICEMODE_BARGE_CONTROL_COMMAND",
            "VOICEMODE_VOICE_BARGE_GATE_DB", "VOICEMODE_VOICE_BARGE_DEVICE")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for k in SETTINGS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)  # upstream walks up from the working folder for env files
    monkeypatch.setattr(st, "_retry_sleep", lambda s: None)
    monkeypatch.setattr(st, "_ENV_CACHE", {})
    monkeypatch.setattr(st, "_WARNED", set())


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(st, "_on_windows", lambda: True)


# -- 1. one resolver ----------------------------------------------------------

def test_base_dir_defaults_to_home(tmp_path):
    assert st.voicemode_base() == tmp_path / "home" / ".voicemode"
    assert st.default_base() == tmp_path / "home" / ".voicemode" / "pill"
    assert st.control_socket_path() == tmp_path / "home" / ".voicemode" / "control.sock"


def test_base_dir_honours_the_setting(monkeypatch, tmp_path):
    monkeypatch.setenv("VOICEMODE_BASE_DIR", str(tmp_path / "rt" / ".voicemode"))
    base = tmp_path / "rt" / ".voicemode"
    assert st.voicemode_base() == base
    assert st.default_base() == base / "pill"
    assert st.control_socket_path() == base / "control.sock"
    assert vb.preroll_path() == base / "barge_preroll.bin"
    # upstream's own files stay under the home folder (config.py:33, conch.py:133)
    assert st.env_file() == tmp_path / "home" / ".voicemode" / "voicemode.env"
    assert st.upstream_home() == tmp_path / "home" / ".voicemode"


def test_base_dir_expands_variables_and_tilde_like_upstream(monkeypatch, tmp_path):
    monkeypatch.setenv("VML_ROOT", str(tmp_path / "r"))
    monkeypatch.setenv("VOICEMODE_BASE_DIR", "$VML_ROOT/vm")
    assert st.voicemode_base() == tmp_path / "r" / "vm"
    monkeypatch.setenv("VOICEMODE_BASE_DIR", "~/vm2")
    assert st.voicemode_base() == tmp_path / "home" / "vm2"


def test_base_dir_from_voicemode_env_like_the_server(monkeypatch, tmp_path):
    # config.py loads voicemode.env into the environment before reading BASE_DIR.
    _env_file(tmp_path, f"VOICEMODE_BASE_DIR={tmp_path / 'fb'}\r\n".encode())
    assert st.voicemode_base() == tmp_path / "fb"
    assert st.default_base() == tmp_path / "fb" / "pill"
    assert st.control_socket_path() == tmp_path / "fb" / "control.sock"
    monkeypatch.setenv("VOICEMODE_BASE_DIR", str(tmp_path / "env"))
    assert st.voicemode_base() == tmp_path / "env"


def test_base_dir_from_the_file_expands_like_upstream(monkeypatch, tmp_path):
    _env_file(tmp_path, b"VOICEMODE_BASE_DIR=~/vmx\n")
    assert st.voicemode_base() == tmp_path / "home" / "vmx"


def test_empty_base_dir_means_the_default(monkeypatch, tmp_path):
    monkeypatch.setenv("VOICEMODE_BASE_DIR", "")
    assert st.voicemode_base() == tmp_path / "home" / ".voicemode"


def test_explicit_control_socket_still_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("VOICEMODE_BASE_DIR", str(tmp_path / "a"))
    monkeypatch.setenv("VOICEMODE_CONTROL_SOCKET", str(tmp_path / "x.sock"))
    assert st.control_socket_path() == tmp_path / "x.sock"


def test_a_base_dir_change_is_seen_without_a_restart(monkeypatch, tmp_path):
    ctx = vp.Context()
    monkeypatch.setenv("VOICEMODE_BASE_DIR", str(tmp_path / "one"))
    assert (ctx.base, ctx.sock) == (tmp_path / "one" / "pill", tmp_path / "one" / "control.sock")
    monkeypatch.setenv("VOICEMODE_BASE_DIR", str(tmp_path / "two"))
    assert (ctx.base, ctx.sock) == (tmp_path / "two" / "pill", tmp_path / "two" / "control.sock")
    monkeypatch.setenv("HOME", str(tmp_path / "h3"))
    assert ctx.home == tmp_path / "h3" / ".voicemode"
    assert vb.preroll_path() == tmp_path / "two" / "barge_preroll.bin"


def test_explicit_context_paths_are_kept(tmp_path):
    ctx = vp.Context(base=tmp_path / "p", home=tmp_path / "h", sock=tmp_path / "s")
    assert (ctx.base, ctx.home, ctx.sock) == (tmp_path / "p", tmp_path / "h", tmp_path / "s")
    ctx.home = tmp_path / "h2"
    assert ctx.home == tmp_path / "h2"


def test_state_without_a_base_lands_in_the_base_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("VOICEMODE_BASE_DIR", str(tmp_path / "rt"))
    st.register_session(os.getpid(), "demo", None)
    assert (tmp_path / "rt" / "pill" / "sessions" / f"{os.getpid()}.json").exists()
    assert not (tmp_path / "home" / ".voicemode").exists()


def test_conch_files_are_read_where_upstream_writes_them(monkeypatch, tmp_path):
    # upstream: Conch.LOCK_FILE = Path.home() / ".voicemode" / "conch", whatever the base dir
    monkeypatch.setenv("VOICEMODE_BASE_DIR", str(tmp_path / "rt"))
    home = tmp_path / "home" / ".voicemode"
    (home / "conch.queue.d").mkdir(parents=True)
    (tmp_path / "rt").mkdir()
    (home / "conch").write_text(json.dumps({"pid": os.getpid(), "agent": "a"}))
    (home / "conch.queue.d" / "w.json").write_text(json.dumps({"pid": os.getpid(), "seq": 1}))
    (tmp_path / "rt" / "conch").write_text(json.dumps({"pid": 1, "agent": "wrong"}))
    conch = vp.read_conch(vp.Context().home)
    assert conch["holder"]["pid"] == os.getpid() and len(conch["waiters"]) == 1


def test_env_file_falls_back_to_the_old_name_like_upstream(tmp_path):
    d = tmp_path / "home" / ".voicemode"
    d.mkdir(parents=True)
    (d / ".voicemode.env").write_text("VOICEMODE_PILL=true\n")
    assert st.env_file() == d / ".voicemode.env" and st.pill_enabled() is True
    (d / "voicemode.env").write_text("VOICEMODE_PILL=false\n")
    assert st.env_file() == d / "voicemode.env" and st.pill_enabled() is False


def _upstream_source(name):
    spec = importlib.util.find_spec("voice_mode")  # finds the package; does not import it
    if spec is None or not spec.submodule_search_locations:
        pytest.skip("no installed voice_mode to compare against")
    path = Path(list(spec.submodule_search_locations)[0]) / name
    if not path.is_file():
        pytest.skip(f"installed voice_mode has no {name}")
    return path.read_text(encoding="utf-8")


def _upstream_eval(expr):
    ns = {"Path": Path, "os": os, "BASE_DIR": st.voicemode_base(),
          "expand_path": lambda v: Path(os.path.expanduser(os.path.expandvars(v)))}
    return eval(expr, ns)  # noqa: S307 - fixed expressions from this file


# (file, expression as upstream writes it, what ours resolves to)
UPSTREAM_PATHS = [
    ("conch.py", 'Path.home() / ".voicemode" / "conch"', lambda: vp.Context().home / "conch"),
    ("conch_queue.py", 'QUEUE_DIRNAME = "conch.queue.d"', None),
    ("conch_queue.py", "return Conch.LOCK_FILE.parent", None),
    ("config.py", 'Path.home() / ".voicemode" / "voicemode.env"', lambda: st.env_file()),
    ("config.py", 'expand_path(os.getenv("VOICEMODE_BASE_DIR", str(Path.home() / ".voicemode")))',
     lambda: st.voicemode_base()),
    ("config.py", 'os.getenv("VOICEMODE_CONTROL_SOCKET", str(BASE_DIR / "control.sock"))',
     lambda: st.control_socket_path()),
]


@pytest.mark.parametrize("filename,expr,ours", UPSTREAM_PATHS)
@pytest.mark.parametrize("base_dir", [None, "rt"])
def test_paths_are_pinned_to_upstream_expressions(monkeypatch, tmp_path, filename, expr, ours,
                                                  base_dir):
    assert expr in _upstream_source(filename), f"upstream {filename} drifted: {expr}"
    if base_dir:
        monkeypatch.setenv("VOICEMODE_BASE_DIR", str(tmp_path / base_dir))
    if ours is not None:
        assert Path(_upstream_eval(expr)) == ours()


def test_control_socket_follows_upstream_expansion(monkeypatch, tmp_path):
    monkeypatch.setenv("VML_S", str(tmp_path))
    monkeypatch.setenv("VOICEMODE_CONTROL_SOCKET", "$VML_S/c.sock")
    assert st.control_socket_path() == tmp_path / "c.sock"


@pytest.mark.parametrize("filename", ["voice_pill_state.py", "voice_pill.py", "voice_barge.py"])
def test_no_fixed_voicemode_path_outside_the_resolver(filename):
    code = [ln for ln in (PATCHES / filename).read_text(encoding="utf-8").splitlines()
            if not ln.lstrip().startswith("#")]
    hits = [ln for ln in code if '"~/.voicemode' in ln or '/ ".voicemode"' in ln
            or "Path.home()" in ln]
    allowed = 1 if filename == "voice_pill_state.py" else 0  # upstream_home() itself
    assert len(hits) == allowed, hits


# -- 2. settings: environment first, then voicemode.env ----------------------

def _env_file(tmp_path, data: bytes):
    d = tmp_path / "home" / ".voicemode"
    d.mkdir(parents=True, exist_ok=True)
    (d / "voicemode.env").write_bytes(data)
    return d


def test_setting_reads_crlf_utf8_with_bom(tmp_path):
    _env_file(tmp_path, b"\xef\xbb\xbfVOICEMODE_VOICE_BARGE=true\r\n# caf\xc3\xa9\r\n")
    assert st.setting("VOICEMODE_VOICE_BARGE") == "true"


def test_setting_last_assignment_wins_and_comments_are_skipped(tmp_path):
    _env_file(tmp_path, b"VOICEMODE_PILL=false\n# VOICEMODE_PILL=no\nexport VOICEMODE_PILL=\"true\"\n")
    assert st.setting("VOICEMODE_PILL") == "true"


def test_setting_survives_bytes_that_are_not_utf8(tmp_path):
    _env_file(tmp_path, b"X=\xff\xfe\nVOICEMODE_PILL=true\n")
    assert st.setting("VOICEMODE_PILL") == "true"


def test_setting_from_the_environment_beats_the_file(monkeypatch, tmp_path):
    _env_file(tmp_path, b"VOICEMODE_VOICE_BARGE=false\n")
    monkeypatch.setenv("VOICEMODE_VOICE_BARGE", "true")
    assert st.setting("VOICEMODE_VOICE_BARGE") == "true"


def _age(path, seconds=100):
    t = time.time_ns() - seconds * 10 ** 9
    os.utime(path, ns=(t, t))


def test_env_file_is_reread_only_after_it_changes(monkeypatch, tmp_path):
    d = _env_file(tmp_path, b"VOICEMODE_PILL=true\n")
    _age(d / "voicemode.env")  # a file written long ago is cached
    reads = []
    real = st._read_bytes
    monkeypatch.setattr(st, "_read_bytes", lambda p: reads.append(p) or real(p))
    assert st.setting("VOICEMODE_PILL") == "true"
    assert st.setting("VOICEMODE_PILL") == "true"
    st.voicemode_base()
    assert len(reads) == 1
    (d / "voicemode.env").write_bytes(b"VOICEMODE_PILL=false\n")  # new size
    _age(d / "voicemode.env")
    assert st.setting("VOICEMODE_PILL") == "false" and len(reads) == 2
    (d / "voicemode.env").write_bytes(b"VOICEMODE_PILL=fals\n\n")  # same size, new mtime
    os.utime(d / "voicemode.env", ns=(1, 1))
    assert st.setting("VOICEMODE_PILL") == "fals" and len(reads) == 3


def test_a_same_size_rewrite_within_the_same_second_is_seen(tmp_path):
    # mtime moves on a coarse clock tick: an unchanged (mtime, size) proves nothing.
    d = _env_file(tmp_path, b"VOICEMODE_BASE_DIR=/a/one\n")
    path = d / "voicemode.env"
    now = time.time_ns()
    os.utime(path, ns=(now, now))
    assert st.voicemode_base() == Path("/a/one")
    path.write_bytes(b"VOICEMODE_BASE_DIR=/a/two\n")  # same size, same file
    os.utime(path, ns=(now, now))                     # and the same mtime
    assert st.voicemode_base() == Path("/a/two")


def test_a_replaced_file_with_the_same_stamp_is_seen(tmp_path):
    d = _env_file(tmp_path, b"VOICEMODE_PILL=true\n")
    path = d / "voicemode.env"
    _age(path)
    old = os.stat(path).st_mtime_ns
    assert st.setting("VOICEMODE_PILL") == "true"
    tmp = d / "new.env"
    tmp.write_bytes(b"VOICEMODE_PILL=nope\n")
    os.utime(tmp, ns=(old, old))
    os.replace(tmp, path)  # new inode, same mtime and size
    assert st.setting("VOICEMODE_PILL") == "nope"


def test_a_cached_env_cannot_be_changed_by_a_caller(tmp_path):
    _env_file(tmp_path, b"VOICEMODE_PILL=true\n")
    st.read_env_file()["VOICEMODE_PILL"] = "false"
    assert st.setting("VOICEMODE_PILL") == "true"


def test_setting_default_when_absent(tmp_path):
    assert st.setting("VOICEMODE_PILL", "nope") == "nope"


def test_pill_flag(monkeypatch, tmp_path):
    assert st.pill_enabled() is False
    _env_file(tmp_path, b"VOICEMODE_PILL=true\r\n")
    assert st.pill_enabled() is True
    monkeypatch.setenv("VOICEMODE_PILL", "false")
    assert st.pill_enabled() is False


def test_pill_barge_flag_file_wins_when_it_has_the_key(monkeypatch, tmp_path):
    # The pill's own toggle writes voicemode.env: read the flag from where it lands.
    home = _env_file(tmp_path, b"VOICEMODE_VOICE_BARGE=false\r\n")
    monkeypatch.setenv("VOICEMODE_VOICE_BARGE", "true")
    assert vp._barge_enabled(home) is False


def test_pill_barge_flag_env_is_the_default(monkeypatch, tmp_path):
    home = _env_file(tmp_path, b"OTHER=1\n")
    monkeypatch.setenv("VOICEMODE_VOICE_BARGE", "true")
    assert vp._barge_enabled(home) is True


def test_a_barge_toggle_shows_even_with_the_old_value_in_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("VOICEMODE_VOICE_BARGE", "false")
    home = tmp_path / "home" / ".voicemode"
    switch = _script(tmp_path, "voicemode-switch",
                     f'mkdir -p "{home}"; echo "VOICEMODE_VOICE_BARGE=true" > "{home}/voicemode.env"')
    ctx = vp.Context(base=tmp_path / "pill", sock=tmp_path / "none.sock", switch=switch)
    assert vp.gather(ctx)["barge"]["enabled"] is False
    vp.handle_command({"cmd": "set_barge", "on": True}, ctx)
    ctx.barge_thread.join(5)
    assert vp.gather(ctx)["barge"]["enabled"] is True


def test_pill_barge_flag_reads_crlf_file(tmp_path):
    assert vp._barge_enabled(_env_file(tmp_path, b"VOICEMODE_VOICE_BARGE=true\r\n")) is True


def test_beeps_default_env_first(monkeypatch, tmp_path):
    home = _env_file(tmp_path, b"VOICEMODE_AUDIO_FEEDBACK=true\r\n")
    ctx = vp.Context(base=tmp_path / "pill", home=home, sock=tmp_path / "s")
    assert vp._beeps_on(ctx) is True
    monkeypatch.setenv("VOICEMODE_AUDIO_FEEDBACK", "false")
    assert vp._beeps_on(ctx) is False
    st.set_beeps(True, ctx.base)  # the pill's own choice still wins
    assert vp._beeps_on(ctx) is True


def test_barge_trigger_settings_env_first_and_crlf(monkeypatch, tmp_path):
    _env_file(tmp_path, b"VOICEMODE_VOICE_BARGE_GATE_DB=-30\r\nVOICEMODE_VOICE_BARGE_DEVICE=mic1\r\n")
    monkeypatch.setenv("VOICEMODE_VOICE_BARGE_DEVICE", "mic2")
    s = vb.trigger_settings()
    assert s["VOICEMODE_VOICE_BARGE_GATE_DB"] == "-30" and s["VOICEMODE_VOICE_BARGE_DEVICE"] == "mic2"


# -- 3. process identity and liveness on Windows ------------------------------

class FakeTransport:
    def __init__(self, starts):
        self.starts, self.calls = starts, []

    def process_start_time(self, pid):
        self.calls.append(("start", pid))
        return self.starts.get(pid)

    def process_alive(self, pid):
        self.calls.append(("alive", pid))
        return pid in self.starts


def _no_spawn(*a, **k):
    raise AssertionError("must not spawn ps on Windows")


def test_process_start_on_windows_asks_the_transport(monkeypatch, windows):
    fake = FakeTransport({42: "133777"})
    monkeypatch.setattr(st, "_control_transport", lambda: fake)
    monkeypatch.setattr(st.subprocess, "run", _no_spawn)
    assert st.process_start(42) == "133777"
    assert st.process_start(43) is None


def test_process_start_on_windows_swallows_api_failure(monkeypatch, windows):
    def broken():
        raise OSError("no kernel32")
    monkeypatch.setattr(st, "_control_transport", broken)
    assert st.process_start(42) is None


def test_pid_alive_on_windows_never_calls_os_kill(monkeypatch, windows):
    fake = FakeTransport({42: "1"})
    monkeypatch.setattr(st, "_control_transport", lambda: fake)

    def kill(*a):
        raise AssertionError("os.kill(pid, 0) terminates the process on Windows")
    monkeypatch.setattr(vp.os, "kill", kill)
    assert vp.pid_alive(42) is True
    assert vp.pid_alive(43) is False
    assert ("alive", 42) in fake.calls


def test_pid_alive_on_posix_still_uses_kill(monkeypatch):
    monkeypatch.setattr(st, "_on_windows", lambda: False)
    assert vp.pid_alive(os.getpid()) is True


def test_transport_liveness_is_windows_only():
    if sys.platform == "win32":
        pytest.skip("POSIX check")
    with pytest.raises(OSError):
        ct.process_start_time(os.getpid())
    with pytest.raises(OSError):
        ct.process_alive(os.getpid())


def test_filetime_token_is_stable_text():
    assert ct.filetime_token(0x01DB, 0x12345678) == str((0x01DB << 32) | 0x12345678)


# -- 4. single-instance lock with msvcrt --------------------------------------

class FakeMsvcrt:
    LK_NBLCK, LK_UNLCK = 2, 0

    def __init__(self, held=False):
        self.held, self.calls = held, []

    def locking(self, fd, mode, nbytes):
        self.calls.append((os.lseek(fd, 0, os.SEEK_CUR), mode, nbytes))
        if self.held:
            raise PermissionError(13, "locked")


def test_lock_uses_a_far_byte_with_msvcrt(monkeypatch, tmp_path):
    fake = FakeMsvcrt()
    monkeypatch.setattr(vp, "fcntl", None)
    monkeypatch.setattr(vp, "msvcrt", fake)
    f = vp.acquire_lock(tmp_path / "pill")
    try:
        assert f is not None
        assert fake.calls == [(vp.LOCK_OFFSET, fake.LK_NBLCK, 1)]
        assert vp.LOCK_OFFSET >= 1 << 30 and vp.LOCK_OFFSET < 1 << 31
    finally:
        f.close()


def test_msvcrt_lock_is_unlocked_before_close(monkeypatch, tmp_path):
    fake = FakeMsvcrt()
    monkeypatch.setattr(vp, "fcntl", None)
    monkeypatch.setattr(vp, "msvcrt", fake)
    f = vp.acquire_lock(tmp_path / "pill")
    vp.release_lock(f)
    assert fake.calls[-1] == (vp.LOCK_OFFSET, fake.LK_UNLCK, 1)
    assert f.closed


def test_release_lock_on_posix_just_closes(tmp_path):
    f = vp.acquire_lock(tmp_path / "pill")
    vp.release_lock(f)
    assert f.closed and vp.acquire_lock(tmp_path / "pill") is not None


def test_lock_taken_with_msvcrt_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(vp, "fcntl", None)
    monkeypatch.setattr(vp, "msvcrt", FakeMsvcrt(held=True))
    assert vp.acquire_lock(tmp_path / "pill") is None


# -- 5. stdin: reader thread on Windows, select on POSIX ----------------------

def test_input_source_per_platform(monkeypatch):
    r, w = os.pipe()
    try:
        monkeypatch.setattr(st, "_on_windows", lambda: False)
        assert isinstance(vp._input_source(r), vp.SelectInput)
        monkeypatch.setattr(st, "_on_windows", lambda: True)
        src = vp._input_source(r)
        assert isinstance(src, vp.ThreadInput)
    finally:
        os.close(w)
        src._thread.join(2)
        os.close(r)


def test_reader_thread_serves_commands_and_ends_on_eof(monkeypatch, tmp_path):
    monkeypatch.setattr(vp, "_input_source", vp.ThreadInput)  # the Windows path, here
    ctx = vp.Context(base=tmp_path / "pill", home=tmp_path / "vm", sock=tmp_path / "none.sock")
    r, w = os.pipe()
    out = io.StringIO()
    t = threading.Thread(target=vp.run_stdio, args=(ctx,), kwargs={"stdin_fd": r, "out": out},
                         daemon=True)
    t.start()
    os.write(w, (json.dumps({"cmd": "pause"}) + "\n").encode())
    deadline = time.time() + 3
    while '"paused"' not in out.getvalue() and time.time() < deadline:
        time.sleep(0.02)
    os.close(w)
    t.join(3)
    os.close(r)
    assert not t.is_alive()
    assert '"phase": "paused"' in out.getvalue()


def test_reader_thread_treats_a_read_error_as_eof(monkeypatch):
    def bad_read(fd, n):
        raise OSError(9, "bad fd")
    monkeypatch.setattr(vp.os, "read", bad_read)
    src = vp.ThreadInput(99)
    assert src.wait(2) == b""


def test_reader_thread_timeout_returns_none(monkeypatch):
    r, w = os.pipe()
    try:
        src = vp.ThreadInput(r)
        assert src.wait(0.05) is None
        os.write(w, b"x")
        assert src.wait(2) == b"x"
    finally:
        os.close(w)
        src._thread.join(2)
        os.close(r)


# -- 6. atomic writes and reads that survive Windows sharing ------------------

def _flaky(real, failures, exc=PermissionError):
    count = {"n": 0}

    def fn(*a, **k):
        if count["n"] < failures:
            count["n"] += 1
            raise exc(13, "in use")
        return real(*a, **k)
    fn.count = count
    return fn


def test_write_retries_a_busy_target_on_windows(monkeypatch, tmp_path, windows):
    monkeypatch.setattr(st, "_BACKEND", True)  # the backend's longer retry
    flaky = _flaky(os.replace, 3)
    monkeypatch.setattr(st.os, "replace", flaky)
    st._write_json(tmp_path / "x.json", {"a": 1})
    assert json.loads((tmp_path / "x.json").read_text()) == {"a": 1}
    assert flaky.count["n"] == 3
    assert not list(tmp_path.glob(".tmp-*"))


def test_write_gives_up_eventually_and_cleans_up(monkeypatch, tmp_path, windows):
    monkeypatch.setattr(st.os, "replace", _flaky(os.replace, 10 ** 6))
    with pytest.raises(PermissionError):
        st._write_json(tmp_path / "x.json", {"a": 1})
    assert not list(tmp_path.glob(".tmp-*"))


def test_write_does_not_retry_on_posix(monkeypatch, tmp_path):
    monkeypatch.setattr(st, "_on_windows", lambda: False)
    flaky = _flaky(os.replace, 1)
    monkeypatch.setattr(st.os, "replace", flaky)
    with pytest.raises(PermissionError):
        st._write_json(tmp_path / "x.json", {"a": 1})


def test_written_state_is_utf8(tmp_path):
    st._write_json(tmp_path / "x.json", {"label": "café"})
    assert json.loads((tmp_path / "x.json").read_bytes().decode("utf-8")) == {"label": "café"}


def test_read_retries_on_windows(monkeypatch, tmp_path, windows):
    monkeypatch.setattr(st, "_BACKEND", True)  # the backend's mode
    (tmp_path / "x.json").write_text('{"a": 1}')
    monkeypatch.setattr(st, "_read_bytes", _flaky(st._read_bytes, 2))
    assert st._read_json(tmp_path / "x.json") == {"a": 1}


def test_unlink_retries_on_windows(monkeypatch, tmp_path, windows):
    monkeypatch.setattr(st, "_BACKEND", True)
    st.set_paused(True, os.getpid(), base=tmp_path)
    monkeypatch.setattr(st.os, "unlink", _flaky(os.unlink, 2))
    st.set_paused(False, os.getpid(), base=tmp_path)
    assert not (tmp_path / "paused.json").exists()


@pytest.fixture
def sleeps(monkeypatch):
    seen = []
    monkeypatch.setattr(st, "_retry_sleep", seen.append)
    return seen


def _all_busy(monkeypatch):
    def busy(p):
        raise PermissionError(13, "sharing violation")
    monkeypatch.setattr(st, "_read_bytes", busy)


def test_server_side_reads_never_sleep(monkeypatch, tmp_path, windows, sleeps):
    base = tmp_path / "pill"
    st.set_paused(True, os.getpid(), base=base)
    st.register_session(os.getpid(), "demo", None, base=base)
    _all_busy(monkeypatch)
    assert st.is_paused(base=base) is False
    assert st.get_override(1, base=base) == {}
    assert st.effective_voice(1, "v", base=base) == "v"
    assert st.beeps_choice(base=base) is None
    assert st.result_note(1, base=base) == ""
    assert st.interruptions_since(0, base=base) == 0
    st.voicemode_base()
    assert sleeps == []


def test_a_stale_pause_is_removed_without_sleeping(monkeypatch, tmp_path, windows, sleeps):
    base = tmp_path / "pill"
    st._base(base)
    (base / "paused.json").write_text(json.dumps({"owner": 999999999, "start": "x"}))
    monkeypatch.setattr(st, "_control_transport", lambda: FakeTransport({}))
    monkeypatch.setattr(st.os, "unlink", _flaky(os.unlink, 10 ** 6))
    assert st.is_paused(base=base) is False
    assert sleeps == []


def test_preroll_read_never_sleeps(monkeypatch, tmp_path, windows, sleeps):
    import numpy as np
    path = tmp_path / "pre.bin"
    vb.write_preroll(path, np.zeros(160, dtype=np.float32), 1.0)
    monkeypatch.setattr(vb, "_retrying", st._retrying)
    real = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: (_ for _ in ()).throw(PermissionError(13, "busy"))
                        if self == path else real(self))
    out = vb.prepend_preroll(np.zeros(10, dtype=np.int16), 1.5, 16000, path=path, wait_s=0)
    assert len(out) == 10 and sleeps == []


def test_backend_reads_retry(monkeypatch, tmp_path, windows, sleeps):
    monkeypatch.setattr(st, "_BACKEND", True)
    base = tmp_path / "pill"
    st.register_session(os.getpid(), "demo", None, base=base)
    _all_busy(monkeypatch)
    st.live_sessions(base=base)
    assert sleeps


def test_the_backend_turns_read_retries_on(monkeypatch):
    monkeypatch.setattr(st, "_BACKEND", False)
    monkeypatch.setattr(vp, "run_stdio", lambda ctx: None)
    assert vp.main([]) == 0
    assert st._BACKEND is True


def _always_busy(monkeypatch):
    calls = []

    def replace(*a):
        calls.append(a)
        raise PermissionError(13, "busy")
    monkeypatch.setattr(st.os, "replace", replace)
    return calls


def test_server_writes_try_twice_with_one_short_sleep(monkeypatch, tmp_path, windows, sleeps):
    monkeypatch.setattr(st, "_BACKEND", False)
    base = tmp_path / "pill"
    st.register_session(os.getpid(), "demo", None, base=base)
    calls = _always_busy(monkeypatch)
    with pytest.raises(PermissionError):
        st.touch_session(os.getpid(), "v", base=base)
    assert len(calls) == 2 and len(sleeps) <= 1 and all(x <= 0.025 for x in sleeps)


def test_interruption_compaction_in_the_server_tries_twice(monkeypatch, tmp_path, windows, sleeps):
    monkeypatch.setattr(st, "_BACKEND", False)
    monkeypatch.setattr(st, "_MAX_INTERRUPTION_BYTES", 4)
    base = tmp_path / "pill"
    st.record_interruption(1.0, base=base)
    calls = _always_busy(monkeypatch)
    st.record_interruption(2.0, base=base)  # compaction fails quietly; the line is kept
    assert len(calls) == 2 and len(sleeps) <= 1 and all(x <= 0.025 for x in sleeps)
    assert st.interruptions_since(0, base=base) == 2


def test_the_backend_keeps_its_longer_write_retry(monkeypatch, tmp_path, windows, sleeps):
    monkeypatch.setattr(st, "_BACKEND", True)
    calls = _always_busy(monkeypatch)
    with pytest.raises(PermissionError):
        st._write_json(tmp_path / "x.json", {})
    assert len(calls) == st._RETRIES


def _unreadable(monkeypatch, path):
    real = st._read_bytes

    def read(p):
        if Path(p) == path:
            raise PermissionError(13, "sharing violation")
        return real(p)
    monkeypatch.setattr(st, "_read_bytes", read)


def test_an_unreadable_session_is_kept_not_deleted(monkeypatch, tmp_path):
    base = tmp_path / "pill"
    st.register_session(os.getpid(), "demo", None, base=base)
    st.set_override(os.getpid(), "af_bella", None, base=base)
    st.request_stop(os.getpid(), base=base)
    path = base / "sessions" / f"{os.getpid()}.json"
    _unreadable(monkeypatch, path)
    st.live_sessions(base=base)
    assert path.exists()
    assert (base / f"override-{os.getpid()}.json").exists()
    assert (base / "stop" / str(os.getpid())).exists()


def test_an_unreadable_pause_is_kept_and_reads_as_not_paused(monkeypatch, tmp_path):
    base = tmp_path / "pill"
    st.set_paused(True, os.getpid(), base=base)
    _unreadable(monkeypatch, base / "paused.json")
    assert st.is_paused(base=base) is False
    assert (base / "paused.json").exists()


def test_an_alive_pid_with_an_unknown_start_is_kept(monkeypatch, tmp_path):
    base = tmp_path / "pill"
    st.register_session(os.getpid(), "demo", None, base=base)
    st.set_paused(True, os.getpid(), base=base)
    monkeypatch.setattr(st, "process_start", lambda pid: None)  # now ps fails
    assert [s["pid"] for s in st.live_sessions(base=base)] == [os.getpid()]
    assert st.is_paused(base=base) is True


def test_an_unknown_recorded_start_is_treated_the_old_way(tmp_path):
    # A reused pid must not inherit a session or a Pause recorded without a start time.
    base = tmp_path / "pill"
    st._write_json(st._session_path(os.getpid(), base), {"pid": os.getpid(), "label": "x",
                                                         "start": None})
    assert st.live_sessions(base=base) == []
    st._write_json(base / "paused.json", {"owner": os.getpid(), "start": None})
    assert st.is_paused(base=base) is False


def test_a_dead_pid_with_an_unknown_start_is_removed(monkeypatch, tmp_path):
    base = tmp_path / "pill"
    import subprocess as sp
    p = sp.Popen([sys.executable, "-c", "pass"]); p.wait()
    monkeypatch.setattr(st, "process_start", lambda pid: None)
    st.register_session(p.pid, "ghost", None, base=base)
    assert st.live_sessions(base=base) == []


def test_a_missing_or_corrupt_session_is_still_removed(tmp_path):
    base = tmp_path / "pill"
    st.register_session(os.getpid(), "demo", None, base=base)
    (base / "sessions" / "999999.json").write_text("{half")
    assert [s["pid"] for s in st.live_sessions(base=base)] == [os.getpid()]
    assert not (base / "sessions" / "999999.json").exists()


def test_read_json_state_tells_missing_from_unreadable(monkeypatch, tmp_path):
    assert st._read_json_state(tmp_path / "absent.json") == ({}, "missing")
    (tmp_path / "bad.json").write_text("{")
    assert st._read_json_state(tmp_path / "bad.json") == ({}, "invalid")
    (tmp_path / "ok.json").write_text('{"a": 1}')
    _unreadable(monkeypatch, tmp_path / "ok.json")
    assert st._read_json_state(tmp_path / "ok.json") == ({}, "unreadable")


# -- other clients of the resolver ---------------------------------------------

def test_ptt_client_socket_follows_upstream_config(monkeypatch, tmp_path):
    ptt = _load("vml_ptt_portable", "ptt_control_client.py")
    monkeypatch.delenv("VOICEMODE_CONTROL_SOCKET_PATH", raising=False)
    assert ptt.socket_path() == tmp_path / "home" / ".voicemode" / "control.sock"
    monkeypatch.setenv("VOICEMODE_BASE_DIR", str(tmp_path / "rt"))
    assert ptt.socket_path() == tmp_path / "rt" / "control.sock"
    monkeypatch.setenv("VOICEMODE_CONTROL_SOCKET_PATH", str(tmp_path / "old.sock"))
    assert ptt.socket_path() == tmp_path / "old.sock"
    monkeypatch.setenv("VOICEMODE_CONTROL_SOCKET", str(tmp_path / "up.sock"))
    assert ptt.socket_path() == tmp_path / "up.sock"


def test_mac_pill_position_file_is_in_the_pill_folder(monkeypatch, tmp_path):
    mac = _load("vml_mac_portable", "voice_pill_mac.py")
    app = mac.App(False, "backend")
    assert app.pos_file == tmp_path / "home" / ".voicemode" / "pill" / "window-position.txt"
    monkeypatch.setenv("VOICEMODE_BASE_DIR", str(tmp_path / "rt"))
    assert app.pos_file == tmp_path / "rt" / "pill" / "window-position.txt"


@pytest.mark.parametrize("filename", ["ptt_control_client.py", "voice_pill_mac.py"])
def test_no_fixed_pill_or_socket_path_in_other_clients(filename):
    src = (PATCHES / filename).read_text(encoding="utf-8")
    assert '"~/.voicemode/control.sock"' not in src
    assert '".voicemode" / "pill"' not in src


# -- 7. a configurable barge control command ----------------------------------

def _script(tmp_path, name="ctl", body='echo "$@"'):
    p = tmp_path / name
    p.write_text(f"#!/bin/sh\n{body}\n")
    p.chmod(0o755)
    return p


def test_barge_command_from_a_json_list(monkeypatch, tmp_path):
    ctl = _script(tmp_path)
    monkeypatch.setenv("VOICEMODE_BARGE_CONTROL_COMMAND", json.dumps([str(ctl), "felix", "barge"]))
    assert st.barge_control("ON") == "felix barge on"


def test_barge_command_from_a_bare_path(monkeypatch, tmp_path):
    ctl = _script(tmp_path)
    monkeypatch.setenv("VOICEMODE_BARGE_CONTROL_COMMAND", str(ctl))
    assert st.barge_control("off") == "off"


def test_barge_command_in_voicemode_env_is_ignored(monkeypatch, tmp_path):
    # upstream's update_config lets a model write any key into voicemode.env:
    # a command there must never run.
    marker = tmp_path / "ran"
    ctl = _script(tmp_path, body=f'touch "{marker}"')
    _env_file(tmp_path, f"VOICEMODE_BARGE_CONTROL_COMMAND={ctl}\r\n".encode())
    switch = _script(tmp_path, "voicemode-switch")
    monkeypatch.setattr(st, "default_switch", lambda: switch)
    assert st.barge_control("status") == "barge status"
    assert not marker.exists()


def _upstream_loader():
    """upstream's own find_voicemode_env_files + load_voicemode_env, run unmodified."""
    import ast
    raw = os.environ.get("VM_UPSTREAM_CONFIG")
    if raw:
        path = Path(raw)
    else:
        spec = importlib.util.find_spec("voice_mode")
        if spec is None or not spec.submodule_search_locations:
            pytest.skip("no upstream voice_mode config.py (set VM_UPSTREAM_CONFIG)")
        path = Path(list(spec.submodule_search_locations)[0]) / "config.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef)
             and n.name in ("find_voicemode_env_files", "load_voicemode_env")]
    assert len(funcs) == 2, f"upstream {path} drifted"
    ns = {"Path": Path, "os": os}
    exec(compile(ast.Module(body=funcs, type_ignores=[]), str(path), "exec"), ns)
    return ns["load_voicemode_env"]


@pytest.mark.parametrize("where", ["global", "project"])
def test_a_command_the_server_loaded_from_a_file_is_refused(monkeypatch, tmp_path, caplog, where):
    marker = tmp_path / "ran"
    ctl = _script(tmp_path, body=f'touch "{marker}"')
    line = f"VOICEMODE_BARGE_CONTROL_COMMAND={ctl}\n".encode()
    if where == "global":  # what update_config writes
        _env_file(tmp_path, line)
    else:  # a project file upstream finds walking up from its working folder
        (tmp_path / "proj" / "sub").mkdir(parents=True)
        (tmp_path / "proj" / ".voicemode.env").write_bytes(line)
        monkeypatch.chdir(tmp_path / "proj" / "sub")
    _upstream_loader()()  # what config_reload, or a restart, does
    assert os.environ.get("VOICEMODE_BARGE_CONTROL_COMMAND") == str(ctl)
    switch = _script(tmp_path, "voicemode-switch")
    monkeypatch.setattr(st, "default_switch", lambda: switch)
    caplog.set_level(logging.WARNING, logger="voicemode.voice_pill_state")
    assert st.barge_control("on") == "barge on"
    assert st.barge_control("off") == "barge off"
    assert not marker.exists()
    assert len([r for r in caplog.records if "VOICEMODE_BARGE_CONTROL_COMMAND" in r.getMessage()]) == 1


def test_a_command_only_in_the_environment_is_used(monkeypatch, tmp_path):
    _env_file(tmp_path, b"VOICEMODE_PILL=true\n")
    ctl = _script(tmp_path)
    monkeypatch.setenv("VOICEMODE_BARGE_CONTROL_COMMAND", str(ctl))
    assert st.barge_control("on") == "on"


def test_barge_control_docstring_keeps_the_promise():
    assert "model output never reaches" in st.barge_control.__doc__
    assert "environment only" in st.barge_command.__doc__


def test_barge_command_missing_executable(monkeypatch, tmp_path):
    monkeypatch.setenv("VOICEMODE_BARGE_CONTROL_COMMAND", str(tmp_path / "absent"))
    assert "not found" in st.barge_control("on")


@pytest.mark.parametrize("value", ["[", "[]", '["a", 1]', '{"a": 1}'])
def test_barge_command_malformed_is_refused(monkeypatch, tmp_path, value):
    monkeypatch.setenv("VOICEMODE_BARGE_CONTROL_COMMAND", value)
    assert "VOICEMODE_BARGE_CONTROL_COMMAND" in st.barge_control("on")


def test_barge_command_still_rejects_bad_actions(monkeypatch, tmp_path):
    marker = tmp_path / "ran"
    ctl = _script(tmp_path, body=f'touch "{marker}"')
    monkeypatch.setenv("VOICEMODE_BARGE_CONTROL_COMMAND", str(ctl))
    assert "must be on, off or status" in st.barge_control("on; rm -rf /")
    assert not marker.exists()


def test_an_explicit_switch_beats_the_setting(monkeypatch, tmp_path):
    monkeypatch.setenv("VOICEMODE_BARGE_CONTROL_COMMAND", str(tmp_path / "absent"))
    switch = _script(tmp_path, "voicemode-switch")
    assert st.barge_control("on", switch) == "barge on"


def test_default_is_still_voicemode_switch(monkeypatch, tmp_path):
    switch = _script(tmp_path, "voicemode-switch")
    monkeypatch.setattr(st, "default_switch", lambda: switch)
    assert st.barge_control("off") == "barge off"


# -- 8. an elevated server, refused by a Medium client: say why, once ---------

def test_an_elevated_server_refusal_is_explained_once(monkeypatch, caplog):
    monkeypatch.setattr(ct, "current_user_sid", lambda: "S-1-5-21-1")
    monkeypatch.setattr(ct, "pipe_owner_sid", lambda conn: "S-1-5-21-1")
    monkeypatch.setattr(ct, "_REFUSALS_LOGGED", set())

    def denied(conn):
        e = PermissionError(13, "Access is denied")
        e.winerror = 5
        raise e
    monkeypatch.setattr(ct, "server_identity", denied)
    caplog.set_level(logging.WARNING, logger="voicemode.control_transport")
    for _ in range(3):
        with pytest.raises(PermissionError) as info:
            ct._verify_server(object(), "\\\\.\\pipe\\x")
        assert "elevated" in str(info.value)
    assert len([r for r in caplog.records if "elevated" in r.getMessage()]) == 1


def test_label_cap_comment_does_not_promise_access():
    assert "can still reach an elevated server" not in (PATCHES / "control_transport.py").read_text()


# -- 9. exits and signals ------------------------------------------------------

def _stub_run(monkeypatch):
    monkeypatch.setattr(vp, "run_stdio", lambda ctx: None)


def test_main_runs_where_sigterm_is_missing(monkeypatch, tmp_path):
    _stub_run(monkeypatch)
    monkeypatch.delattr(signal, "SIGTERM")
    assert vp.main(["--stdio"]) == 0


def test_main_runs_off_the_main_thread(monkeypatch, tmp_path):
    _stub_run(monkeypatch)
    result = []
    t = threading.Thread(target=lambda: result.append(vp.main(["--stdio"])))
    t.start()
    t.join(5)
    assert result == [0]


def test_main_releases_its_pause_on_eof(monkeypatch, tmp_path):
    monkeypatch.setenv("VOICEMODE_BASE_DIR", str(tmp_path / "rt"))

    def run(ctx):
        st.set_paused(True, os.getpid(), ctx.base)
    monkeypatch.setattr(vp, "run_stdio", run)
    assert vp.main([]) == 0
    assert not (tmp_path / "rt" / "pill" / "paused.json").exists()


def test_main_on_windows_ends_cleanly_when_the_window_pipe_is_gone(monkeypatch, windows):
    def run(ctx):
        raise OSError(22, "Invalid argument")  # Windows' write to a closed pipe
    monkeypatch.setattr(vp, "run_stdio", run)
    assert vp.main([]) == 0


def test_main_on_posix_keeps_other_errors_loud(monkeypatch):
    monkeypatch.setattr(st, "_on_windows", lambda: False)

    def run(ctx):
        raise OSError(22, "Invalid argument")
    monkeypatch.setattr(vp, "run_stdio", run)
    with pytest.raises(OSError):
        vp.main([])


def test_barge_stop_handlers_skip_missing_signals(monkeypatch):
    monkeypatch.delattr(signal, "SIGTERM")
    stop = threading.Event()
    vb.install_stop_handlers(stop)  # must not raise
    seen = []
    t = threading.Thread(target=lambda: seen.append(vb.install_stop_handlers(stop)))
    t.start()
    t.join(5)
    assert seen == [None]
