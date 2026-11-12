"""vml_supervisor.services: ensure, release and stop the shared TTS and STT services.

Ported from FELIX 454f20d0e tests/repo/test_voicemode_setup.py (the `services` fixture and its
generic lifecycle tests). The fake world: real loopback HTTP processes standing in for Kokoro and
whisper.cpp on free ports, and a stand-in connector whose command carries the lease marker.

Renamed from FELIX (FELIX name -> ours):
  test_a_taken_felix_port_is_left_alone_and_the_next_one_is_used
      -> test_a_taken_port_is_left_alone_and_the_next_one_is_used
  test_stop_never_signals_a_process_that_is_not_felix_voice
      -> test_stop_never_signals_a_process_that_is_not_ours
  test_launch_stops_a_voice_server_that_ignores_the_stop_request (its services half only; launch is not here)
      -> test_stop_ends_a_service_that_ignores_the_stop_request
"""
import contextlib
import dataclasses
import io
import json
import os
import random
import signal
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from vml_supervisor import ports as ports_module
from vml_supervisor import services
from vml_supervisor.hooks import Hooks
from vml_supervisor.profile import HostProfile, PortOverride, ServiceSpec
from vml_supervisor.services import (check_service, ensure_services, release, stop, stop_entry,
                                     valid_response)

# These tests act out POSIX with fake programs; os.kill(pid, 0) would END a process on Windows.
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="acts out POSIX with fake programs")

FAKE_SERVICE = """
import json, os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
port = int(sys.argv[sys.argv.index("--port") + 1])
if "--dump" in sys.argv:
    with open(sys.argv[sys.argv.index("--dump") + 1], "w") as out:
        json.dump({"env": dict(os.environ), "cwd": os.getcwd()}, out)
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass
    def do_GET(self):
        body = json.dumps({"status": "ok", "data": [{"id": "kokoro-onnx"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
HTTPServer(("127.0.0.1", port), Handler).serve_forever()
"""


def _free(port):
    with contextlib.closing(socket.socket()) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def free_pair(avoid=()):
    """Two consecutive free loopback ports (a profile's port range is contiguous)."""
    for _ in range(500):
        port = random.randrange(30000, 60000)
        if not {port, port + 1} & set(avoid) and _free(port) and _free(port + 1):
            return [port, port + 1]
    raise RuntimeError("no two consecutive free ports")


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    with contextlib.suppress(ChildProcessError):  # a zombie child of ours: reap it
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            return False
    return True


def wait_dead(pid, timeout=10):
    deadline = time.monotonic() + timeout
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not alive(pid)


def codes(results):
    return [r.code for r in results]


def read_state(profile):
    path = profile.run_dir / "state.json"
    return json.loads(path.read_text()) if path.exists() else {}


def sleeper(*marker):
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)", *marker])


@pytest.fixture
def world(tmp_path):
    """Real loopback processes standing in for Kokoro and whisper.cpp, and a stand-in connector."""
    fake = tmp_path / "fake_service.py"
    fake.write_text(FAKE_SERVICE)
    tts = free_pair()
    stt = free_pair(avoid=tts)
    root = tmp_path / "voice root with spaces"
    runtime = root / "runtime"
    runtime.mkdir(parents=True)
    (runtime / ".complete.json").write_text("{}")
    profile = HostProfile(
        name="test", root=root, run_dir=root / "run", runtime_dir=runtime,
        python=__import__("pathlib").Path(sys.executable), working_dir=root / "work",
        env={},
        services={
            "tts": ServiceSpec([sys.executable, str(fake), "tts", "--port", "{port}"], f"{fake} tts",
                               (tts[0], tts[1]), "/v1/models", "kokoro"),
            "stt": ServiceSpec([sys.executable, str(fake), "stt", "--port", "{port}"], f"{fake} stt",
                               (stt[0], stt[1]), "/health", "whisper"),
        },
        helpers={}, reserved_ports=(), port_override=PortOverride("none"),
        start_timeout=30.0, stop_timeout=5.0, watch_interval=3.0, restart_limit=(3, 60.0),
        lease_markers=("voice.py launch",), voice_server_marker="voice-mode",
        host_command=[], host_marker="vml-test-host",
        watch_command=[sys.executable, "-c", "import time; time.sleep(600)", "vml-test-watch"],
        watch_marker="vml-test-watch", job_name="vml-test")
    # The lease belongs to a stand-in launcher whose command carries the lease marker, as on a real machine.
    connector = sleeper("voice.py", "launch")
    started = []
    try:
        yield SimpleNamespace(profile=profile, ports={"tts": tts, "stt": stt}, connector=connector,
                              started=started, fake=fake, tmp=tmp_path)
        state = read_state(profile) if (profile.run_dir / "state.json").exists() else {}
        watch = profile.run_dir / "watch.pid"
        pids = [e["pid"] for e in state.values() if isinstance(e, dict) and isinstance(e.get("pid"), int)
                and e["pid"] > 2] + started
        if watch.is_file():
            pids.append(int(watch.read_text()))
        for pid in pids:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
    finally:
        connector.kill()
        connector.wait()


# ----- readiness: real whisper and kokoro response shapes (FELIX 681) -------------------------


@pytest.mark.parametrize("stt,tts", [
    ({"status": "ok"}, {"data": [{"id": "kokoro-onnx"}]}),
    ({"status": "loading"}, {"data": [{"id": "kokoro-onnx"}]}),
    ({"status": "ok"}, {}),
    ({"status": "ok"}, {"data": []}),
    ([], {"data": [{"id": "kokoro-onnx"}]}),
    ({"status": "ok"}, {"data": [{"id": "text-only-llm"}]}),
])
def test_health_uses_real_whisper_and_kokoro_response_shapes(monkeypatch, stt, tts):
    def response(url):
        stream = io.BytesIO(json.dumps(stt if url.endswith("/health") else tts).encode())
        stream.status = 200
        return stream
    monkeypatch.setattr(services, "local_get", response)
    good = {"stt": stt == {"status": "ok"}, "tts": tts == {"data": [{"id": "kokoro-onnx"}]}}
    for kind, ok in good.items():
        assert check_service(kind, 1) is ok
        assert valid_response(kind, stt if kind == "stt" else tts) is ok


def test_readiness_ignores_proxy_settings(world, monkeypatch):
    profile = world.profile
    monkeypatch.setenv("http_proxy", "http://proxy.invalid:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    chosen, results = ensure_services(profile, world.connector.pid)
    assert results == [] and set(chosen) == {"tts", "stt"}
    assert check_service("tts", chosen["tts"]) and check_service("stt", chosen["stt"])
    release(profile, world.connector.pid)


# ----- own run folder, own ports, stop when the last connector exits (FELIX 699, 716) -----------


def test_services_use_own_run_folder_and_stop_with_the_last_connector(world):
    profile = world.profile
    chosen, results = ensure_services(profile, world.connector.pid)
    assert results == []
    assert chosen == {"tts": world.ports["tts"][0], "stt": world.ports["stt"][0]}
    state = read_state(profile)
    assert (profile.run_dir / "clients" / str(world.connector.pid)).exists()
    assert all(entry["log"] == str(profile.run_dir / f"{kind}.log") for kind, entry in state.items())
    for kind, entry in state.items():
        assert set(entry) == {"pid", "port", "marker", "log", "command", "runtime"}
        assert entry["runtime"] == str(profile.runtime_dir)
        assert entry["command"][-1] == str(chosen[kind])
        assert check_service(kind, chosen[kind])
    assert release(profile, world.connector.pid) == []
    assert all(wait_dead(entry["pid"]) for entry in state.values())
    assert read_state(profile) == {}
    assert list((profile.run_dir / "clients").iterdir()) == []


def test_services_stay_while_another_connector_runs(world):
    profile = world.profile
    ensure_services(profile, world.connector.pid)
    state = read_state(profile)
    other = sleeper("voice.py", "launch")
    world.started.append(other.pid)
    (profile.run_dir / "clients" / str(other.pid)).write_text("")
    release(profile, world.connector.pid)
    assert all(alive(entry["pid"]) for entry in state.values())
    other.kill()
    other.wait()
    # The other connector died without cleanup: its stale lease no longer keeps services alive.
    release(profile, world.connector.pid)
    assert all(wait_dead(entry["pid"]) for entry in state.values())


def test_a_taken_port_is_left_alone_and_the_next_one_is_used(world):
    profile = world.profile
    personal = socket.socket()
    personal.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    personal.bind(("0.0.0.0", world.ports["tts"][0]))
    personal.listen()
    try:
        chosen, results = ensure_services(profile, world.connector.pid)
        assert results == []
        assert chosen == {"tts": world.ports["tts"][1], "stt": world.ports["stt"][0]}
        release(profile, world.connector.pid)
        with contextlib.closing(socket.create_connection(("127.0.0.1", world.ports["tts"][0]), timeout=2)):
            pass  # The other program's listener still answers.
    finally:
        personal.close()


# ----- failures are Results, and what was started is stopped (FELIX 787, 904, 925, 945) --------


def test_failed_start_stops_what_it_started(world):
    spec = world.profile.services["stt"]
    broken = dataclasses.replace(spec, argv=[sys.executable, "-c", "import sys; sys.exit(3)", "--port", "{port}"],
                                 marker="never-matches")
    profile = dataclasses.replace(world.profile, start_timeout=0,
                                  services={**world.profile.services, "stt": broken})
    try:
        chosen, results = ensure_services(profile, world.connector.pid)
        assert "stt" not in chosen
        assert {"service_not_ready", "service_exited"} & set(codes(results))
    finally:
        state = read_state(profile)
        release(profile, world.connector.pid)
    assert wait_dead(state["tts"]["pid"])


def test_failure_before_the_second_service_starts_stops_the_first(world):
    profile = world.profile
    blockers = []
    for port in world.ports["stt"]:
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", port))
        blocker.listen()
        blockers.append(blocker)
    try:
        chosen, results = ensure_services(profile, world.connector.pid)
        no_port = [r for r in results if r.code == "no_free_port"]
        assert len(no_port) == 1 and no_port[0].data["service"] == "stt"
        assert "stt" not in chosen and "tts" in chosen
        tts = read_state(profile)["tts"]
        assert alive(tts["pid"])
        release(profile, world.connector.pid)
        assert wait_dead(tts["pid"])
    finally:
        for blocker in blockers:
            blocker.close()


def test_a_program_that_takes_the_port_during_startup_is_not_used(world, monkeypatch):
    profile = world.profile
    stranger = subprocess.Popen([sys.executable, str(world.fake), "stranger", "--port", str(world.ports["tts"][0])])
    world.started.append(stranger.pid)
    deadline = time.monotonic() + 10
    while not services.listener_pids(world.ports["tts"][0]) and time.monotonic() < deadline:
        time.sleep(0.05)
    # Simulate the race: the port looked free when it was chosen.
    monkeypatch.setattr(ports_module, "port_free", lambda port: True)
    try:
        chosen, results = ensure_services(profile, world.connector.pid)
        assert "tts" not in chosen
        assert {"owner_other", "service_exited"} & set(codes(results))
    finally:
        release(profile, world.connector.pid)
    assert stranger.poll() is None


def test_an_empty_port_lookup_is_not_reported_as_another_program(world, monkeypatch):
    profile = world.profile
    monkeypatch.setattr(services, "listener_pids", lambda port: set())
    try:
        chosen, results = ensure_services(profile, world.connector.pid)
    finally:
        release(profile, world.connector.pid)
    assert chosen == {}
    unknown = [r for r in results if r.code == "owner_unknown"]
    assert len(unknown) == 2 and all(r.data["listeners"] == [] for r in unknown)
    assert "owner_other" not in codes(results)


# ----- stopping (FELIX 975's stop half, 1030, and the stranger rule) --------------------------


def test_services_stop_on_the_first_request(world):
    profile = world.profile
    ensure_services(profile, world.connector.pid)
    state = read_state(profile)
    begin = time.monotonic()
    release(profile, world.connector.pid)
    # Services started while stop signals were held must still obey SIGTERM, not wait for SIGKILL.
    assert time.monotonic() - begin < profile.stop_timeout
    assert all(wait_dead(entry["pid"], timeout=2) for entry in state.values())


def test_stop_ends_a_service_that_ignores_the_stop_request(world):
    """FELIX 975 (launch's stubborn voice server) at the services layer: SIGKILL after stop_timeout."""
    profile = dataclasses.replace(world.profile, stop_timeout=1.0)
    stubborn = subprocess.Popen([sys.executable, "-c", "import signal, sys, time; "
                                 "signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); "
                                 "time.sleep(60)", "stubborn-service"],
                                stdout=subprocess.PIPE, text=True, start_new_session=True)
    world.started.append(stubborn.pid)
    assert stubborn.stdout.readline().strip() == "ready"
    profile.run_dir.mkdir(parents=True)
    (profile.run_dir / "state.json").write_text(json.dumps(
        {"tts": {"pid": stubborn.pid, "port": None, "marker": "stubborn-service", "log": "x"}}))
    begin = time.monotonic()
    results = stop(profile)
    assert time.monotonic() - begin < 8
    assert stubborn.wait(timeout=5) == -signal.SIGKILL
    assert read_state(profile) == {}
    assert [r for r in results if r.code == "services_stopped"][0].data["services"] == ["tts"]


def test_stop_never_signals_a_process_that_is_not_ours(world):
    profile = world.profile
    stranger = sleeper()
    world.started.append(stranger.pid)
    profile.run_dir.mkdir(parents=True)
    (profile.run_dir / "state.json").write_text(json.dumps(
        {"tts": {"pid": stranger.pid, "port": world.ports["tts"][0], "marker": "kokoro_onnx_server.py",
                 "log": "x"}}))
    results = stop(profile)
    time.sleep(0.2)
    assert stranger.poll() is None
    assert [r for r in results if r.code == "services_stopped"][0].data["services"] == []


def test_stop_ends_everything_whatever_leases_remain(world):
    profile = world.profile
    ensure_services(profile, world.connector.pid)
    state = read_state(profile)
    results = stop(profile)
    stopped = [r for r in results if r.code == "services_stopped"][0]
    assert sorted(stopped.data["services"]) == ["stt", "tts"] and stopped.data["connectors"] == 1
    assert all(wait_dead(entry["pid"]) for entry in state.values())
    assert read_state(profile) == {}


def test_stop_entry_leaves_garbage_alone(world):
    for entry in (None, [], {"pid": True, "marker": "x"}, {"pid": -1, "marker": "x"}, {"pid": "1"}):
        assert stop_entry(world.profile, entry) is None


# ----- ports (FELIX's taken guard, with ours' isinstance guard) --------------------------------


def test_a_port_chosen_for_one_service_is_not_chosen_for_the_other(world, monkeypatch):
    override = world.profile.root / "ports.json"
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text(json.dumps({"tts": world.ports["stt"][0]}))
    profile = dataclasses.replace(world.profile, port_override=PortOverride("file", override))
    monkeypatch.setattr(ports_module, "port_free", lambda port: True)  # TTS has not bound it yet.
    chosen, _ = ensure_services(profile, world.connector.pid)
    assert chosen.get("tts") == world.ports["stt"][0]
    assert read_state(profile)["stt"]["port"] == world.ports["stt"][1]
    release(profile, world.connector.pid)


def test_a_corrupt_state_is_a_result_and_read_as_empty(world):
    profile = world.profile
    profile.run_dir.mkdir(parents=True)
    (profile.run_dir / "state.json").write_text('{"tts": 5, "stt": "x", "junk": ')
    chosen, results = ensure_services(profile, world.connector.pid)
    assert codes(results) == ["state_corrupt"]
    assert set(chosen) == {"tts", "stt"}
    release(profile, world.connector.pid)


def test_a_state_holding_non_entries_is_not_a_crash(world):
    profile = world.profile
    profile.run_dir.mkdir(parents=True)
    (profile.run_dir / "state.json").write_text(json.dumps({"tts": 5, "stt": [1], "old": "x"}))
    chosen, results = ensure_services(profile, world.connector.pid)
    assert results == [] and set(chosen) == {"tts", "stt"}
    release(profile, world.connector.pid)


# ----- ours merged in: the runtime guard, the record failure, the watchdog, hooks, env -----------


def test_a_replaced_runtime_starts_nothing(world, monkeypatch):
    profile = world.profile
    (profile.runtime_dir / ".complete.json").unlink()
    spawned = []
    monkeypatch.setattr(services, "spawn", lambda *a, **k: spawned.append(a))
    chosen, results = ensure_services(profile, world.connector.pid)
    assert chosen == {} and codes(results) == ["runtime_incomplete"] and spawned == []
    assert (profile.run_dir / "clients" / str(world.connector.pid)).exists()  # the lease came first
    release(profile, world.connector.pid)


def test_a_runtime_that_is_not_current_is_a_result(world):
    profile = world.profile
    hooks = Hooks({"resolve_runtime": lambda p: [str(profile.runtime_dir), False]})
    chosen, results = ensure_services(profile, world.connector.pid, hooks)
    assert codes(results) == ["runtime_not_current"] and set(chosen) == {"tts", "stt"}
    release(profile, world.connector.pid)


def test_a_service_that_cannot_be_recorded_is_killed(world, monkeypatch):
    profile = world.profile
    real_spawn = services.spawn
    spawned = []

    def spawn(*args, **kwargs):
        process, result = real_spawn(*args, **kwargs)
        spawned.append(process)
        return process, result

    def broken(path, value):
        raise OSError("disk full")

    monkeypatch.setattr(services, "spawn", spawn)
    monkeypatch.setattr(services, "atomic_json", broken)
    chosen, results = ensure_services(profile, world.connector.pid)
    assert chosen == {}
    assert codes(results).count("state_write_failed") == 2
    assert spawned and all(p.wait(timeout=10) is not None for p in spawned)


def test_the_watchdog_is_started_once_and_carries_its_marker(world):
    profile = world.profile
    ensure_services(profile, world.connector.pid)
    watcher = int((profile.run_dir / "watch.pid").read_text())
    assert alive(watcher)
    assert profile.watch_marker in services.process_command(watcher)
    ensure_services(profile, world.connector.pid)
    assert int((profile.run_dir / "watch.pid").read_text()) == watcher
    release(profile, world.connector.pid)


def test_services_use_the_hooks_command_and_only_the_hooks_environment(world, monkeypatch):
    profile = world.profile
    dump = world.tmp / "stt-env.json"
    monkeypatch.setenv("VML_TEST_SECRET", "must-not-travel")

    def command(p, kind, runtime, port):
        argv = [sys.executable, str(world.fake), kind, "--port", str(port)]
        return [argv + (["--dump", str(dump)] if kind == "stt" else []), f"{world.fake} {kind}"]

    seen = []

    def environment(p, runtime, ports, modes):
        seen.append((runtime, ports))
        return {"FROM_HOOK": "1"}

    hooks = Hooks({"service_command": command, "environment": environment})
    chosen, results = ensure_services(profile, world.connector.pid, hooks)
    assert results == [] and set(chosen) == {"tts", "stt"}
    assert seen == [(str(profile.runtime_dir), chosen)]
    child = json.loads(dump.read_text())
    assert child["env"]["FROM_HOOK"] == "1"
    assert "VML_TEST_SECRET" not in child["env"]
    assert child["env"]["PATH"] == os.environ["PATH"]
    release(profile, world.connector.pid)


def test_a_failing_hook_is_a_result_and_the_default_is_used(world):
    profile = world.profile

    def broken(*args):
        raise RuntimeError("boom")

    hooks = Hooks({"service_command": broken})
    chosen, results = ensure_services(profile, world.connector.pid, hooks)
    assert set(chosen) == {"tts", "stt"}
    assert codes(results) == ["hook_failed", "hook_failed"]
    release(profile, world.connector.pid)


# ----- Review Focus 5: children start in the profile's working folder --------------------------


def test_children_start_in_the_profiles_working_dir_not_the_callers(world, monkeypatch):
    caller = world.tmp / "customer repo"
    caller.mkdir()
    planted = world.tmp / "planted-ran"
    evil = caller / "whisper-server"
    evil.write_text(f"#!/bin/sh\ntouch '{planted}'\nexit 7\n")
    evil.chmod(0o755)
    tools = world.tmp / "bin"
    tools.mkdir()
    dump = world.tmp / "stt-env.json"
    real = tools / "whisper-server"
    real.write_text(f"#!{sys.executable}\n" + FAKE_SERVICE)
    real.chmod(0o755)
    # A relative PATH entry is searched from the child's working folder, as Windows searches the
    # current folder first: started from the caller's folder, the planted program would run.
    spec = dataclasses.replace(world.profile.services["stt"], marker="whisper-server",
                               argv=["whisper-server", "--dump", str(dump), "--port", "{port}"])
    profile = dataclasses.replace(world.profile, env={"PATH": os.pathsep.join([".", str(tools)])},
                                  services={**world.profile.services, "stt": spec})
    monkeypatch.chdir(caller)
    assert not profile.working_dir.exists()
    chosen, results = ensure_services(profile, world.connector.pid)
    assert not planted.exists(), "the program planted in the caller's folder ran"
    assert results == [] and set(chosen) == {"tts", "stt"}
    assert json.loads(dump.read_text())["cwd"] == str(profile.working_dir)
    release(profile, world.connector.pid)


# ----- Review Focus 2: two connectors at the same moment --------------------------------------


def test_two_connectors_at_once_start_one_tts_and_one_stt(world, monkeypatch):
    profile = world.profile
    second = sleeper("voice.py", "launch")
    world.started.append(second.pid)
    real_spawn = services.spawn
    spawned = []

    def spawn(argv, *args, **kwargs):
        process, result = real_spawn(argv, *args, **kwargs)
        world.started.append(process.pid)
        if str(world.fake) in argv:
            spawned.append(process)
        return process, result

    monkeypatch.setattr(services, "spawn", spawn)
    out = {}
    barrier = threading.Barrier(2)

    def connect(pid):
        barrier.wait()
        out[pid] = ensure_services(profile, pid)

    threads = [threading.Thread(target=connect, args=(pid,)) for pid in (world.connector.pid, second.pid)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert len(out) == 2
    (a_ports, a_results), (b_ports, b_results) = out.values()
    assert a_results == [] and b_results == []
    assert a_ports == b_ports and set(a_ports) == {"tts", "stt"}
    assert len(spawned) == 2
    leases = sorted(p.name for p in (profile.run_dir / "clients").iterdir())
    assert leases == sorted([str(world.connector.pid), str(second.pid)])
    release(profile, world.connector.pid)
    release(profile, second.pid)


# ----- FELIX 454f20d0e: on Windows the connector starts the host, not the services --------------


def test_on_windows_a_connector_starts_the_host_not_the_services(world, monkeypatch):
    profile = world.profile
    monkeypatch.setattr(services, "WINDOWS", True)
    monkeypatch.setattr(services, "require_psutil", lambda: None)
    hosts, started = [], []
    monkeypatch.setattr(services, "ensure_host", lambda p, h, results: hosts.append(p) or True)
    monkeypatch.setattr(services, "start_missing_services", lambda *a: started.append(a))
    monkeypatch.setattr(services, "wait_until_ready", lambda p, started=None, kinds=None: ({"tts": 1, "stt": 2}, []))
    assert ensure_services(profile, world.connector.pid) == ({"tts": 1, "stt": 2}, [])
    assert hosts == [profile] and started == []
    assert (profile.run_dir / "clients" / str(world.connector.pid)).exists()  # the lease comes first
    assert not (profile.run_dir / "watch.pid").exists()  # the host, not a watchdog, supervises


def test_on_windows_a_host_that_cannot_start_starts_nothing_and_says_so(world, monkeypatch):
    profile = world.profile
    monkeypatch.setattr(services, "WINDOWS", True)
    monkeypatch.setattr(services, "require_psutil", lambda: None)
    monkeypatch.setattr(services, "start_missing_services", lambda *a: pytest.fail("started services"))
    monkeypatch.setattr(services, "ensure_host", lambda p, h, results: results.append(
        services.Result("host_start_refused", "9 0")) and False)
    chosen, results = ensure_services(profile, world.connector.pid)
    assert chosen == {} and codes(results) == ["host_start_refused"]


def test_wait_until_ready_waits_for_a_service_another_process_is_starting(world):
    """The host may record a service after the connector began waiting: state.json is read at every look."""
    profile = world.profile
    port = world.ports["tts"][0]
    service = subprocess.Popen([sys.executable, str(world.fake), "tts", "--port", str(port)], start_new_session=True)
    world.started.append(service.pid)
    profile.run_dir.mkdir(parents=True)

    def record_later():
        time.sleep(1.0)
        (profile.run_dir / "state.json").write_text(json.dumps(
            {"tts": {"pid": service.pid, "port": port, "marker": f"{world.fake} tts", "log": "x"}}))

    writer = threading.Thread(target=record_later)
    writer.start()
    ready, results = services.wait_until_ready(profile, kinds=["tts"])
    writer.join()
    assert ready == {"tts": port} and results == []


# ----- fix round 1: markers, the watchdog record, Windows env keys, odd ports, owned commands ----


def _command_hook(argv_for, marker):
    return Hooks({"service_command": lambda p, kind, runtime, port: [argv_for(kind, port), marker(kind)]})


def test_an_empty_marker_from_the_command_hook_starts_nothing(world, monkeypatch):
    spawned = []
    monkeypatch.setattr(services, "spawn", lambda *a, **k: spawned.append(a))
    hooks = _command_hook(lambda kind, port: [sys.executable, str(world.fake), kind, "--port", str(port)],
                          lambda kind: "")
    chosen, results = ensure_services(world.profile, world.connector.pid, hooks)
    assert chosen == {} and spawned == []
    assert codes(results) == ["service_command_bad", "service_command_bad"]
    release(world.profile, world.connector.pid)


@pytest.mark.parametrize("style", ["--port=N", "-p N", "marker missing"])
def test_a_command_that_owned_could_never_match_starts_nothing(world, monkeypatch, style):
    spawned = []
    monkeypatch.setattr(services, "spawn", lambda *a, **k: spawned.append(a))

    def argv(kind, port):
        base = [sys.executable, str(world.fake), kind]
        if style == "--port=N":
            return base + [f"--port={port}"]
        if style == "-p N":
            return base + ["-p", str(port)]
        return [sys.executable, "elsewhere.py", "--port", str(port)]

    hooks = _command_hook(argv, lambda kind: f"{world.fake} {kind}")
    chosen, results = ensure_services(world.profile, world.connector.pid, hooks)
    assert chosen == {} and spawned == []
    assert codes(results) == ["service_command_unowned", "service_command_unowned"]
    release(world.profile, world.connector.pid)


def test_an_empty_watch_marker_is_a_result_not_a_match_for_any_process(world):
    profile = dataclasses.replace(world.profile, watch_marker="")
    stranger = sleeper()
    world.started.append(stranger.pid)
    profile.run_dir.mkdir(parents=True)
    (profile.run_dir / "watch.pid").write_text(str(stranger.pid))
    chosen, results = ensure_services(profile, world.connector.pid)
    assert "watch_marker_empty" in codes(results)
    assert set(chosen) == {"tts", "stt"}
    release(profile, world.connector.pid)


def test_a_watchdog_that_cannot_be_recorded_is_stopped(world, monkeypatch):
    profile = world.profile
    real_popen = subprocess.Popen
    launched = []

    def popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        launched.append(process)
        return process

    monkeypatch.setattr(services.subprocess, "Popen", popen)
    profile.run_dir.mkdir(parents=True)
    (profile.run_dir / "watch.pid").mkdir()  # writing the pid file fails
    chosen, results = ensure_services(profile, world.connector.pid)
    assert "watchdog_start_failed" in codes(results)
    watchdogs = [p for p in launched if profile.watch_marker in p.args]
    assert len(watchdogs) == 1 and watchdogs[0].wait(timeout=10) is not None
    release(profile, world.connector.pid)


def test_on_windows_a_hook_key_replaces_the_os_key_whatever_its_case(monkeypatch):
    monkeypatch.setattr(services, "WINDOWS", True)
    monkeypatch.setattr(services.os, "environ", {"Path": "C:/Windows", "SystemRoot": "C:/Windows",
                                                 "SECRET": "x"})
    env = services.service_environment({"PATH": "C:/runtime", "no_proxy": "example"})
    assert [k for k in env if k.upper() == "PATH"] == ["PATH"] and env["PATH"] == "C:/runtime"
    assert [k for k in env if k.upper() == "NO_PROXY"] == ["no_proxy"] and env["no_proxy"] == "example"
    assert env["SYSTEMROOT"] == "C:/Windows" and "SECRET" not in env


def test_on_windows_the_proxy_exemption_is_one_key(monkeypatch):
    monkeypatch.setattr(services, "WINDOWS", True)
    monkeypatch.setattr(services.os, "environ", {"PATH": "C:/Windows"})
    env = services.service_environment({})
    assert [k for k in env if k.upper() == "NO_PROXY"] == ["NO_PROXY"]


def test_a_recorded_port_that_is_not_a_number_is_not_a_crash(world):
    profile = world.profile
    profile.run_dir.mkdir(parents=True)
    (profile.run_dir / "state.json").write_text(json.dumps(
        {"old": {"pid": 1, "port": [1]}, "other": {"pid": 2, "port": {"a": 1}}, "flag": {"port": True}}))
    chosen, results = ensure_services(profile, world.connector.pid)
    assert results == [] and set(chosen) == {"tts", "stt"}
    release(profile, world.connector.pid)
