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
