"""vml_supervisor.host: the POSIX watchdog, the Windows voice host, and the connector's ensure.

Ported from FELIX 454f20d0e tests/repo/test_voicemode_setup.py (`_host_world` and the six host
tests after it, plus test_watchdog_stops_services_when_every_connector_was_killed at ~959) and from
voicemode-local's tests/test_voicemode_native.py (the watch_tick tests, ~L589-616).

Mapping (source test -> here):
  FELIX test_watchdog_stops_services_when_every_connector_was_killed
      -> test_the_watchdog_stops_services_when_every_connector_was_killed (a real watchdog
         process running watch_main from the profile file)
  ours test_watchdog_restarts_a_crashed_service_on_the_same_port and
       test_watchdog_does_not_restart_on_a_taken_port_or_too_often
      -> test_the_watchdog_restarts_a_dead_service_within_its_limit
  FELIX test_the_voice_host_keeps_services_and_helpers_until_no_connector_is_left
      -> same name. FELIX called start_missing_services every tick; here it is called only for a
         service with no live record, so a service that runs is started once.
  FELIX test_the_voice_host_follows_the_barge_choice_and_records_helper_problems
      -> test_the_voice_host_records_helper_results_in_helpers_json. FELIX's "off" choice (stop
         the trigger) has no equivalent: read_mode gives a mode, and a changed mode restarts the
         helper (test_host_restarts_a_helper_when_its_mode_changes). helpers.json is
         {kind: {state, code, detail, results}} instead of a list of problem strings.
  FELIX test_a_new_voice_host_waits_for_its_first_connector_then_ends
      -> test_host_stops_after_the_grace_when_no_lease_is_left
  FELIX test_on_windows_a_connector_starts_the_host_not_the_services
      -> same name, through ensure() (the services half stays in test_supervisor_services.py)
  FELIX test_the_host_is_started_once_outside_every_chat -> same name, on ensure_host
  FELIX test_windows_process_service_result_is_read_strictly
      -> test_a_refused_host_start_is_a_result (plus the strict "0 <pid>" read)
"""
import contextlib
import dataclasses
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from vml_supervisor import helpers as helpers_module
from vml_supervisor import host, services
from vml_supervisor.hooks import Hooks
from vml_supervisor.profile import HelperSpec, HostProfile, PortOverride, ServiceSpec
from vml_supervisor.result import Result
from vml_supervisor.services import check_service, ensure_services

from test_supervisor_services import alive, codes, read_state, wait_dead, world  # noqa: F401 (fixture)

REPO = Path(__file__).resolve().parent.parent
POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="acts out POSIX with fake programs")

# A watchdog the way a profile owner starts one: our python, the profile file as its argument.
WATCH_SCRIPT = ("import sys; sys.path.insert(0, sys.argv[1]); from vml_supervisor import host; "
                "sys.exit(host.main(['watch', sys.argv[2]]))")


def log_codes(profile):
    path = Path(profile.run_dir) / "watch.log"
    if not path.exists():
        return []
    return [json.loads(line.split(" ", 2)[2])["code"] for line in path.read_text().splitlines()]


# ----- the POSIX watchdog ---------------------------------------------------------------------


@POSIX_ONLY
def test_the_watchdog_stops_services_when_every_connector_was_killed(world):
    """FELIX ~959: the connector crashed (no release); the watchdog sees no live lease and stops
    the services, then exits."""
    profile_file = world.tmp / "profile.json"
    profile = dataclasses.replace(
        world.profile, watch_interval=0.3,
        watch_command=[sys.executable, "-c", WATCH_SCRIPT, str(REPO), str(profile_file), "vml-test-watch"])
    profile.to_file(profile_file)
    chosen, results = ensure_services(profile, world.connector.pid)
    assert set(chosen) == {"tts", "stt"}, results
    state = read_state(profile)
    watcher = int((profile.run_dir / "watch.pid").read_text())
    assert alive(watcher) and all(alive(entry["pid"]) for entry in state.values())
    world.connector.kill()  # no release(): the connector crashed
    world.connector.wait()
    assert all(wait_dead(entry["pid"], timeout=20) for entry in state.values())
    assert wait_dead(watcher, timeout=10)
    assert read_state(profile) == {}


@POSIX_ONLY
def test_the_watchdog_restarts_a_dead_service_within_its_limit(world, monkeypatch):
    """Ours L589-616: a dead service is started again with its recorded command on its recorded
    port; never onto a taken port; at most restart_limit times in the window; each is a Result in
    run/watch.log."""
    profile = dataclasses.replace(world.profile, restart_limit=(3, 300.0))
    chosen, results = ensure_services(profile, world.connector.pid)
    assert set(chosen) == {"tts", "stt"}, results
    old = read_state(profile)["tts"]
    os.kill(old["pid"], signal.SIGKILL)
    assert wait_dead(old["pid"])
    watch = host.WatchState()
    assert host.watch_tick(profile, Hooks(), watch) is True
    entry = read_state(profile)["tts"]
    world.started.append(entry["pid"])
    assert entry["pid"] != old["pid"] and entry["port"] == old["port"] == chosen["tts"]
    deadline = time.monotonic() + 20
    while not check_service("kokoro", entry["port"], "/v1/models") and time.monotonic() < deadline:
        time.sleep(0.1)
    assert check_service("kokoro", entry["port"], "/v1/models")
    assert log_codes(profile) == ["service_restarted"]
    assert "restarted tts on port" in (profile.run_dir / "watch.log").read_text()

    # A taken port: not restarted there, said once.
    os.kill(entry["pid"], signal.SIGKILL)
    assert wait_dead(entry["pid"])
    spawned = []
    monkeypatch.setattr(host, "port_free", lambda port: False)
    monkeypatch.setattr(host, "spawn", lambda argv, job, **kw: spawned.append(argv) or (
        SimpleNamespace(pid=4_000_000 + len(spawned)), Result("no_job")))
    assert host.watch_tick(profile, Hooks(), watch) is True
    assert host.watch_tick(profile, Hooks(), watch) is True
    assert spawned == [] and log_codes(profile).count("restart_port_taken") == 1

    # Too often: the fake restarts never live, so each look restarts again, up to the limit.
    monkeypatch.setattr(host, "port_free", lambda port: True)
    for _ in range(5):
        host.watch_tick(profile, Hooks(), watch)
    assert len(spawned) == 2   # one restart was already spent above
    assert log_codes(profile).count("restart_limit_reached") == 1
    assert all(arg == old["command"] for arg in spawned)


# ----- the Windows host (fakes) ---------------------------------------------------------------


def make_profile(root: Path, **over) -> HostProfile:
    barge = HelperSpec(["python", "-m", "voice_barge", "--mode={mode}"], "voice_barge", "barge.log", (), True)
    ptt = HelperSpec(["ptt-listener", "--go"], "ptt-listener", "ptt.log", (), False)
    fields = dict(
        name="test", root=root, run_dir=root / "run", runtime_dir=root / "runtimes/r",
        python=Path(sys.executable), working_dir=root / "work", env={},
        services={"tts": ServiceSpec(["tts", "--port", "{port}"], "tts", (28880, 28899), "/v1/models", "kokoro"),
                  "stt": ServiceSpec(["stt", "--port", "{port}"], "stt", (22022, 22041), "/health", "whisper")},
        helpers={"barge": barge, "ptt": ptt}, reserved_ports=(), port_override=PortOverride("none"),
        start_timeout=5.0, stop_timeout=2.0, watch_interval=3.0, restart_limit=(3, 300.0),
        lease_markers=("lease",), voice_server_marker="voice-server",
        host_command=[sys.executable, str(root / "voice.py"), "host"], host_marker="voice.py host",
        watch_command=[], watch_marker="", job_name="vml-test-job", host_start_grace=60.0)
    fields.update(over)
    return HostProfile(**fields)


def _host_world(tmp_path, monkeypatch, **over):
    """FELIX's _host_world: a voice folder, fake services and helpers, no real lock or sleep."""
    root = tmp_path / "voice"
    (root / "run").mkdir(parents=True)
    profile = make_profile(root, **over)
    profile_file = root / "run" / host.HOST_PROFILE
    profile.to_file(profile_file)
    calls, live = [], set()
    monkeypatch.setattr(host, "WINDOWS", False)   # no real Job Object here
    monkeypatch.setattr(host, "service_lock", lambda run_dir: contextlib.nullcontext())
    monkeypatch.setattr(host, "owned", lambda entry: isinstance(entry, dict) and entry.get("pid") in live)

    def start_missing(profile, hooks, runtime, state, started, results):
        calls.append("services")
        for kind, pid, port in (("tts", 1, 28880), ("stt", 2, 22022)):
            if kind in profile.services:
                state[kind] = {"pid": pid, "port": port, "marker": kind, "log": "x",
                               "command": [kind, "--port", str(port)], "runtime": runtime}
                live.add(pid)
        host.atomic_json(profile.run_dir / "state.json", state)
        return set()

    def start_helper(profile, kind, mode, hooks=None):
        calls.append((kind, mode))
        return Result("helper_started", f"{kind} started", {"helper": kind, "results": []})

    def stop_recorded(profile, state):
        calls.append("stopped")
        return []

    monkeypatch.setattr(host, "start_missing_services", start_missing)
    monkeypatch.setattr(host, "start_helper", start_helper)
    monkeypatch.setattr(host, "stop_recorded", stop_recorded)
    monkeypatch.setattr(host.time, "sleep", lambda seconds: None)
    return SimpleNamespace(root=root, profile=profile, file=profile_file, calls=calls, live=live)


def test_the_voice_host_keeps_services_and_helpers_until_no_connector_is_left(tmp_path, monkeypatch):
    """Laptop, 2026-11-12: the services were children of the first chat's connector, and the
    Copilot app ended them with that chat. The host owns them instead, keeps its helpers running,
    and ends with the last connector."""
    world = _host_world(tmp_path, monkeypatch)
    leases = iter([[11], [11], []])
    monkeypatch.setattr(host, "live_leases", lambda profile: next(leases))
    (world.root / "run/host.pid").write_text("99")
    assert host.host_main(world.file) == 0
    assert world.calls.count("services") == 1   # started once, running at the second look
    assert world.calls.count(("barge", None)) == 2 and world.calls.count(("ptt", None)) == 2
    assert world.calls[-1] == "stopped" and not (world.root / "run/host.pid").exists()
    assert "host_stopped" in log_codes(world.profile)


def test_the_voice_host_records_helper_results_in_helpers_json(tmp_path, monkeypatch):
    """helpers.json is the chat's start notes: {kind: {state, code, detail, results}}, with the
    secondary results flattened in, so a kill_incomplete behind a successful start shows."""
    world = _host_world(tmp_path, monkeypatch)
    leases = iter([[11], []])
    monkeypatch.setattr(host, "live_leases", lambda profile: next(leases))
    written = []
    real_atomic = host.atomic_json
    monkeypatch.setattr(host, "atomic_json", lambda path, value: (
        written.append(json.loads(json.dumps(value))) if Path(path).name == host.HELPERS_FILE else None,
        real_atomic(path, value)))

    def start_helper(profile, kind, mode, hooks=None):
        if kind == "ptt":
            return Result("helper_exited", "the ptt helper stopped at once", {"helper": kind, "results": []})
        return Result("helper_started", "started", {"helper": kind, "results": [
            Result("kill_incomplete", "process 5 is still alive", {"pids": [5]})]})

    monkeypatch.setattr(host, "start_helper", start_helper)
    host.host_main(world.file)
    assert written[-1] == {
        "barge": {"state": "running", "code": "helper_started", "detail": "started",
                  "results": [{"code": "kill_incomplete", "detail": "process 5 is still alive"}]},
        "ptt": {"state": "failed", "code": "helper_exited", "detail": "the ptt helper stopped at once",
                "results": []}}


def test_host_stops_after_the_grace_when_no_lease_is_left(tmp_path, monkeypatch):
    """FELIX: a new host waits host_start_grace for its first connector, then ends."""
    world = _host_world(tmp_path, monkeypatch)
    monkeypatch.setattr(host, "live_leases", lambda profile: [])
    late = world.profile.host_start_grace + 1.0
    clock = iter([0.0, 1.0, 1.0])
    monkeypatch.setattr(host.time, "monotonic", lambda: next(clock, late))
    assert host.host_main(world.file) == 0
    assert world.calls.count("services") == 1 and world.calls[-1] == "stopped"


def test_host_restarts_a_helper_when_its_mode_changes(tmp_path, monkeypatch):
    """A helper reads its mode only when it starts. The host asks read_mode every watch_interval
    and restarts the helper when the answer changed; the same mode leaves it alone."""
    world = _host_world(tmp_path, monkeypatch)
    leases = iter([[11], [11], [11], []])
    monkeypatch.setattr(host, "live_leases", lambda profile: next(leases))
    modes = iter(["auto", "auto", "on"])
    monkeypatch.setattr(host, "read_mode", lambda profile, kind, hooks, results: next(modes) if kind == "barge"
                        else None)
    pids = iter(range(500, 600))

    def start_helper(profile, kind, mode, hooks=None):
        world.calls.append((kind, mode))
        state = read_state(profile)
        pid = next(pids)
        world.live.add(pid)
        state[kind] = {"pid": pid, "port": None, "marker": kind, "mode": mode if kind == "barge" else None}
        host.atomic_json(profile.run_dir / "state.json", state)
        return Result("helper_started", "started", {"helper": kind, "results": []})

    monkeypatch.setattr(host, "start_helper", start_helper)
    host.host_main(world.file)
    assert [c for c in world.calls if isinstance(c, tuple) and c[0] == "barge"] == [("barge", "auto"), ("barge", "on")]
    assert [c for c in world.calls if isinstance(c, tuple) and c[0] == "ptt"] == [("ptt", None)]


def test_the_host_restarts_a_dead_service_on_its_recorded_port(tmp_path, monkeypatch):
    """Review Focus 3: a new host finds the services of the one that died recorded but dead; it
    starts them again on their recorded ports when free, and on another port when not (said)."""
    world = _host_world(tmp_path, monkeypatch)
    host.atomic_json(world.profile.run_dir / "state.json", {
        "tts": {"pid": 70, "port": 28885, "marker": "tts", "log": str(world.root / "run/tts.log"),
                "command": ["tts", "--port", "28885"], "runtime": "r"},
        "stt": {"pid": 71, "port": 22030, "marker": "stt", "log": str(world.root / "run/stt.log"),
                "command": ["stt", "--port", "22030"], "runtime": "r"}})
    leases = iter([[11], []])
    monkeypatch.setattr(host, "live_leases", lambda profile: next(leases))
    monkeypatch.setattr(host, "port_free", lambda port: port == 28885)
    spawned = []
    monkeypatch.setattr(host, "spawn", lambda argv, job, **kw: spawned.append((argv, kw["cwd"])) or (
        SimpleNamespace(pid=800), Result("no_job")))
    host.host_main(world.file)
    assert spawned == [(["tts", "--port", "28885"], str(world.profile.working_dir))]
    assert world.calls.count("services") == 1   # stt, whose port was taken, on another port
    log = log_codes(world.profile)
    assert "service_restarted" in log and "restart_port_taken" in log and "service_moved" in log


# ----- start_host, ensure_host and ensure (Windows, faked) ------------------------------------


def test_a_refused_host_start_is_a_result(tmp_path, monkeypatch):
    """Windows' process service is asked through Windows PowerShell by its full path; the reply
    is read strictly as "0 <pid>". Anything else is host_start_refused, with no fallback."""
    profile = make_profile(tmp_path / "voice")
    runs = []
    replies = iter([SimpleNamespace(stdout="0 1234\r\n", stderr="", returncode=0),
                    SimpleNamespace(stdout="9 0", stderr="denied", returncode=0),
                    SimpleNamespace(stdout="", stderr="CIM is blocked by policy", returncode=1)])
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    monkeypatch.setattr(host.subprocess, "run", lambda argv, **kw: runs.append((argv, kw)) or next(replies))
    monkeypatch.setattr(host, "background_options", lambda: {})
    started = host.start_host(profile)
    assert started.code == "host_started" and started.data["pid"] == 1234
    argv, kw = runs[0]
    assert argv[0].replace("\\", "/").endswith("System32/WindowsPowerShell/v1.0/powershell.exe")
    assert argv[0].startswith("C:")
    script = argv[-1]
    assert "New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly" in script and "ShowWindow" in script
    assert "Invoke-CimMethod -ClassName Win32_Process -MethodName Create" in script
    profile_file = profile.run_dir / host.HOST_PROFILE
    words = [*profile.host_command, str(profile_file)]
    assert "CommandLine='" + " ".join(f'"{w}"' for w in words) + "'" in script
    assert f"CurrentDirectory='{profile.working_dir}'" in script
    assert kw["cwd"] == str(profile.working_dir) and kw["stdin"] == subprocess.DEVNULL
    assert HostProfile.from_file(profile_file) == profile   # the host inherits nothing: it reads this
    for _ in range(2):
        refused = host.start_host(profile)
        assert refused.code == "host_start_refused"
    assert "CIM is blocked by policy" in refused.detail
    runs.clear()
    quoted = host.start_host(make_profile(tmp_path / "voice", host_command=['C:/p"q/python.exe', "voice.py host"]))
    broken = host.start_host(make_profile(tmp_path / "voice", host_command=["python", "voice.py host\n"]))
    assert quoted.code == broken.code == "host_command_bad" and runs == []


def test_a_word_ending_in_a_backslash_keeps_its_closing_quote(tmp_path, monkeypatch):
    """`"C:\\dir\\"` would read as an escaped quote; the trailing backslash is doubled."""
    profile = make_profile(tmp_path / "voice", host_command=[sys.executable, "voice.py host", "C:\\dir\\"])
    runs = []
    monkeypatch.setattr(host.subprocess, "run", lambda argv, **kw: runs.append(argv) or SimpleNamespace(
        stdout="0 5", stderr="", returncode=0))
    monkeypatch.setattr(host, "background_options", lambda: {})
    assert host.start_host(profile).code == "host_started"
    assert '"C:\\dir\\\\"' in runs[0][-1]


@pytest.mark.parametrize("running", [True, False])
def test_the_host_is_started_once_outside_every_chat(tmp_path, monkeypatch, running):
    profile = make_profile(tmp_path / "voice")
    profile.run_dir.mkdir(parents=True)
    (profile.run_dir / "host.pid").write_text("4242")
    monkeypatch.setattr(host, "process_command",
                        lambda pid: f"python voice.py host {profile.run_dir}" if running else None)
    started = []
    monkeypatch.setattr(host, "start_host", lambda p: started.append(p) or Result("host_started", "", {"pid": 777}))
    results = []
    assert host.ensure_host(profile, results) is True
    if running:
        assert started == [] and results == []
        return
    assert started == [profile] and codes(results) == ["host_started"]
    assert (profile.run_dir / "host.pid").read_text() == "777"


def test_without_psutil_no_host_is_started(tmp_path, monkeypatch):
    """Without psutil no process can be looked up on Windows: a running host would look dead, and
    every connector would start another one beside it."""
    profile = make_profile(tmp_path / "voice")
    profile.run_dir.mkdir(parents=True)
    monkeypatch.setattr(host, "require_psutil", lambda: Result("psutil_missing", "no psutil"))
    monkeypatch.setattr(host, "start_host", lambda p: pytest.fail("a host was started"))
    results = []
    assert host.ensure_host(profile, results) is False
    assert codes(results) == ["host_requires_psutil"]


def _windows(monkeypatch, profile):
    monkeypatch.setattr(services, "WINDOWS", True)
    monkeypatch.setattr(host, "WINDOWS", True)
    monkeypatch.setattr(services, "require_psutil", lambda: None)
    monkeypatch.setattr(services, "start_missing_services", lambda *a: pytest.fail("a connector started services"))
    monkeypatch.setattr(services, "_start_watchdog", lambda *a: pytest.fail("a watchdog on Windows"))
    monkeypatch.setattr(services, "wait_until_ready",
                        lambda p, started=None, kinds=None: ({"tts": 28880, "stt": 22022}, []))
    Path(profile.runtime_dir).mkdir(parents=True, exist_ok=True)
    (Path(profile.runtime_dir) / ".complete.json").write_text("{}")


def test_on_windows_a_connector_starts_the_host_not_the_services(tmp_path, monkeypatch):
    profile = make_profile(tmp_path / "voice")
    _windows(monkeypatch, profile)
    hosts = []
    monkeypatch.setattr(host, "ensure_host", lambda p, results: hosts.append(
        (p, (p.run_dir / "clients" / "11").exists())) or True)
    assert host.ensure(profile, 11) == ({"tts": 28880, "stt": 22022}, [])
    assert hosts == [(profile, True)]   # the lease comes first


def test_a_dead_host_is_started_again_by_the_next_connector(tmp_path, monkeypatch):
    """Review Focus 3: run/host.pid names a process that is gone, or that is now another program;
    the next connector's ensure starts a new host and records it."""
    profile = make_profile(tmp_path / "voice")
    _windows(monkeypatch, profile)
    profile.run_dir.mkdir(parents=True)
    commands = {4242: None, 4243: "notepad.exe"}
    monkeypatch.setattr(host, "process_command", lambda pid: commands.get(pid))
    started = []

    def start_host(p):
        pid = 900 + len(started)
        started.append(pid)
        commands[pid] = f"python voice.py host {p.run_dir}"
        return Result("host_started", f"host {pid}", {"pid": pid})

    monkeypatch.setattr(host, "start_host", start_host)
    for dead in (4242, 4243):
        (profile.run_dir / "host.pid").write_text(str(dead))
        ports, results = host.ensure(profile, 11)
        assert ports == {"tts": 28880, "stt": 22022} and codes(results) == ["host_started"]
        assert (profile.run_dir / "host.pid").read_text() == str(started[-1])
    assert host.ensure(profile, 12) == ({"tts": 28880, "stt": 22022}, [])   # it runs: left alone
    assert started == [900, 901]


def test_a_refused_host_start_starts_nothing_and_waits_for_nothing(tmp_path, monkeypatch):
    profile = make_profile(tmp_path / "voice")
    _windows(monkeypatch, profile)
    monkeypatch.setattr(host, "process_command", lambda pid: None)
    monkeypatch.setattr(host, "start_host", lambda p: Result("host_start_refused", "9 0 denied"))
    monkeypatch.setattr(services, "wait_until_ready", lambda *a, **kw: pytest.fail("waited"))
    ports, results = host.ensure(profile, 11)
    assert ports == {} and codes(results) == ["host_start_refused"]
    assert not (profile.run_dir / "host.pid").exists()


@POSIX_ONLY
def test_two_connectors_start_one_host(tmp_path, monkeypatch):
    """Review Focus 2: two connectors at the same moment. run/host.pid is written under the
    service lock, so the second finds the first one's host and starts none."""
    profile = make_profile(tmp_path / "voice")
    _windows(monkeypatch, profile)
    commands = {}
    monkeypatch.setattr(host, "process_command", lambda pid: commands.get(pid))
    started = []

    def start_host(p):
        time.sleep(0.3)   # a slow PowerShell: the other connector arrives meanwhile
        pid = 700 + len(started)
        started.append(pid)
        commands[pid] = "python voice.py host x"
        return Result("host_started", "", {"pid": pid})

    monkeypatch.setattr(host, "start_host", start_host)
    out = {}
    threads = [threading.Thread(target=lambda pid=pid: out.__setitem__(pid, host.ensure(profile, pid)))
               for pid in (11, 12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert started == [700]
    assert (profile.run_dir / "host.pid").read_text() == "700"
    assert sorted(p.name for p in (profile.run_dir / "clients").iterdir()) == ["11", "12"]
    assert all(ports == {"tts": 28880, "stt": 22022} for ports, _ in out.values())


# ----- stop, and ensure elsewhere ---------------------------------------------------------------


@POSIX_ONLY
def test_stop_stops_the_host_before_the_services(tmp_path, monkeypatch):
    """FELIX: the host goes first, or it would start the services again."""
    profile = make_profile(tmp_path / "voice")
    profile.run_dir.mkdir(parents=True)
    (profile.run_dir / "host.pid").write_text("4242")
    (profile.run_dir / "host-starts.json").write_text("[1.0, 2.0]")   # a deliberate stop is not a crash
    host.atomic_json(profile.run_dir / "state.json", {"tts": {"pid": 51, "port": 28880, "marker": "tts"}})
    order = []
    monkeypatch.setattr(services, "owned", lambda entry: isinstance(entry, dict))
    monkeypatch.setattr(services, "stop_entry", lambda p, entry: order.append(entry["pid"]) or None)
    monkeypatch.setattr(services, "live_leases", lambda p: [])
    results = services.stop(profile)
    assert order == [4242, 51]
    assert not (profile.run_dir / "host.pid").exists() and not (profile.run_dir / "host-starts.json").exists()
    assert results[-1].code == "services_stopped" and results[-1].data["host"] is True


@POSIX_ONLY
def test_stop_keeps_the_host_record_when_it_will_not_die(tmp_path, monkeypatch):
    profile = make_profile(tmp_path / "voice")
    profile.run_dir.mkdir(parents=True)
    (profile.run_dir / "host.pid").write_text("4242")
    monkeypatch.setattr(services, "owned", lambda entry: isinstance(entry, dict))
    monkeypatch.setattr(services, "stop_entry", lambda p, entry: Result("kill_incomplete", "4242 lives"))
    monkeypatch.setattr(services, "live_leases", lambda p: [])
    results = services.stop(profile)
    assert "kill_incomplete" in codes(results) and (profile.run_dir / "host.pid").exists()


def test_elsewhere_ensure_starts_the_services_and_this_platforms_helpers(tmp_path, monkeypatch):
    profile = make_profile(tmp_path / "voice", helpers={
        "barge": HelperSpec(["b"], "b", "b.log", (), True),
        "ptt": HelperSpec(["p"], "p", "p.log", ("no-such-platform",), False)})
    monkeypatch.setattr(host, "WINDOWS", False)
    monkeypatch.setattr(host, "ensure_services", lambda p, pid, hooks=None: ({"tts": 1}, [Result("runtime_not_current")]))
    helper_calls = []
    monkeypatch.setattr(host, "start_helper", lambda p, kind, mode=None, hooks=None: helper_calls.append(kind) or Result(
        "helper_started", "", {"helper": kind, "results": [Result("kill_incomplete", "old one lives")]}))
    ports, results = host.ensure(profile, 11)
    assert ports == {"tts": 1} and helper_calls == ["barge"]
    assert codes(results) == ["runtime_not_current", "helper_started", "kill_incomplete"]


def test_main_needs_a_command_and_a_profile_file(tmp_path):
    assert host.main([]) == 2 and host.main(["host"]) == 2 and host.main(["nope", "x"]) == 2
    assert host.main(["host", str(tmp_path / "missing.json")]) == 2


# ----- fix round 1 ------------------------------------------------------------------------------


def _leases(monkeypatch, ticks):
    leases = iter([[11]] * ticks + [[]])
    monkeypatch.setattr(host, "live_leases", lambda profile: next(leases))


def _recording_start_helper(world, code="helper_started"):
    pids = iter(range(500, 600))

    def start_helper(profile, kind, mode, hooks=None):
        world.calls.append((kind, mode))
        if code == "helper_started":
            state = read_state(profile)
            pid = next(pids)
            world.live.add(pid)
            state[kind] = {"pid": pid, "port": None, "marker": kind, "mode": mode}
            host.atomic_json(profile.run_dir / "state.json", state)
        return Result(code, code, {"helper": kind, "results": []})

    return start_helper


def test_a_failing_read_mode_hook_leaves_the_helper_alone_and_is_said_once(tmp_path, monkeypatch):
    """Review, Task 7: a failed read_mode gave None, which is not the running mode, so the host
    restarted the helper in its default mode and flipped it back on the next good look; and
    hook_failed went to watch.log every 3 s."""
    world = _host_world(tmp_path, monkeypatch, helpers={
        "barge": HelperSpec(["b", "--mode={mode}"], "b", "b.log", (), True)})
    _leases(monkeypatch, 4)
    answers = iter(["auto", None, None, "auto"])

    def read_mode(profile, kind, hooks, results):
        answer = next(answers)
        if answer is None:
            results.append(Result("hook_failed", "read_mode: boom", {"hook": "read_mode"}))
        return answer

    monkeypatch.setattr(host, "read_mode", read_mode)
    monkeypatch.setattr(host, "start_helper", _recording_start_helper(world))
    host.host_main(world.file)
    assert [c for c in world.calls if isinstance(c, tuple)] == [("barge", "auto")]
    assert log_codes(world.profile).count("hook_failed") == 1


def test_read_mode_off_stops_the_helper(tmp_path, monkeypatch):
    """Ruling, Task 7: "off" from read_mode (any case) means the helper must not run."""
    world = _host_world(tmp_path, monkeypatch, helpers={
        "barge": HelperSpec(["b", "--mode={mode}"], "b", "b.log", (), True)})
    _leases(monkeypatch, 3)
    answers = iter(["auto", "OFF", "off"])
    monkeypatch.setattr(host, "read_mode", lambda profile, kind, hooks, results: next(answers))
    monkeypatch.setattr(host, "start_helper", _recording_start_helper(world))
    stops = []

    def stop_helper(profile, kind, hooks=None):
        stops.append(kind)
        state = read_state(profile)
        state.pop(kind, None)
        host.atomic_json(profile.run_dir / "state.json", state)
        return Result("helper_stopped", "stopped", {"helper": kind, "results": []})

    monkeypatch.setattr(host, "stop_helper", stop_helper)
    records = []
    real_atomic = host.atomic_json
    monkeypatch.setattr(host, "atomic_json", lambda path, value: (
        records.append(json.loads(json.dumps(value))) if Path(path).name == host.HELPERS_FILE else None,
        real_atomic(path, value)))
    host.host_main(world.file)
    assert [c for c in world.calls if isinstance(c, tuple)] == [("barge", "auto")]
    assert stops == ["barge"]   # once: the second "off" finds it stopped
    assert records[-1]["barge"]["state"] == "stopped"


def test_elsewhere_ensure_stops_a_helper_whose_mode_is_off(tmp_path, monkeypatch):
    profile = make_profile(tmp_path / "voice", helpers={"barge": HelperSpec(["b"], "b", "b.log", (), True)})
    monkeypatch.setattr(host, "WINDOWS", False)
    monkeypatch.setattr(host, "ensure_services", lambda p, pid, hooks=None: ({}, []))
    monkeypatch.setattr(host, "read_mode", lambda p, kind, hooks, results: "Off")
    monkeypatch.setattr(host, "start_helper", lambda *a, **kw: pytest.fail("started"))
    monkeypatch.setattr(host, "stop_helper", lambda p, kind, hooks=None: Result(
        "helper_not_running", "", {"helper": kind, "results": []}))
    assert codes(host.ensure(profile, 11)[1]) == ["helper_not_running"]


def test_a_helper_that_exits_at_once_is_retried_within_the_restart_limit(tmp_path, monkeypatch):
    world = _host_world(tmp_path, monkeypatch, helpers={
        "ptt": HelperSpec(["p"], "p", "p.log", (), False)}, restart_limit=(3, 300.0))
    _leases(monkeypatch, 8)
    monkeypatch.setattr(host, "start_helper", _recording_start_helper(world, code="helper_exited"))
    records = []
    real_atomic = host.atomic_json
    monkeypatch.setattr(host, "atomic_json", lambda path, value: (
        records.append(json.loads(json.dumps(value))) if Path(path).name == host.HELPERS_FILE else None,
        real_atomic(path, value)))
    host.host_main(world.file)
    assert world.calls.count(("ptt", None)) == 4   # the start and three retries
    assert log_codes(world.profile).count("helper_restart_limit_reached") == 1
    assert records[-1]["ptt"]["code"] == "helper_restart_limit_reached"
    assert records[-1]["ptt"]["state"] == "failed"


@POSIX_ONLY
def test_the_host_profile_file_is_readable_by_the_user_only(tmp_path, monkeypatch):
    """It holds profile.env."""
    profile = make_profile(tmp_path / "voice", env={"SECRET": "x"})
    monkeypatch.setattr(host.subprocess, "run", lambda argv, **kw: SimpleNamespace(stdout="0 5", stderr="", returncode=0))
    monkeypatch.setattr(host, "background_options", lambda: {})
    os.umask(0o022)
    assert host.start_host(profile).code == "host_started"
    assert (profile.run_dir / host.HOST_PROFILE).stat().st_mode & 0o777 == 0o600


def test_a_host_without_psutil_ends_at_once_and_keeps_the_leases(tmp_path, monkeypatch):
    world = _host_world(tmp_path, monkeypatch)
    monkeypatch.setattr(host, "require_psutil", lambda: Result("psutil_missing", "no psutil"))
    monkeypatch.setattr(host, "live_leases", lambda profile: pytest.fail("leases read without psutil"))
    assert host.host_main(world.file) == 2
    assert log_codes(world.profile) == ["psutil_missing"]


def test_a_host_command_not_run_by_the_profiles_python_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(host.subprocess, "run", lambda *a, **kw: pytest.fail("PowerShell was asked"))
    profile = make_profile(tmp_path / "voice", host_command=["/other/python", "voice.py", "host"])
    assert host.start_host(profile).code == "host_command_bad"


def test_a_host_started_after_powershell_timed_out_is_found_and_recorded(tmp_path, monkeypatch):
    profile = make_profile(tmp_path / "voice")
    monkeypatch.setattr(host, "background_options", lambda: {})
    monkeypatch.setattr(host, "process_command", lambda pid: None)

    def run(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw.get("timeout"))

    monkeypatch.setattr(host.subprocess, "run", run)
    profile_file = profile.run_dir / host.HOST_PROFILE
    listing = [(1, "init"), (4321, f"python voice.py host {profile_file}"), (4400, "python voice.py host /other/run/x.json")]
    monkeypatch.setattr(host, "running_commands", lambda: (listing, None))
    results = []
    assert host.ensure_host(profile, results) is True
    assert codes(results) == ["host_started_late"]
    assert (profile.run_dir / "host.pid").read_text() == "4321"
    listing[:] = [(1, "init")]
    assert host.start_host(profile).code == "host_start_refused"


def test_host_failed_removes_helpers_json(tmp_path, monkeypatch):
    world = _host_world(tmp_path, monkeypatch)
    _leases(monkeypatch, 3)
    looks = iter([None, RuntimeError("boom")])

    def services_look(profile, hooks, watch, results):
        problem = next(looks)
        if problem:
            raise problem

    monkeypatch.setattr(host, "_host_services", services_look)
    assert host.host_main(world.file) == 1
    assert not (world.root / "run" / host.HELPERS_FILE).exists()
    assert "host_failed" in log_codes(world.profile)


def test_host_starts_are_limited(tmp_path, monkeypatch):
    """A host that dies at once must not be started by every connector for ever."""
    profile = make_profile(tmp_path / "voice", restart_limit=(3, 300.0))
    profile.run_dir.mkdir(parents=True)
    monkeypatch.setattr(host, "process_command", lambda pid: None)   # each host died at once
    started = []
    monkeypatch.setattr(host, "start_host", lambda p: started.append(1) or Result(
        "host_started", "", {"pid": 900 + len(started)}))
    outcomes = []
    for _ in range(4):
        results = []
        outcomes.append((host.ensure_host(profile, results), codes(results)))
    assert len(started) == 3
    assert outcomes[-1] == (False, ["host_restart_limit"])


def test_a_failing_watchdog_says_so(tmp_path, monkeypatch):
    profile = make_profile(tmp_path / "voice")
    profile.run_dir.mkdir(parents=True)
    monkeypatch.setattr(host.time, "sleep", lambda s: None)
    monkeypatch.setattr(host, "watch_tick", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    assert host.watch_main(profile) == 1
    assert log_codes(profile) == ["watch_failed"]


# ----- fix round 2 ------------------------------------------------------------------------------


def test_clean_host_exits_do_not_count_towards_the_host_start_limit(tmp_path, monkeypatch):
    """Review, round 2: closing and reopening every chat three times in 300 s must not lock voice
    out. Only crashes and failed starts count; a host that ended because no connector was left
    resets the count."""
    world = _host_world(tmp_path, monkeypatch, restart_limit=(3, 300.0))
    monkeypatch.setattr(host, "process_command", lambda pid: None)   # the last host has ended
    started = []
    monkeypatch.setattr(host, "start_host", lambda p: started.append(1) or Result(
        "host_started", "", {"pid": 900 + len(started)}))
    for cycle in range(4):
        results = []
        assert host.ensure_host(world.profile, results) is True, (cycle, codes(results))
        _leases(monkeypatch, 1)
        assert host.host_main(world.file) == 0   # the chats closed: host_stopped
    assert len(started) == 4


def test_a_failing_read_mode_does_not_start_a_helper_the_user_turned_off(tmp_path, monkeypatch):
    world = _host_world(tmp_path, monkeypatch, helpers={
        "barge": HelperSpec(["b", "--mode={mode}"], "b", "b.log", (), True)})
    _leases(monkeypatch, 4)
    answers = iter(["auto", "off", None, None])

    def read_mode(profile, kind, hooks, results):
        answer = next(answers)
        if answer is None:
            results.append(Result("hook_failed", "read_mode: boom", {"hook": "read_mode"}))
        return answer

    monkeypatch.setattr(host, "read_mode", read_mode)
    monkeypatch.setattr(host, "start_helper", _recording_start_helper(world))
    stops = []

    def stop_helper(profile, kind, hooks=None):
        stops.append(kind)
        world.live.clear()
        return Result("helper_stopped", "stopped", {"helper": kind, "results": []})

    monkeypatch.setattr(host, "stop_helper", stop_helper)
    host.host_main(world.file)
    assert [c for c in world.calls if isinstance(c, tuple)] == [("barge", "auto")]
    assert stops == ["barge"] and log_codes(world.profile).count("hook_failed") == 1


@pytest.mark.parametrize("running", [True, False])
def test_elsewhere_a_failing_read_mode_leaves_the_helper_as_it_is(tmp_path, monkeypatch, running):
    """Review, round 2: each connector's ensure restarted a running helper in its default mode when
    the hook failed (the flip-flop of round 1, once per connector)."""
    profile = make_profile(tmp_path / "voice", helpers={"barge": HelperSpec(["b"], "b", "b.log", (), True)})
    profile.run_dir.mkdir(parents=True)
    host.atomic_json(profile.run_dir / "state.json", {"barge": {"pid": 5, "port": None, "marker": "b", "mode": "on"}})
    monkeypatch.setattr(host, "WINDOWS", False)
    monkeypatch.setattr(host, "owned", lambda entry: running and isinstance(entry, dict))
    monkeypatch.setattr(host, "ensure_services", lambda p, pid, hooks=None: ({}, []))
    monkeypatch.setattr(host, "read_mode", lambda p, kind, hooks, results: results.append(
        Result("hook_failed", "read_mode: boom")))
    monkeypatch.setattr(host, "start_helper", lambda *a, **kw: pytest.fail("started"))
    monkeypatch.setattr(host, "stop_helper", lambda *a, **kw: pytest.fail("stopped"))
    assert codes(host.ensure(profile, 11)[1]) == ["hook_failed"]
