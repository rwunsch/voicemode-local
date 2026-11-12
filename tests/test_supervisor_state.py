import subprocess
import sys

from vml_supervisor.profile import HostProfile
from vml_supervisor.state import (atomic_json, live_leases, read_json,
                                  service_lock, write_lease)


def make_profile(tmp_path):
    return HostProfile(
        name="t", root=tmp_path, run_dir=tmp_path / "run", runtime_dir=tmp_path / "rt",
        python=tmp_path / "py", working_dir=tmp_path, env={}, services={}, helpers={},
        reserved_ports=(), port_override=None, start_timeout=1, stop_timeout=1,
        watch_interval=1, restart_limit=(3, 60), lease_markers=("voice.py launch",),
        voice_server_marker="voice-mode", host_command=[], host_marker="", watch_command=[],
        watch_marker="", job_name="j")


def test_a_bom_written_lease_is_read(tmp_path):
    p = tmp_path / "lease"
    p.write_bytes("﻿{\"child\": 5}".encode("utf-8"))
    value, err = read_json(p, {})
    assert err is None and value["child"] == 5


def test_a_missing_file_is_the_default(tmp_path):
    assert read_json(tmp_path / "none", {"a": 1}) == ({"a": 1}, None)


def test_a_corrupt_state_is_a_result_not_a_crash(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("{ not json")
    value, err = read_json(p, {"d": 1})
    assert value == {"d": 1} and err.code == "state_corrupt"


def test_atomic_json_round_trips(tmp_path):
    atomic_json(tmp_path / "a" / "s.json", {"x": 1})
    assert read_json(tmp_path / "a" / "s.json", None) == ({"x": 1}, None)


def test_the_lock_holds_a_second_taker(tmp_path):
    code = ("import fcntl,sys\nf=open(sys.argv[1],'a+')\n"
            "try:\n fcntl.flock(f, fcntl.LOCK_EX|fcntl.LOCK_NB)\nexcept OSError:\n sys.exit(7)\n")
    with service_lock(tmp_path / "run"):
        held = subprocess.run([sys.executable, "-c", code, str(tmp_path / "run" / "services.lock")])
        assert held.returncode == 7
    free = subprocess.run([sys.executable, "-c", code, str(tmp_path / "run" / "services.lock")])
    assert free.returncode == 0


def test_a_killed_launchers_voice_server_keeps_its_lease(tmp_path):
    profile = make_profile(tmp_path)
    cmds = {4242: "python x voice-mode serve"}
    write_lease(profile, 999999, {"child": 4242})  # launcher is gone
    write_lease(profile, 999998, {})
    (profile.run_dir / "clients" / "999998").write_text("")  # gone with no server
    assert live_leases(profile, command_of=cmds.get) == [999999]
    assert not (profile.run_dir / "clients" / "999998").exists()
    cmds.clear()
    assert live_leases(profile, command_of=cmds.get) == []


def test_a_live_launcher_keeps_its_lease_and_dot_files_are_skipped(tmp_path):
    profile = make_profile(tmp_path)
    write_lease(profile, 10, {})
    (profile.run_dir / "clients" / ".record-x").write_text("")
    assert live_leases(profile, command_of={10: "py voice.py launch"}.get) == [10]
    assert (profile.run_dir / "clients" / ".record-x").exists()


def test_a_dead_empty_lease_is_removed(tmp_path):
    profile = make_profile(tmp_path)
    write_lease(profile, 77, {})
    (profile.run_dir / "clients" / "88").write_text("{ corrupt")
    assert live_leases(profile, command_of=lambda pid: None) == []
    assert not (profile.run_dir / "clients" / "77").exists()
    assert not (profile.run_dir / "clients" / "88").exists()


def test_a_directory_in_clients_does_not_break_live_leases(tmp_path):
    profile = make_profile(tmp_path)
    (profile.run_dir / "clients" / "55").mkdir(parents=True)
    (profile.run_dir / "clients" / "junk").mkdir()
    write_lease(profile, 10, {})
    assert live_leases(profile, command_of={10: "py voice.py launch"}.get) == [10]


def test_a_lease_that_cannot_be_removed_is_not_live_and_does_not_raise(tmp_path, monkeypatch):
    import pathlib
    profile = make_profile(tmp_path)
    write_lease(profile, 77, {})
    write_lease(profile, 10, {})

    def deny(self, missing_ok=False):
        raise PermissionError("held open")

    monkeypatch.setattr(pathlib.Path, "unlink", deny)
    assert live_leases(profile, command_of={10: "py voice.py launch"}.get) == [10]


def test_atomic_json_retries_a_blocked_replace(tmp_path, monkeypatch):
    import pathlib
    from vml_supervisor import state
    real = pathlib.Path.replace
    calls = []

    def flaky(self, target):
        calls.append(1)
        if len(calls) <= 2:
            raise PermissionError("reader holds it")
        return real(self, target)

    monkeypatch.setattr(pathlib.Path, "replace", flaky)
    monkeypatch.setattr(state.time, "sleep", lambda s: None)
    atomic_json(tmp_path / "s.json", {"k": 1})
    assert len(calls) == 3 and read_json(tmp_path / "s.json", None) == ({"k": 1}, None)


def test_atomic_json_reraises_after_twenty_blocked_replaces(tmp_path, monkeypatch):
    import pathlib
    import pytest
    from vml_supervisor import state
    calls = []

    def blocked(self, target):
        calls.append(1)
        raise PermissionError("always")

    monkeypatch.setattr(pathlib.Path, "replace", blocked)
    monkeypatch.setattr(state.time, "sleep", lambda s: None)
    with pytest.raises(PermissionError):
        atomic_json(tmp_path / "s.json", {"k": 1})
    assert len(calls) == 20
    assert not list(tmp_path.glob(".record-*"))


def test_read_state_reports_a_corrupt_or_non_object_file_and_reads_it_as_empty(tmp_path):
    from vml_supervisor.state import read_state
    profile = make_profile(tmp_path)
    results = []
    assert read_state(profile, results) == {} and results == []          # missing: empty, no problem
    (tmp_path / "run").mkdir()
    (tmp_path / "run/state.json").write_text("[1, 2]")
    assert read_state(profile, results) == {} and [r.code for r in results] == ["state_corrupt"]
    (tmp_path / "run/state.json").write_text("{not json")
    assert read_state(profile, results) == {} and [r.code for r in results] == ["state_corrupt"] * 2
    (tmp_path / "run/state.json").write_text('{"tts": {"pid": 1}}')
    assert read_state(profile, results) == {"tts": {"pid": 1}} and len(results) == 2


def test_write_state_reports_a_failed_write(tmp_path, monkeypatch):
    from vml_supervisor import state as state_module
    profile = make_profile(tmp_path)
    results = []
    assert state_module.write_state(profile, {"a": 1}, results) is True and results == []
    assert read_json(tmp_path / "run/state.json") == ({"a": 1}, None)

    def denied(path, value):
        raise PermissionError(13, "denied")
    monkeypatch.setattr(state_module, "atomic_json", denied)
    assert state_module.write_state(profile, {"a": 2}, results) is False
    assert [r.code for r in results] == ["state_write_failed"] and "denied" in results[0].detail
