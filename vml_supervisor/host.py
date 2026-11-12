"""The POSIX watchdog, the Windows voice host, and the connector's ensure.

Moved from FELIX voice.py (454f20d0e: HOST_MARKER, HOST_GRACE, HOST_PROBLEMS, HOST_CHECK,
start_outside_chats, ensure_host, host(), watch(), launch's host backstop, main's `host`) and merged
with voicemode_native.py's watch / watch_tick (restarts: a dead service is started again on its
recorded port, never onto a taken one, at most restart_limit times, each logged to run/watch.log).

On Windows the Copilot app ends a chat's processes with the chat's job, so services started by one
chat died for every chat when the app unloaded it (FELIX laptop, 2026-11-12, n=1). The host is
started through Windows' process service (Win32_Process.Create), so it belongs to no chat's job; it
owns the services' Job Object, the services and the helpers as its children. Elsewhere a new session
already outlives the chat, so the connector starts the services and a watchdog.

The host inherits nothing: it takes the profile FILE as its argument (start_host writes it to
run/host-profile.json). It loads the hooks once when it starts and keeps running on what it loaded;
host_command[0] should be profile.python, which the hooks are written for.

Every Result the watchdog and the host meet goes to run/watch.log, one per line (time, then the
Result as JSON), and to the profile's on_result hook. Nothing here raises for an expected failure.

Result codes
- start_host: host_started (data: pid, command), host_not_configured (empty host_command),
  host_marker_empty, host_command_bad (an argument holds `"` or a line break),
  host_command_unowned (host_marker is not in the command), host_profile_write_failed,
  host_start_refused (PowerShell did not answer "0 <pid>": no fallback).
- ensure_host: start_host's, host_pid_write_failed (the new host was stopped), and
  host_requires_psutil (Windows without psutil: no host is started).
- ensure: on Windows services.ensure_services's (which reaches ensure_host under the lock);
  elsewhere ensure_services's, then for each helper of this platform its start_helper Result
  followed by the Results in its data["results"].
- watch_tick / the host's look (watch.log): service_restarted, service_restart_failed,
  restart_port_taken (POSIX: said once and left for the next connector; host: started on another
  port, service_moved), restart_limit_reached (said once per run of the limit),
  state_write_failed (the restarted process was stopped), kill_incomplete, plus
  services.start_missing_services's and stop_recorded's codes, host_stopped, watch_stopped,
  watch_lock_failed, host_failed (an unexpected error ended the host; the next connector starts
  a new one), helpers_write_failed, job_unavailable, hook_failed, profile_key_unknown.
- run/helpers.json: {kind: {state: running|stopped|failed, code, detail, results: [{code,
  detail}]}}, the helper's start_helper Result with its secondary results flattened in, so a
  kill_incomplete behind a successful start shows.
"""
from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path

from . import hooks as hook_calls
from .helpers import PLATFORM, read_mode, start_helper
from .hooks import Hooks, load_hooks
from .procs import (WINDOWS, background_options, kill_tree, owned, port_free, process_command,
                    require_psutil)
from .procs import stop_signals_held, unblock_stop_signals
from .profile import HostProfile, load_profile
from .result import Result
from .services import (HOST_PID, ensure_services, service_environment, start_missing_services,
                       stop_recorded)
from .state import atomic_json, live_leases, read_json, read_state, service_lock, write_state
from .winjob import SpawnError, services_job, spawn

HOST_PROFILE = "host-profile.json"   # in run_dir: the profile the host reads
HELPERS_FILE = "helpers.json"        # in run_dir: the helpers' state, for the chat's start notes
WATCH_LOG = "watch.log"              # in run_dir
HOST_CHECK = 10.0                    # seconds between a connector's checks that the host still runs
HOST_START_TIMEOUT = 60              # seconds PowerShell may take to ask Windows for the host
_RUNNING = ("helper_started", "helper_running", "helper_restarted")
_STOPPED = ("helper_stopped", "helper_not_running")
_SINGLE_QUOTES = "'\u2018\u2019\u201a\u201b"   # PowerShell ends a '...' string at any of these


# ----- the log ------------------------------------------------------------------------------------


def _as_dict(result: Result) -> dict:
    return {"code": result.code, "detail": result.detail, "data": result.data}


def log_results(profile: HostProfile, hooks: Hooks | None, results: list) -> None:
    """Append each Result to run/watch.log and hand them to the on_result hook. Never raises: the
    log is the only place a host started with no console can speak."""
    if not results:
        return
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    lines = "".join(f"{stamp} {json.dumps(_as_dict(r), default=str)}\n" for r in results)
    with contextlib.suppress(OSError):
        with (Path(profile.run_dir) / WATCH_LOG).open("a", encoding="utf-8") as log:
            log.write(lines)
    if hooks is not None:
        _, problem = hook_calls.on_result(hooks, profile, [json.loads(json.dumps(_as_dict(r), default=str))
                                                           for r in results])
        if problem is not None:
            log_results(profile, None, [problem])


# ----- restarts (the watchdog and the host) -------------------------------------------------------


@dataclasses.dataclass
class WatchState:
    """What a watchdog or host remembers between looks."""
    restarts: dict = dataclasses.field(default_factory=dict)   # kind -> [monotonic times]
    said: set = dataclasses.field(default_factory=set)         # (kind, pid, code) already logged


def _recorded(entry) -> bool:
    return (isinstance(entry, dict) and isinstance(entry.get("command"), list) and entry["command"]
            and isinstance(entry.get("port"), int) and not isinstance(entry["port"], bool))


def restart_dead_services(profile: HostProfile, hooks: Hooks, state: dict, watch: WatchState, now: float,
                          results: list) -> set[str]:
    """Start each recorded service that died again, with its recorded command on its recorded port,
    at most restart_limit[0] times in restart_limit[1] seconds, as a child of this process (in the
    services' Job Object on Windows). The caller holds the lock and writes nothing else. Returns the
    services whose recorded port another program now holds (each restart_port_taken, said once):
    the caller decides whether to start them on another port."""
    count, window = profile.restart_limit
    working_dir = Path(profile.working_dir)
    taken: set[str] = set()
    job = None
    for kind in profile.services:
        entry = state.get(kind)
        if not _recorded(entry) or owned(entry):
            continue
        recent = [t for t in watch.restarts.get(kind, []) if now - t < window]
        watch.restarts[kind] = recent
        if len(recent) >= count:
            if (kind, "limit") not in watch.said:
                watch.said.add((kind, "limit"))
                results.append(Result("restart_limit_reached", f"{kind} died {count} times in {window:g} s; "
                                      "not restarted", {"service": kind, "port": entry["port"]}))
            continue
        watch.said.discard((kind, "limit"))
        port, old = entry["port"], entry.get("pid")
        if not port_free(port):
            if (kind, old, "taken") not in watch.said:
                watch.said.add((kind, old, "taken"))
                results.append(Result("restart_port_taken", f"{kind} died; port {port} is taken, not "
                                      "restarted there", {"service": kind, "port": port}))
            taken.add(kind)
            continue
        if WINDOWS and job is None:
            try:
                job = services_job(profile)
            except OSError as error:
                results.append(Result("job_unavailable", f"{profile.job_name}: {error}; {kind} restarts "
                                      "without the job"))
        runtime = entry.get("runtime") if isinstance(entry.get("runtime"), str) else str(profile.runtime_dir)
        ports = {k: e["port"] for k, e in state.items() if k in profile.services and _recorded(e)}
        env, problem = hook_calls.environment(hooks, profile, runtime, ports, {})
        if problem is not None:
            results.append(problem)
        log = entry.get("log") if isinstance(entry.get("log"), str) else str(Path(profile.run_dir) / f"{kind}.log")
        extra = {} if WINDOWS else {"preexec_fn": unblock_stop_signals}
        watch.restarts[kind].append(now)
        with stop_signals_held():
            try:
                working_dir.mkdir(parents=True, exist_ok=True)
                with open(log, "a") as output:
                    process, spawned = spawn([str(a) for a in entry["command"]], job,
                                             env=service_environment(env), cwd=str(working_dir),
                                             stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                             **background_options(), **extra)
            except SpawnError as error:
                results.append(error.result)
                continue
            except OSError as error:
                results.append(Result("service_restart_failed", f"{kind}: {entry['command']}: {error}",
                                      {"service": kind, "log": log}))
                continue
            if spawned.code not in ("no_job", "breakaway"):
                results.append(spawned)
            entry["pid"] = process.pid
            if not write_state(profile, state, results):
                failed = kill_tree(process.pid, profile.stop_timeout)
                if failed is not None:
                    results.append(failed)
                entry["pid"] = old
                continue
        results.append(Result("service_restarted", f"restarted {kind} on port {port}: process {process.pid} "
                              f"(was {old})", {"service": kind, "port": port, "pid": process.pid, "was": old}))
    return taken


# ----- the POSIX watchdog ---------------------------------------------------------------------------


def watch_tick(profile: HostProfile, hooks: Hooks, watch: WatchState) -> bool:
    """One look, under the lock: no live lease stops every recorded service and helper (False);
    otherwise a dead service is restarted on its recorded port (True). Results go to watch.log."""
    results: list[Result] = []
    try:
        with service_lock(profile.run_dir):
            if not live_leases(profile):
                results.extend(stop_recorded(profile, read_state(profile, results)))
                results.append(Result("watch_stopped", "no connector is left"))
                return False
            state = read_state(profile, results)
            restart_dead_services(profile, hooks, state, watch, time.monotonic(), results)
            return True
    except OSError as error:
        results.append(Result("watch_lock_failed", f"{profile.run_dir}: {error}"))
        return True
    finally:
        log_results(profile, hooks, results)


def watch_main(profile: HostProfile) -> int:
    """The POSIX watchdog: every watch_interval a watch_tick, until no connector is left."""
    hooks, loaded = load_hooks(profile)
    log_results(profile, hooks, loaded)
    watch = WatchState()
    while True:
        time.sleep(profile.watch_interval)
        if not watch_tick(profile, hooks, watch):
            return 0


# ----- the Windows host -----------------------------------------------------------------------------


def _helper_record(result: Result) -> dict:
    secondary = result.data.get("results") if isinstance(result.data, dict) else None
    state = "running" if result.code in _RUNNING else "stopped" if result.code in _STOPPED else "failed"
    return {"state": state, "code": result.code, "detail": result.detail,
            "results": [{"code": r.code, "detail": r.detail} for r in secondary or [] if isinstance(r, Result)]}


def _host_services(profile: HostProfile, hooks: Hooks, watch: WatchState, results: list) -> None:
    """The host's look at its services, under the lock: restart the dead ones on their ports, and
    start those with no record (or whose port was taken, on another port)."""
    state = read_state(profile, results)
    taken = restart_dead_services(profile, hooks, state, watch, time.monotonic(), results)
    moved = {kind: state.pop(kind)["port"] for kind in taken}
    wanted = {kind: spec for kind, spec in profile.services.items()
              if kind in taken or not _recorded(state.get(kind))}
    wanted = {kind: spec for kind, spec in wanted.items() if not owned(state.get(kind))}
    if not wanted:
        if moved:
            write_state(profile, state, results)
        return
    (runtime, current), problem = hook_calls.resolve_runtime(hooks, profile)
    if problem is not None:
        results.append(problem)
    for kind in wanted:
        state.pop(kind, None)
    start_missing_services(dataclasses.replace(profile, services=wanted), hooks, runtime, state, {}, results)
    for kind, old in moved.items():
        entry = state.get(kind)
        if isinstance(entry, dict):
            results.append(Result("service_moved", f"{kind} from port {old} to {entry.get('port')}",
                                  {"service": kind, "was": old, "port": entry.get("port")}))


def _host_helpers(profile: HostProfile, hooks: Hooks, records: dict, results: list) -> None:
    """Outside the lock (start_helper takes it): start each helper of this platform that is not
    running in the mode read_mode gives now; one that runs in that mode is left alone."""
    state, _ = read_json(Path(profile.run_dir) / "state.json", {})
    state = state if isinstance(state, dict) else {}
    for kind, spec in profile.helpers.items():
        if spec.platforms and PLATFORM not in spec.platforms:
            continue
        mode = read_mode(profile, kind, hooks, results) if spec.needs_mode else None
        entry = state.get(kind)
        if kind in records and owned(entry) and entry.get("mode") == mode:
            continue
        record = _helper_record(start_helper(profile, kind, mode, hooks))
        if records.get(kind) != record:
            results.append(Result(record["code"], f"{kind}: {record['detail']}",
                                  {"helper": kind, "results": record["results"]}))
        records[kind] = record


def host_main(profile_file: Path) -> int:
    """The Windows voice host. Every watch_interval: under the lock, end everything once no
    connector lease is left (after host_start_grace for the first), restart dead services, start
    missing ones; then bring each helper to its mode and write run/helpers.json."""
    profile_file = Path(profile_file)
    profile, problems = load_profile(profile_file)
    if profile is None:
        with contextlib.suppress(OSError):   # next to the profile file: run_dir, if anywhere
            with (profile_file.parent / WATCH_LOG).open("a", encoding="utf-8") as log:
                for problem in problems:
                    log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {json.dumps(_as_dict(problem))}\n")
        return 2
    run_dir = Path(profile.run_dir)
    with contextlib.suppress(OSError):
        run_dir.mkdir(parents=True, exist_ok=True)
    hooks, loaded = load_hooks(profile)
    log_results(profile, hooks, [*problems, *loaded])
    if WINDOWS:
        try:
            services_job(profile)   # held for the host's lifetime: its services and helpers live in it
        except OSError as error:
            log_results(profile, hooks, [Result("job_unavailable", f"{profile.job_name}: {error}")])
    began, seen = time.monotonic(), False
    watch = WatchState()
    records: dict = {}
    written = None
    try:
        while True:
            results: list[Result] = []
            try:
                with service_lock(run_dir):
                    if live_leases(profile):
                        seen = True
                    elif seen or time.monotonic() - began > profile.host_start_grace:
                        results.extend(stop_recorded(profile, read_state(profile, results)))
                        for name in (HOST_PID, HELPERS_FILE):
                            with contextlib.suppress(OSError):
                                (run_dir / name).unlink(missing_ok=True)
                        results.append(Result("host_stopped", "no connector is left" if seen else
                                              f"no connector came in {profile.host_start_grace:g} s"))
                        log_results(profile, hooks, results)
                        return 0
                    _host_services(profile, hooks, watch, results)
            except OSError as error:
                results.append(Result("watch_lock_failed", f"{run_dir}: {error}"))
            _host_helpers(profile, hooks, records, results)
            if records != written:
                try:
                    atomic_json(run_dir / HELPERS_FILE, records)
                    written = json.loads(json.dumps(records))
                except OSError as error:
                    results.append(Result("helpers_write_failed", f"{run_dir / HELPERS_FILE}: {error}"))
            log_results(profile, hooks, results)
            time.sleep(profile.watch_interval)
    except Exception:  # noqa: BLE001 - said in the log; the next connector starts a new host
        log_results(profile, hooks, [Result("host_failed", traceback.format_exc())])
        return 1


# ----- starting the host (the connector side) ------------------------------------------------------


def _quote(word: str) -> str:
    """One argument for a Windows command line: quoted, a trailing backslash doubled so it does not
    escape the closing quote. Words holding `"` are refused before this."""
    stripped = word.rstrip("\\")
    return '"' + stripped + "\\" * (2 * (len(word) - len(stripped))) + '"'


def _single_quoted(text: str) -> str:
    """Text for inside a PowerShell '...' string: each kind of single quote doubled."""
    return "".join(c * 2 if c in _SINGLE_QUOTES else c for c in text)


def start_host(profile: HostProfile) -> Result:
    """Start the host through Windows' process service (Win32_Process.Create), so it belongs to no
    chat's job and outlives the chat that asked for it: FELIX's start_outside_chats, the route of
    fe-bootstrap's voice-install.js startDetachedWindows. Windows PowerShell by its full path (the
    current folder is searched first otherwise), no window, working folder profile.working_dir. The
    host inherits nothing: its arguments are profile.host_command + [the profile file], which this
    writes first. A refusal is host_start_refused, with no fallback."""
    command = [str(word) for word in profile.host_command]
    if not command:
        return Result("host_not_configured", "the profile names no host_command")
    if not profile.host_marker:
        return Result("host_marker_empty", "the profile's host_marker is empty; no host was started")
    profile_file = Path(profile.run_dir) / HOST_PROFILE
    words = [*command, str(profile_file)]
    folder = str(profile.working_dir)
    bad = [word for word in [*words, folder] if any(c in word for c in '"\r\n')]
    if bad:
        return Result("host_command_bad", f"an argument holds a quotation mark or a line break: {bad[0]!r}; "
                      "no host was started", {"argument": bad[0]})
    if profile.host_marker not in " ".join(words):
        return Result("host_command_unowned", f"the host_marker {profile.host_marker!r} is not in the "
                      "command; no host was started", {"command": words})
    try:
        Path(profile.working_dir).mkdir(parents=True, exist_ok=True)
        profile.to_file(profile_file)
    except OSError as error:
        return Result("host_profile_write_failed", f"{profile_file}: {error}")
    line = _single_quoted(" ".join(_quote(word) for word in words))
    script = ("$s = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ShowWindow=[uint16]0}; "
              "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments "
              f"@{{CommandLine='{line}'; CurrentDirectory='{_single_quoted(folder)}'; ProcessStartupInformation=$s}}; "
              '"$($r.ReturnValue) $($r.ProcessId)"')
    system_root = os.environ.get("SystemRoot") or os.environ.get("SYSTEMROOT") or r"C:\Windows"
    powershell = system_root.rstrip("\\/") + r"\System32\WindowsPowerShell\v1.0\powershell.exe"
    try:
        done = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-Command", script],
                              cwd=folder, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              check=False, timeout=HOST_START_TIMEOUT, **background_options())
    except (OSError, subprocess.SubprocessError) as error:
        return Result("host_start_refused", f"{powershell}: {error}", {"command": words})
    out, err = (done.stdout or "").strip(), (done.stderr or "").strip()
    match = re.fullmatch(r"0 (\d+)", out)
    if not match:
        return Result("host_start_refused", f"Windows did not start the host: {out} {err[:300]}".strip(),
                      {"command": words, "returncode": done.returncode, "stdout": out, "stderr": err[:300]})
    pid = int(match.group(1))
    return Result("host_started", f"host {pid}", {"pid": pid, "command": words})


def ensure_host(profile: HostProfile, results: list) -> bool:
    """Start the host once per install unless run/host.pid is a live process carrying host_marker.
    The caller holds the service lock, so two connectors start one host. True when it runs.
    Without psutil (Windows) nothing can be looked up: a running host would look dead and each
    connector would start another, so none is started."""
    if require_psutil() is not None:
        results.append(Result("host_requires_psutil", "psutil is not installed; a running host cannot be "
                              "found without it, so no host was started"))
        return False
    pid_file = Path(profile.run_dir) / HOST_PID
    if profile.host_marker:
        with contextlib.suppress(ValueError, OSError):
            command = process_command(int(pid_file.read_text()))
            if command and profile.host_marker in command:
                return True
    started = start_host(profile)
    results.append(started)
    if started.code != "host_started":
        return False
    try:
        pid_file.write_text(str(started.data["pid"]))
    except OSError as error:
        # Unrecorded, the next connector would start a second host beside it.
        failed = kill_tree(started.data["pid"], profile.stop_timeout)
        if failed is not None:
            results.append(failed)
        results.append(Result("host_pid_write_failed", f"{pid_file}: {error}; host {started.data['pid']} "
                              "was stopped", {"pid": started.data["pid"]}))
        return False
    return True


def check_host(profile: HostProfile) -> list[Result]:
    """The connector's backstop (FELIX launch, every HOST_CHECK seconds on Windows): a host that
    ended anyway is started again, and it restarts the services."""
    results: list[Result] = []
    try:
        with service_lock(profile.run_dir):
            ensure_host(profile, results)
    except OSError as error:
        results.append(Result("watch_lock_failed", f"{profile.run_dir}: {error}"))
    return results


def ensure(profile: HostProfile, connector_pid: int) -> tuple[dict[str, int], list[Result]]:
    """The connector's entry point. Windows: write the lease, start the host unless it runs, wait
    up to start_timeout for the services it starts (services.ensure_services does all three). The
    host starts the helpers. Elsewhere: ensure_services, then start_helper for each helper of this
    platform; each helper's secondary results follow its Result."""
    hooks, loaded = load_hooks(profile)
    ports, results = ensure_services(profile, connector_pid, hooks)
    results = [*loaded, *results]
    if WINDOWS:
        return ports, results
    for kind, spec in profile.helpers.items():
        if spec.platforms and PLATFORM not in spec.platforms:
            continue
        result = start_helper(profile, kind, hooks=hooks)
        results.append(result)
        results.extend(r for r in result.data.get("results", []) if isinstance(r, Result))
    return ports, results


# ----- command line ------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """`host <profile file>` runs host_main; `watch <profile file>` runs watch_main. 2 on bad use."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2 or argv[0] not in ("host", "watch"):
        return 2
    if argv[0] == "host":
        return host_main(Path(argv[1]))
    profile, _ = load_profile(Path(argv[1]))
    if profile is None:
        return 2
    return watch_main(profile)


if __name__ == "__main__":
    sys.exit(main())
