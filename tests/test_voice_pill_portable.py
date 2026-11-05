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
    monkeypatch.setattr(st, "_retry_sleep", lambda s: None)


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
    assert st.env_file() == base / "voicemode.env"


def test_base_dir_expands_variables_and_tilde_like_upstream(monkeypatch, tmp_path):
    monkeypatch.setenv("VML_ROOT", str(tmp_path / "r"))
    monkeypatch.setenv("VOICEMODE_BASE_DIR", "$VML_ROOT/vm")
    assert st.voicemode_base() == tmp_path / "r" / "vm"
    monkeypatch.setenv("VOICEMODE_BASE_DIR", "~/vm2")
    assert st.voicemode_base() == tmp_path / "home" / "vm2"


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
    assert (ctx.base, ctx.home, ctx.sock) == (
        tmp_path / "one" / "pill", tmp_path / "one", tmp_path / "one" / "control.sock")
    monkeypatch.setenv("VOICEMODE_BASE_DIR", str(tmp_path / "two"))
    assert (ctx.base, ctx.home, ctx.sock) == (
        tmp_path / "two" / "pill", tmp_path / "two", tmp_path / "two" / "control.sock")
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


def test_conch_files_are_read_from_the_base_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("VOICEMODE_BASE_DIR", str(tmp_path / "rt"))
    (tmp_path / "rt").mkdir()
    (tmp_path / "rt" / "conch").write_text(json.dumps({"pid": os.getpid(), "agent": "a"}))
    assert vp.read_conch(vp.Context().home)["holder"]["pid"] == os.getpid()


@pytest.mark.parametrize("filename", ["voice_pill_state.py", "voice_pill.py", "voice_barge.py"])
def test_no_fixed_voicemode_path_outside_the_resolver(filename):
    code = [ln for ln in (PATCHES / filename).read_text(encoding="utf-8").splitlines()
            if not ln.lstrip().startswith("#")]
    hits = [ln for ln in code if '"~/.voicemode' in ln or '/ ".voicemode"' in ln
            or "Path.home()" in ln]
    allowed = 1 if filename == "voice_pill_state.py" else 0  # the resolver itself
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


def test_setting_default_when_absent(tmp_path):
    assert st.setting("VOICEMODE_PILL", "nope") == "nope"


def test_pill_flag(monkeypatch, tmp_path):
    assert st.pill_enabled() is False
    _env_file(tmp_path, b"VOICEMODE_PILL=true\r\n")
    assert st.pill_enabled() is True
    monkeypatch.setenv("VOICEMODE_PILL", "false")
    assert st.pill_enabled() is False


def test_pill_barge_flag_env_first(monkeypatch, tmp_path):
    home = _env_file(tmp_path, b"VOICEMODE_VOICE_BARGE=false\r\n")
    assert vp._barge_enabled(home) is False
    monkeypatch.setenv("VOICEMODE_VOICE_BARGE", "true")
    assert vp._barge_enabled(home) is True


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
    (tmp_path / "x.json").write_text('{"a": 1}')
    monkeypatch.setattr(st, "_read_bytes", _flaky(st._read_bytes, 2))
    assert st._read_json(tmp_path / "x.json") == {"a": 1}


def test_unlink_retries_on_windows(monkeypatch, tmp_path, windows):
    st.set_paused(True, os.getpid(), base=tmp_path)
    monkeypatch.setattr(st.os, "unlink", _flaky(os.unlink, 2))
    st.set_paused(False, os.getpid(), base=tmp_path)
    assert not (tmp_path / "paused.json").exists()


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


def test_barge_command_from_voicemode_env(tmp_path):
    ctl = _script(tmp_path)
    _env_file(tmp_path, f"VOICEMODE_BARGE_CONTROL_COMMAND={ctl}\r\n".encode())
    assert st.barge_control("status") == "status"


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
