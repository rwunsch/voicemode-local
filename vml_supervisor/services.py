"""The shared speech services: start TTS and STT once for every connector, lease them, stop them
with the last one.

Moved from FELIX voice.py (454f20d0e: ensure_services, start_missing_services, wait_until_ready,
release, stop, stop_recorded, stop_entry, check_service, valid_response and its proxy-free opener;
the Windows host itself is Task 7, behind the ensure_host seam), with voicemode_native.py's additions
merged in: the runtime/.complete.json guard, the isinstance guard on the ports already taken, the
services' Job Object, and the command and runtime kept in each record.

Nothing here raises for an expected failure; each one is a Result in the returned list.

Result codes
- from ensure_services: hook_failed, hook_bad_return (a hook; its default was used),
  psutil_missing (Windows), runtime_not_current (resolve_runtime said so; services still start),
  runtime_incomplete (runtime/.complete.json is gone: nothing is started), host_required
  (Windows, until Task 7 fills the ensure_host seam: nothing is started), lease_write_failed
  (nothing is started), working_dir_unavailable (nothing is started), job_unavailable (Windows:
  services start without the job), watchdog_not_configured, watch_marker_empty,
  watchdog_start_failed (POSIX; a watchdog whose pid could not be recorded is stopped),
  state_corrupt (read as empty, under the lock), port_override_invalid, unknown_service,
  no_free_port, service_command_bad (empty marker: not started), service_command_unowned (the
  marker or `--port N` is missing from the command, so it could never be owned: not started),
  breakaway_refused, job_assign_failed, resume_failed, service_start_failed,
  state_write_failed (the new service was killed), kill_incomplete, service_exited,
  service_not_ready, owner_other (another process answers on the port), owner_unknown (the
  answer came, but no listener could be found to prove it is ours).
- from release: lease_remove_failed, state_corrupt, kill_incomplete, psutil_missing,
  state_write_failed.
- from stop: the same as release, then services_stopped (data: the services that were running,
  and how many connectors still hold a lease).
"""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from . import hooks as hook_calls
from .hooks import Hooks, load_hooks
from .ports import choose_port, port_candidates
from .procs import (LOOPBACK, WINDOWS, background_options, kill_tree, listener_pids, owned,
                    process_command, process_tree, require_psutil, stop_signals_held,
                    unblock_stop_signals)
from .profile import HostProfile
from .result import Result
from .state import atomic_json, live_leases, read_json, service_lock, write_lease
from .winjob import SpawnError, services_job, spawn

# Readiness checks talk to loopback only: never through a proxy from the environment.
_local_opener = build_opener(ProxyHandler({}))
READY_POLL = 0.5

# The operating system's own variables a service needs; everything else comes from the profile.
_OS_KEEP = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TZ")
_WINDOWS_KEEP = ("PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP",
                 "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "PROGRAMDATA", "LOCALAPPDATA")
LOCAL_HOSTS = "127.0.0.1,localhost,::1"

_KOKORO = ("tts", "kokoro")
_WHISPER = ("stt", "whisper")


# ----- readiness ------------------------------------------------------------------------------


def valid_response(kind, data) -> bool:
    """True for Kokoro's model list naming kokoro-onnx (tts) or whisper.cpp's {"status": "ok"} (stt)."""
    if kind in _KOKORO:
        return isinstance(data, dict) and isinstance(data.get("data"), list) and any(
            isinstance(model, dict) and model.get("id") == "kokoro-onnx" for model in data["data"])
    return isinstance(data, dict) and data.get("status") == "ok"


def local_get(address):
    return _local_opener.open(address, timeout=2)


def check_service(kind, port, path=None) -> bool:
    """True when the service on 127.0.0.1:port answers its readiness path with a valid shape.
    kind is tts/kokoro or stt/whisper; path defaults to FELIX's (/v1/models, /health)."""
    if path is None:
        path = "/v1/models" if kind in _KOKORO else "/health"
    address = f"http://{LOOPBACK}:{port}{path}"
    try:
        response = local_get(address)
        with response:
            return response.status == 200 and valid_response(kind, json.load(response))
    except (URLError, OSError, ValueError):
        return False


# ----- stopping ------------------------------------------------------------------------------


def stop_entry(profile: HostProfile, entry) -> Result | None:
    """Stop one recorded process and its tree; None once it is gone. An entry that is not ours (it
    exited, or its pid now belongs to another program) is left alone."""
    if not owned(entry):
        return None
    return kill_tree(entry["pid"], profile.stop_timeout)


def stop_recorded(profile: HostProfile, state: dict) -> list[Result]:
    """Stop every recorded service and write the emptied state. The caller holds the lock."""
    results = []
    for kind in list(state):
        failed = stop_entry(profile, state.pop(kind))
        if failed is not None:
            results.append(failed)
    try:
        atomic_json(Path(profile.run_dir) / "state.json", state)
    except OSError as error:
        results.append(Result("state_write_failed", f"{profile.run_dir / 'state.json'}: {error}"))
    return results


def _read_state(profile: HostProfile, results: list) -> dict:
    """state.json under the lock; a corrupt or non-object file is a Result and read as empty."""
    path = Path(profile.run_dir) / "state.json"
    state, problem = read_json(path, {})
    if problem is not None:
        results.append(problem)
        return {}
    if not isinstance(state, dict):
        results.append(Result("state_corrupt", f"{path}: not a JSON object"))
        return {}
    return state


def _remove_lease(profile: HostProfile, connector_pid: int, results: list) -> None:
    lease = Path(profile.run_dir) / "clients" / str(connector_pid)
    try:
        lease.unlink(missing_ok=True)
    except OSError as error:
        results.append(Result("lease_remove_failed", f"{lease}: {error}", {"pid": connector_pid}))


def release(profile: HostProfile, connector_pid: int) -> list[Result]:
    """Drop this connector's lease; stop the services when no connector is left."""
    results: list[Result] = []
    missing = require_psutil()
    if missing is not None:
        results.append(missing)
    with service_lock(profile.run_dir):
        _remove_lease(profile, connector_pid, results)
        if not live_leases(profile):
            results.extend(stop_recorded(profile, _read_state(profile, results)))
    return results


def stop(profile: HostProfile) -> list[Result]:
    """Stop every recorded service, whatever leases remain."""
    results: list[Result] = []
    missing = require_psutil()
    if missing is not None:
        results.append(missing)
    with service_lock(profile.run_dir):
        others = live_leases(profile)
        state = _read_state(profile, results)
        running = [kind for kind in state if owned(state[kind])]
        results.extend(stop_recorded(profile, state))
    results.append(Result("services_stopped", ", ".join(running),
                          {"services": running, "connectors": len(others)}))
    return results


# ----- starting ------------------------------------------------------------------------------


def service_environment(env: dict) -> dict:
    """The environment a service runs with: the profile's (or its hook's) variables over the few the
    operating system needs. Nothing else of this process's environment travels."""
    keep = _WINDOWS_KEEP if WINDOWS else _OS_KEEP
    if WINDOWS:
        # Windows names are case-insensitive: one key per name, and a hook's key replaces the
        # OS key whatever its case (a hook's "Path" must not become a second PATH).
        upper = {key.upper(): value for key, value in os.environ.items()}
        base = {key: upper[key] for key in keep if key in upper}
        base["NO_PROXY"] = LOCAL_HOSTS
        for key, value in env.items():
            for old in [k for k in base if k.upper() == key.upper()]:
                del base[old]
            base[key] = value
        return base
    base = {key: os.environ[key] for key in keep if key in os.environ}
    base.setdefault("NO_PROXY", LOCAL_HOSTS)
    base.setdefault("no_proxy", LOCAL_HOSTS)
    base.update(env)
    return base


def _start_watchdog(profile: HostProfile, working_dir: Path, results: list) -> None:
    """POSIX: start (once) the process that stops the services when every connector is gone. On
    Windows the host (Task 7) holds that role."""
    if WINDOWS:
        return
    if not profile.watch_command:
        results.append(Result("watchdog_not_configured", "the profile names no watch_command"))
        return
    if not profile.watch_marker:
        # An empty marker is in every command line: any live process at watch.pid would count.
        results.append(Result("watch_marker_empty", "the profile's watch_marker is empty; no watchdog "
                              "was started"))
        return
    pid_file = Path(profile.run_dir) / "watch.pid"
    with contextlib.suppress(ValueError, OSError):
        command = process_command(int(pid_file.read_text()))
        if command and profile.watch_marker in command:
            return
    try:
        process = subprocess.Popen(list(profile.watch_command), cwd=str(working_dir), env=dict(os.environ),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, **background_options())
    except OSError as error:
        results.append(Result("watchdog_start_failed", f"{profile.watch_command}: {error}"))
        return
    try:
        pid_file.write_text(str(process.pid))
    except OSError as error:
        # Untracked, it would run beside the next connector's watchdog: stop it.
        failed = kill_tree(process.pid, profile.stop_timeout)
        if failed is not None:
            results.append(failed)
        results.append(Result("watchdog_start_failed", f"{pid_file}: {error}; watchdog {process.pid} "
                              "was stopped", {"pid": process.pid}))


def _choose(profile: HostProfile, kind: str, state: dict, results: list) -> int | None:
    candidates, problem = port_candidates(profile, kind)
    if problem is not None:
        results.append(problem)
    if not candidates:
        return None
    # A service still loading has not bound its port yet: never pick it twice.
    taken = {entry["port"] for entry in state.values() if isinstance(entry, dict)
             and isinstance(entry.get("port"), int) and not isinstance(entry["port"], bool)}
    port = choose_port(profile, kind, taken)
    if port is None:
        results.append(Result("no_free_port", f"{kind}: tried {candidates[0]} to {candidates[-1]}",
                              {"service": kind, "first": candidates[0], "last": candidates[-1]}))
    return port


def _unownable(command: list, marker: str, port: int):
    """(code, detail) when procs.owned could never match this service, or could match any process:
    the marker must be non-empty and in the command, and the command must hold `--port N` as two
    arguments. None when it is fine."""
    if not marker:
        return "service_command_bad", "the marker is empty, which would match any process"
    if marker not in " ".join(command):
        return "service_command_unowned", f"the marker {marker!r} is not in the command"
    if not any(a == "--port" and b == str(port) for a, b in zip(command, command[1:])):
        return "service_command_unowned", f"the command does not hold '--port {port}' as two arguments"
    return None


def _start(profile, hooks, kind, port, runtime, env, working_dir, job, state, results):
    """Start one service and record it. The caller holds the lock. Returns the process or None."""
    run_dir = Path(profile.run_dir)
    (command, marker), problem = hook_calls.service_command(hooks, profile, kind, runtime, port)
    if problem is not None:
        results.append(problem)
    unusable = _unownable(command, marker, port)
    if unusable is not None:
        results.append(Result(*unusable, {"service": kind, "command": list(command), "marker": marker}))
        return None
    log = run_dir / f"{kind}.log"
    extra = {} if WINDOWS else {"preexec_fn": unblock_stop_signals}
    with stop_signals_held():
        try:
            with log.open("a") as output:
                process, spawned = spawn(command, job, env=env, cwd=str(working_dir), stdin=subprocess.DEVNULL,
                                         stdout=output, stderr=output, **background_options(), **extra)
        except SpawnError as error:
            results.append(error.result)
            return None
        except OSError as error:
            results.append(Result("service_start_failed", f"{kind}: {command}: {error}",
                                  {"service": kind, "log": str(log)}))
            return None
        if spawned.code not in ("no_job", "breakaway"):
            results.append(spawned)
        state[kind] = {"pid": process.pid, "port": port, "marker": marker, "log": str(log),
                       "command": [str(part) for part in command], "runtime": runtime}
        try:
            atomic_json(run_dir / "state.json", state)
        except BaseException as error:
            # On Windows a venv's python.exe starts the real interpreter as a child: stop the tree.
            state.pop(kind, None)
            failed = kill_tree(process.pid, profile.stop_timeout)
            if failed is not None:
                results.append(failed)
            if not isinstance(error, Exception):
                raise
            results.append(Result("state_write_failed", f"{kind}: {error}; process {process.pid} was stopped",
                                  {"service": kind, "pid": process.pid}))
            return None
    return process


def start_missing_services(profile: HostProfile, hooks: Hooks, runtime: str, state: dict, started: dict,
                           results: list) -> set[str]:
    """Start the profile's services that are not running, as children of this process, and record
    them in state (and state.json). The caller holds the service lock. Returns the services that
    could not be started (each with its Result); the running and started ones are in state."""
    working_dir = Path(profile.working_dir)
    try:
        working_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        results.append(Result("working_dir_unavailable", f"{working_dir}: {error}"))
        return set(profile.services)
    job = None
    if WINDOWS:
        try:
            job = services_job(profile)
        except OSError as error:
            results.append(Result("job_unavailable", f"{profile.job_name}: {error}; services start "
                                  "without the job"))
    failed: set[str] = set()
    ports: dict[str, int] = {}
    wanted = []
    for kind in profile.services:
        if owned(state.get(kind)):
            ports[kind] = state[kind]["port"]
            continue
        state.pop(kind, None)
        port = _choose(profile, kind, {**state, **{k: {"port": p} for k, p in ports.items()}}, results)
        if port is None:
            failed.add(kind)
        else:
            ports[kind] = port
            wanted.append(kind)
    if wanted:
        env, problem = hook_calls.environment(hooks, profile, runtime, dict(ports), {})
        if problem is not None:
            results.append(problem)
        env = service_environment(env)
        for kind in wanted:
            process = _start(profile, hooks, kind, ports[kind], runtime, env, working_dir, job, state, results)
            if process is None:
                failed.add(kind)
            else:
                started[kind] = process
    return failed


def wait_until_ready(profile: HostProfile, started: dict | None = None, deadline: float | None = None,
                     kinds=None) -> tuple[dict[str, int], list[Result]]:
    """Outside the lock: wait until each recorded service answers, from the process recorded, not
    from another program that took the port in the meantime. state.json is read again at every look,
    so a service another process (the Windows host) is still starting is waited for. Returns the
    ports of the services that are ready and ours, and a Result for each that is not."""
    started = started or {}
    deadline = time.monotonic() + profile.start_timeout if deadline is None else deadline
    run_dir = Path(profile.run_dir)
    ready: dict[str, int] = {}
    results: list[Result] = []
    for kind in (profile.services if kinds is None else kinds):
        spec = profile.services[kind]
        log = str(run_dir / f"{kind}.log")
        while True:
            state, _ = read_json(run_dir / "state.json", {})
            entry = state.get(kind) if isinstance(state, dict) else None
            usable = isinstance(entry, dict) and isinstance(entry.get("port"), int) and isinstance(entry.get("pid"), int)
            if usable and check_service(spec.ready_kind, entry["port"], spec.ready_path):
                port, pid = entry["port"], entry["pid"]
                listeners = listener_pids(port)
                if not process_tree(pid) & listeners or not owned(entry):
                    # An empty lookup found nobody, which is not proof of another program.
                    code = "owner_other" if listeners else "owner_unknown"
                    results.append(Result(code, f"{kind} on {LOOPBACK}:{port}: recorded process {pid}, "
                                          f"listening {sorted(listeners)}",
                                          {"service": kind, "port": port, "pid": pid,
                                           "listeners": sorted(listeners)}))
                else:
                    ready[kind] = port
                break
            process = started.get(kind)
            if process is not None and process.poll() is not None:
                results.append(Result("service_exited", f"{kind} exited with {process.returncode}; see {log}",
                                      {"service": kind, "code": process.returncode, "log": log}))
                break
            if time.monotonic() >= deadline:
                where = f"on {LOOPBACK}:{entry['port']}" if usable else "(not started yet)"
                results.append(Result("service_not_ready", f"{kind} {where} did not answer {spec.ready_path} "
                                      f"in {profile.start_timeout} s; see {log}",
                                      {"service": kind, "port": entry["port"] if usable else None, "log": log}))
                break
            time.sleep(READY_POLL)
    return ready, results


def ensure_host(profile: HostProfile, hooks: Hooks) -> Result | None:
    """Seam for Task 7. On Windows the services belong to a host started outside every chat (FELIX
    454f20d0e ensure_host): the app ends a chat's processes with its job. Until the host is moved in,
    this returns Result("host_required") and ensure_services starts nothing on Windows. Called under
    the service lock, after the lease is written; None means the host runs."""
    return Result("host_required", "the Windows host is not part of this package yet (Task 7)")


def ensure_services(profile: HostProfile, connector_pid: int,
                    hooks: Hooks | None = None) -> tuple[dict[str, int], list[Result]]:
    """Register this connector's lease and make sure the profile's services run; wait until they
    answer. Returns the ports of the services that are ready and ours, and every problem met.

    On Windows the services belong to the host (ensure_host, Task 7); elsewhere a new session
    already outlives the chat, so this connector starts them, with a watchdog."""
    results: list[Result] = []
    if hooks is None:
        hooks, loaded = load_hooks(profile)
        results.extend(loaded)
    missing = require_psutil()
    if missing is not None:
        results.append(missing)
    (runtime, current), problem = hook_calls.resolve_runtime(hooks, profile)
    if problem is not None:
        results.append(problem)
    if not current:
        results.append(Result("runtime_not_current", runtime, {"runtime": runtime}))
    run_dir = Path(profile.run_dir)
    started: dict = {}
    failed: set[str] = set()
    with service_lock(run_dir):
        # Register first, so the host or the watchdog never sees no connector while this one starts.
        try:
            write_lease(profile, connector_pid, {"runtime": runtime})
        except OSError as error:
            results.append(Result("lease_write_failed", f"{connector_pid}: {error}", {"pid": connector_pid}))
            return {}, results
        if not (Path(runtime) / ".complete.json").is_file():
            results.append(Result("runtime_incomplete", f"{runtime} has no .complete.json; it was replaced "
                                  "or never finished", {"runtime": runtime}))
            return {}, results
        if WINDOWS:
            host = ensure_host(profile, hooks)
            if host is not None:
                results.append(host)
                return {}, results
        else:
            try:
                Path(profile.working_dir).mkdir(parents=True, exist_ok=True)
            except OSError:
                pass  # start_missing_services records it
            _start_watchdog(profile, Path(profile.working_dir), results)
            state = _read_state(profile, results)
            failed = start_missing_services(profile, hooks, runtime, state, started, results)
    ready, waited = wait_until_ready(profile, started, kinds=[k for k in profile.services if k not in failed])
    return ready, results + waited
