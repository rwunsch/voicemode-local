"""The shared speech services: start TTS and STT once for every connector, lease them, stop them
with the last one.

Moved from FELIX voice.py (454f20d0e: ensure_services, start_missing_services, wait_until_ready,
release, stop, stop_recorded, stop_entry, check_service, valid_response and its proxy-free opener;
the Windows host itself is in host.py, reached through ensure_host), with voicemode_native.py's additions
merged in: the runtime/.complete.json guard, the isinstance guard on the ports already taken, the
services' Job Object, and the command and runtime kept in each record.

Nothing here raises for an expected failure; each one is a Result in the returned list.

Result codes
- from ensure_services: validate()'s findings (a BLOCKING one returns at once, nothing started),
  service_lock_failed (nothing is done), stop_marker_remove_failed,
  hook_failed, hook_bad_return (a hook; its default was used),
  psutil_missing (Windows), runtime_not_current (resolve_runtime said so; services still start),
  runtime_incomplete (runtime/.complete.json is gone: nothing is started), on Windows the host's
  codes from host.ensure_host (host_started; host_not_configured, host_marker_empty,
  host_command_bad, host_command_unowned, host_profile_write_failed, host_start_refused,
  host_pid_write_failed, host_requires_psutil: nothing is started and nothing is waited for),
  lease_write_failed (nothing is started), working_dir_unavailable (nothing is started),
  job_unavailable (Windows: services start without the job), watchdog_not_configured,
  watch_marker_empty,
  watchdog_start_failed (POSIX; a watchdog whose pid could not be recorded is stopped),
  state_corrupt / state_unreadable (read as empty, under the lock), port_override_invalid, unknown_service,
  override_port_taken (the override's port is taken: another is used), no_free_port,
  service_command_bad (empty marker: not started), service_command_unowned (the
  marker or `--port N` is missing from the command, so it could never be owned: not started),
  breakaway_refused, job_assign_failed, resume_failed, service_start_failed,
  state_write_failed (the new service was killed), kill_incomplete, service_exited,
  service_not_ready, owner_other (another process answers on the port), owner_unknown (the
  answer came, but no listener could be found to prove it is ours).
- from wait_until_ready: a failure the Windows host recorded for the service ({"failed": code}
  in state.json: no_free_port, service_command_unowned, job_assign_failed, ...) at once, a recorded
  note (breakaway_refused) with a ready service, service_exited (also when the recorded process is
  gone before it answered), service_not_ready, owner_other, owner_unknown.
- from release: service_lock_failed, lease_remove_failed, state_corrupt, state_unreadable, kill_incomplete, psutil_missing,
  state_write_failed.
- from stop: service_lock_failed, stop_marker_write_failed (run/stopped, which keeps a host from
  starting again until the next connector's ensure), host_marker_empty (run/host.pid is left alone), kill_incomplete (the host would not
  die: run/host.pid is kept), the same as release, then services_stopped (data: the services that
  were running, whether a host was, and how many connectors still hold a lease).
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
from .ports import choose_port, port_candidates, requested_port
from .procs import (LOOPBACK, WINDOWS, background_options, kill_tree, listener_pids, owned,
                    process_command, process_tree, require_psutil, stop_signals_held,
                    unblock_stop_signals)
from .profile import HostProfile, blocking
from .result import Result
from .state import atomic_json, live_leases, read_json, read_state, service_lock, write_lease, write_state
from .winjob import SpawnError, services_job, spawn

# Readiness checks talk to loopback only: never through a proxy from the environment.
_local_opener = build_opener(ProxyHandler({}))
READY_POLL = 0.5
HOST_PID = "host.pid"   # in run_dir: the Windows host's process id, written under the service lock
HOST_STARTS = "host-starts.json"   # in run_dir: recent host starts, for the start limit
# in run_dir: written by stop(). While it exists no host is started (ensure_host, check_host), so
# a stop sticks on Windows as it does elsewhere; the next connector's ensure removes it.
STOPPED = "stopped"

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
    write_state(profile, state, results)
    return results


def _remove_lease(profile: HostProfile, connector_pid: int, results: list) -> None:
    lease = Path(profile.run_dir) / "clients" / str(connector_pid)
    try:
        lease.unlink(missing_ok=True)
    except OSError as error:
        results.append(Result("lease_remove_failed", f"{lease}: {error}", {"pid": connector_pid}))


def _take_lock(stack: contextlib.ExitStack, run_dir, results: list) -> bool:
    """The service lock on the stack; False (with service_lock_failed in results) when run_dir
    cannot be created or locked."""
    try:
        stack.enter_context(service_lock(run_dir))
    except OSError as error:
        results.append(Result("service_lock_failed", f"{run_dir}: {error}"))
        return False
    return True


def release(profile: HostProfile, connector_pid: int) -> list[Result]:
    """Drop this connector's lease; stop the services when no connector is left."""
    results: list[Result] = []
    missing = require_psutil()
    if missing is not None:
        results.append(missing)
    with contextlib.ExitStack() as stack:
        if not _take_lock(stack, profile.run_dir, results):
            return results
        _remove_lease(profile, connector_pid, results)
        if not live_leases(profile):
            results.extend(stop_recorded(profile, read_state(profile, results)))
    return results


def _stop_host(profile: HostProfile, results: list) -> bool:
    """Stop the Windows host recorded in run/host.pid (owned through profile.host_marker) and drop
    the record; True when a host was running. The caller holds the lock. Kept when it would not die,
    so a later stop tries again."""
    pid_file = Path(profile.run_dir) / HOST_PID
    try:
        pid = int(pid_file.read_text())
    except (OSError, ValueError):
        return False
    if not profile.host_marker:
        # An empty marker is in every command line: whatever now has that pid would be killed.
        results.append(Result("host_marker_empty", f"the profile's host_marker is empty; {pid_file} "
                              "was left alone"))
        return False
    entry = {"pid": pid, "port": None, "marker": profile.host_marker}
    running = owned(entry)
    failed = stop_entry(profile, entry)
    if failed is not None:
        results.append(failed)
        return running
    with contextlib.suppress(OSError):
        pid_file.unlink(missing_ok=True)
        # A deliberate stop is not a crash: it must not count towards the host-start limit.
        (Path(profile.run_dir) / HOST_STARTS).unlink(missing_ok=True)
    return running


def stop(profile: HostProfile) -> list[Result]:
    """Stop the host first (it would start the services again), then every recorded service and
    helper, whatever leases remain."""
    results: list[Result] = []
    missing = require_psutil()
    if missing is not None:
        results.append(missing)
    with contextlib.ExitStack() as stack:
        if not _take_lock(stack, profile.run_dir, results):
            return results
        try:
            (Path(profile.run_dir) / STOPPED).write_text(time.strftime("%Y-%m-%d %H:%M:%S\n"))
        except OSError as error:
            results.append(Result("stop_marker_write_failed", f"{Path(profile.run_dir) / STOPPED}: {error}; "
                                  "a connector still running may start the host again"))
        host = _stop_host(profile, results)
        others = live_leases(profile)
        state = read_state(profile, results)
        running = [kind for kind in state if owned(state[kind])]
        results.extend(stop_recorded(profile, state))
    results.append(Result("services_stopped", ", ".join(running),
                          {"services": running, "host": host, "connectors": len(others)}))
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
    Windows the host (host.py) holds that role."""
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
    requested, source = requested_port(profile, kind)
    if requested is not None and port != requested:
        # The override asked for this port and it is taken: said, never a silent fallback.
        results.append(Result("override_port_taken", f"{kind}: {source} asks for port {requested}, which is "
                              f"taken; {'using ' + str(port) if port is not None else 'no other port is free'}",
                              {"service": kind, "port": requested, "chosen": port, "source": source}))
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
            results.append(Result(spawned.code, spawned.detail, {**spawned.data, "service": kind}))
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
                           results: list, failures: dict | None = None) -> set[str]:
    """Start the profile's services that are not running, as children of this process, and record
    them in state (and state.json). The caller holds the service lock. Returns the services that
    could not be started (each with its Result); the running and started ones are in state. With
    `failures`, each service that could not be started maps to the Result that says why (the
    Windows host records it in state.json for the connectors waiting)."""
    failures = {} if failures is None else failures
    working_dir = Path(profile.working_dir)
    try:
        working_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        problem = Result("working_dir_unavailable", f"{working_dir}: {error}")
        results.append(problem)
        failures.update({kind: problem for kind in profile.services})
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

    def fail(kind, before):
        failed.add(kind)
        failures[kind] = results[-1] if len(results) > before else Result(
            "service_start_failed", f"{kind} could not be started", {"service": kind})

    for kind in profile.services:
        if owned(state.get(kind)):
            ports[kind] = state[kind]["port"]
            continue
        state.pop(kind, None)
        before = len(results)
        port = _choose(profile, kind, {**state, **{k: {"port": p} for k, p in ports.items()}}, results)
        if port is None:
            fail(kind, before)
        else:
            ports[kind] = port
            wanted.append(kind)
    if wanted:
        env, problem = hook_calls.environment(hooks, profile, runtime, dict(ports), {})
        if problem is not None:
            results.append(problem)
        env = service_environment(env)
        for kind in wanted:
            before = len(results)
            process = _start(profile, hooks, kind, ports[kind], runtime, env, working_dir, job, state, results)
            if process is None:
                fail(kind, before)
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
            if isinstance(entry, dict) and isinstance(entry.get("failed"), str):
                # The Windows host could not start it: said at once, not after start_timeout.
                data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
                results.append(Result(entry["failed"], str(entry.get("detail", "")), {**data, "service": kind}))
                break
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
                    for note in entry.get("notes") or []:
                        if isinstance(note, dict) and isinstance(note.get("code"), str):
                            results.append(Result(note["code"], str(note.get("detail", "")), {"service": kind}))
                break
            process = started.get(kind)
            if process is not None and process.poll() is not None:
                results.append(Result("service_exited", f"{kind} exited with {process.returncode}; see {log}",
                                      {"service": kind, "code": process.returncode, "log": log}))
                break
            if usable and not owned(entry):
                # Recorded, not answering, and the recorded process is gone: it died after its record.
                log = entry.get("log") if isinstance(entry.get("log"), str) else log
                results.append(Result("service_exited", f"{kind} (process {entry['pid']}) is gone; see {log}",
                                      {"service": kind, "pid": entry["pid"], "log": log}))
                break
            if time.monotonic() >= deadline:
                where = f"on {LOOPBACK}:{entry['port']}" if usable else "(not started yet)"
                results.append(Result("service_not_ready", f"{kind} {where} did not answer {spec.ready_path} "
                                      f"in {profile.start_timeout} s; see {log}",
                                      {"service": kind, "port": entry["port"] if usable else None, "log": log}))
                break
            time.sleep(READY_POLL)
    return ready, results


def ensure_host(profile: HostProfile, hooks: Hooks, results: list) -> bool:
    """Windows: the services belong to a host started outside every chat (FELIX 454f20d0e
    ensure_host), because the app ends a chat's processes with its job. host.ensure_host starts it
    unless run/host.pid is a live host. Called under the service lock, after the lease is written;
    True when the host runs, its Results in `results`."""
    from . import host   # host imports this module
    return host.ensure_host(profile, results)


def ensure_services(profile: HostProfile, connector_pid: int, hooks: Hooks | None = None,
                    resolved: tuple[str, bool] | None = None) -> tuple[dict[str, int], list[Result]]:
    """Register this connector's lease and make sure the profile's services run; wait until they
    answer. Returns the ports of the services that are ready and ours, and every problem met.

    resolved is (runtime, current) from a caller that has already validated the profile and asked
    resolve_runtime (host.ensure); without it this validates (a BLOCKING finding refuses the start)
    and resolves itself.

    On Windows the services belong to the host (ensure_host, host.py); elsewhere a new session
    already outlives the chat, so this connector starts them, with a watchdog."""
    results: list[Result] = []
    if hooks is None:
        hooks, loaded = load_hooks(profile)
        results.extend(loaded)
    if resolved is None:
        invalid = profile.validate()
        results.extend(invalid)
        if blocking(invalid):
            return {}, results
        (runtime, current), problem = hook_calls.resolve_runtime(hooks, profile)
        if problem is not None:
            results.append(problem)
    else:
        runtime, current = resolved
    missing = require_psutil()
    if missing is not None:
        results.append(missing)
    if not current:
        results.append(Result("runtime_not_current", runtime, {"runtime": runtime}))
    run_dir = Path(profile.run_dir)
    started: dict = {}
    failed: set[str] = set()
    with contextlib.ExitStack() as stack:
        if not _take_lock(stack, run_dir, results):
            return {}, results
        # Register first, so the host or the watchdog never sees no connector while this one starts.
        try:
            write_lease(profile, connector_pid, {"runtime": runtime})
        except OSError as error:
            results.append(Result("lease_write_failed", f"{connector_pid}: {error}", {"pid": connector_pid}))
            return {}, results
        # A new launch lifts an earlier stop (see STOPPED).
        try:
            (run_dir / STOPPED).unlink(missing_ok=True)
        except OSError as error:
            results.append(Result("stop_marker_remove_failed", f"{run_dir / STOPPED}: {error}"))
        if not (Path(runtime) / ".complete.json").is_file():
            results.append(Result("runtime_incomplete", f"{runtime} has no .complete.json; it was replaced "
                                  "or never finished", {"runtime": runtime}))
            return {}, results
        if WINDOWS:
            if not ensure_host(profile, hooks, results):
                return {}, results
        else:
            try:
                Path(profile.working_dir).mkdir(parents=True, exist_ok=True)
            except OSError:
                pass  # start_missing_services records it
            _start_watchdog(profile, Path(profile.working_dir), results)
            state = read_state(profile, results)
            failed = start_missing_services(profile, hooks, runtime, state, started, results)
    ready, waited = wait_until_ready(profile, started, kinds=[k for k in profile.services if k not in failed])
    return ready, results + waited
