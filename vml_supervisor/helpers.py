"""The helpers: the barge-in trigger and the push-to-talk listener, one each per install, beside the
speech services and stopped with them.

Moved from FELIX voice.py (454f20d0e: start_helper, stop_unrecorded, running_commands, the stop
half of stop_barge, HELPER_START_CHECK; stop_entry is services.stop_entry, one copy). The
commands come from the profile's HelperSpec; FELIX's ensure_barge / ensure_ptt become profile
data. What a helper says about itself (FELIX's `barge status`) stays the owner's command: here
Result.detail carries a state word and the log path.

HelperSpec
- argv: the command. "{mode}" in an argument is replaced by the mode; with no mode (None, the
  helper's default) every argument holding "{mode}" is dropped, so put it in one argument
  ("--mode={mode}" or a positional "{mode}").
- marker: must be non-empty and appear in argv (procs.owned matches it), or nothing is started.
- log: a file name in run_dir, or an absolute path.
- platforms: sys.platform values ("win32", "darwin", "linux"); empty means every platform.
- needs_mode: False means the mode is ignored (recorded as None), so a mode change never
  restarts it.

Nothing here raises for an expected failure.

Result codes
- start_helper: helper_started, helper_running (owned, same mode: left alone), helper_restarted
  (the mode changed), helper_failed (OSError, a failed spawn or a state write: nothing is left
  running), helper_exited (it stopped within HELPER_START_CHECK: not recorded),
  helper_not_for_platform, helper_command_bad (empty marker), helper_command_unowned (the marker
  is not in the command), unknown_helper. data["results"] holds what else happened on the way:
  hook_failed / hook_bad_return (a hook; its default was used), state_corrupt (read as empty),
  job_unavailable (Windows: started without the job), breakaway_refused, job_assign_failed,
  resume_failed, kill_incomplete, and stop_unrecorded's codes.
- stop_helper: helper_stopped, helper_not_running, kill_incomplete (the entry is kept, so a later
  stop tries again), state_write_failed, unknown_helper. data["results"] as above.
- stop_unrecorded: helper_unrecorded_stopped (one per process), kill_incomplete,
  helper_command_bad (empty marker: nothing is looked for), unknown_helper.
"""
from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
from pathlib import Path

from . import hooks as hook_calls
from .hooks import Hooks, load_hooks
from .procs import WINDOWS, background_options, owned, stop_signals_held, unblock_stop_signals
from .profile import HelperSpec, HostProfile
from .result import Result
from .services import service_environment, stop_entry
from .state import atomic_json, read_json, service_lock
from .winjob import SpawnError, services_job, spawn

PLATFORM = sys.platform
HELPER_START_CHECK = 3.0   # seconds a new trigger or listener must stay alive before it counts as started
_FROM_HOOK = object()      # start_helper's default mode: ask the read_mode hook


def _hooks(profile: HostProfile, hooks: Hooks | None, results: list) -> Hooks:
    if hooks is None:
        hooks, loaded = load_hooks(profile)
        results.extend(loaded)
    return hooks


def read_mode(profile: HostProfile, kind: str, hooks: Hooks | None = None,
              results: list | None = None) -> str | None:
    """The helper's mode from the profile's read_mode hook; None (the helper's default) without one.
    A hook failure gives None and is appended to `results` when given."""
    problems: list[Result] = []
    hooks = _hooks(profile, hooks, problems)
    mode, problem = hook_calls.read_mode(hooks, profile, kind)
    if problem is not None:
        problems.append(problem)
    if results is not None:
        results.extend(problems)
    return mode


def running_commands() -> list[tuple[int, str]]:
    """Every process's (pid, command line), for finding helpers started but no longer on record."""
    if WINDOWS:
        try:
            import psutil
        except ImportError:
            return []
        found = []
        for item in psutil.process_iter(["pid", "cmdline"]):
            with contextlib.suppress(psutil.Error, TypeError):
                found.append((item.info["pid"], subprocess.list2cmdline(item.info["cmdline"] or [])))
        return found
    try:
        done = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, check=False)
    except OSError:
        return []
    found = []
    for line in done.stdout.splitlines():
        pid, _, command = line.strip().partition(" ")
        if pid.isdigit():
            found.append((int(pid), command.strip()))
    return found


def _spec(profile: HostProfile, kind: str) -> HelperSpec | Result:
    spec = profile.helpers.get(kind)
    if spec is None:
        return Result("unknown_helper", kind, {"helper": kind})
    return spec


def _log_path(profile: HostProfile, spec: HelperSpec) -> Path:
    log = Path(spec.log)
    return log if log.is_absolute() else Path(profile.run_dir) / log


def stop_unrecorded(profile: HostProfile, kind: str, keep: int | None = None) -> list[Result]:
    """Stop this install's helpers of this kind that run without a record (review, 2026-11-11: a
    watchdog from before an update cleared state.json without stopping them, and the next start
    added a second key listener). Only processes whose command holds both the helper's marker and
    profile.root followed by a separator count, so a sibling such as voice-old never matches; the
    folder is case-folded on Windows. Reads every process's command line: call it when starting or
    switching a helper off, not on every look."""
    spec = _spec(profile, kind)
    if isinstance(spec, Result):
        return [spec]
    if not spec.marker:
        return [Result("helper_command_bad", f"{kind}: the marker is empty, which would match every "
                       "process of this install; nothing was stopped", {"helper": kind})]
    folder = str(profile.root).rstrip("\\/") + os.sep
    folder = folder.lower() if WINDOWS else folder
    marker = spec.marker.lower()
    results: list[Result] = []
    for pid, command in running_commands():
        text = command.lower() if WINDOWS else command
        if pid in (keep, os.getpid()) or marker not in command.lower() or folder not in text:
            continue
        results.append(Result("helper_unrecorded_stopped", f"{kind}: process {pid}: {command}",
                              {"helper": kind, "pid": pid}))
        failed = stop_entry(profile, {"pid": pid, "port": None, "marker": spec.marker})
        if failed is not None:
            results.append(failed)
    return results


def _command(spec: HelperSpec, mode: str | None) -> list[str]:
    if mode is None:
        return [str(a) for a in spec.argv if "{mode}" not in str(a)]
    return [str(a).replace("{mode}", mode) for a in spec.argv]


def _read_state(profile: HostProfile, results: list) -> dict:
    path = Path(profile.run_dir) / "state.json"
    state, problem = read_json(path, {})
    if problem is not None:
        results.append(problem)
        return {}
    if not isinstance(state, dict):
        results.append(Result("state_corrupt", f"{path}: not a JSON object"))
        return {}
    return state


def _write_state(profile: HostProfile, state: dict, results: list) -> bool:
    path = Path(profile.run_dir) / "state.json"
    try:
        atomic_json(path, state)
    except OSError as error:
        results.append(Result("state_write_failed", f"{path}: {error}"))
        return False
    return True


def _environment(profile: HostProfile, hooks: Hooks, kind: str, mode, state: dict, results: list) -> dict:
    """The profile's (or its environment hook's) variables, with this helper's mode and the ports of
    the services running now, over the few the operating system needs."""
    (runtime, _current), problem = hook_calls.resolve_runtime(hooks, profile)
    if problem is not None:
        results.append(problem)
    ports = {name: entry["port"] for name, entry in state.items()
             if name in profile.services and isinstance(entry, dict) and isinstance(entry.get("port"), int)
             and not isinstance(entry["port"], bool)}
    env, problem = hook_calls.environment(hooks, profile, runtime, ports, {kind: mode})
    if problem is not None:
        results.append(problem)
    return service_environment(env)


def start_helper(profile: HostProfile, kind: str, mode=_FROM_HOOK, hooks: Hooks | None = None) -> Result:
    """Start the helper once per install, as a child of this process (in the services' Job Object
    on Windows), and record it in state.json. Restarted when its mode changed, because a helper
    reads its mode only when it starts. Without a mode argument the read_mode hook says it.

    Under the lock with stop signals held: an owned entry in the same mode is left alone; otherwise
    the old one is stopped, then this install's unrecorded ones, and the new one is started and
    recorded at once (so a stop during the check below finds it). Outside the lock: it must stay
    alive HELPER_START_CHECK seconds, or it is taken off the record and reported. A helper that
    cannot run never stops voice: every failure is a Result."""
    results: list[Result] = []
    spec = _spec(profile, kind)
    if isinstance(spec, Result):
        return spec
    log = _log_path(profile, spec)

    def done(code, detail, **data) -> Result:
        return Result(code, detail, {"helper": kind, "log": str(log), **data, "results": results})

    if spec.platforms and PLATFORM not in spec.platforms:
        return done("helper_not_for_platform", f"{kind} runs on {', '.join(spec.platforms)}, not {PLATFORM}")
    hooks = _hooks(profile, hooks, results)
    if mode is _FROM_HOOK:
        mode = read_mode(profile, kind, hooks, results)
    if not spec.needs_mode:
        mode = None
    command = _command(spec, mode)
    if not spec.marker:
        return done("helper_command_bad", f"{kind}: the marker is empty, which would match any process",
                    command=command)
    if spec.marker not in " ".join(command):
        return done("helper_command_unowned", f"{kind}: the marker {spec.marker!r} is not in the command",
                    command=command)
    run_dir = Path(profile.run_dir)
    working_dir = Path(profile.working_dir)
    restarted = False
    with service_lock(run_dir), stop_signals_held():
        state = _read_state(profile, results)
        current = state.pop(kind, None)
        if current and owned(current) and current.get("mode") == mode:
            return done("helper_running", f"already running; log {log}", pid=current.get("pid"), mode=mode)
        if current:
            restarted = owned(current)
            failed = stop_entry(profile, current)
            if failed is not None:
                results.append(failed)
        results.extend(stop_unrecorded(profile, kind))
        job = None
        if WINDOWS:
            try:
                job = services_job(profile)
            except OSError as error:
                results.append(Result("job_unavailable", f"{profile.job_name}: {error}; {kind} starts "
                                      "without the job"))
        extra = {} if WINDOWS else {"preexec_fn": unblock_stop_signals}
        try:
            working_dir.mkdir(parents=True, exist_ok=True)
            log.parent.mkdir(parents=True, exist_ok=True)
            env = _environment(profile, hooks, kind, mode, state, results)
            with log.open("a") as output:
                # The child must not inherit the held stop signals, or it could only be killed.
                process, spawned = spawn(command, job, env=env, cwd=str(working_dir), stdin=subprocess.DEVNULL,
                                         stdout=output, stderr=output, **background_options(), **extra)
        except SpawnError as error:
            results.append(error.result)
            _write_state(profile, state, results)
            return done("helper_failed", f"the {kind} helper could not be started: {error.result.detail}",
                        command=command)
        except OSError as error:
            _write_state(profile, state, results)
            return done("helper_failed", f"the {kind} helper could not be started: {error}", command=command)
        if spawned.code not in ("no_job", "breakaway"):
            results.append(spawned)
        state[kind] = {"pid": process.pid, "port": None, "marker": spec.marker, "log": str(log),
                       "mode": mode, "command": command}
        if not _write_state(profile, state, results):
            failed = stop_entry(profile, state.pop(kind))
            if failed is not None:
                results.append(failed)
            return done("helper_failed", f"the {kind} helper was stopped: its record could not be written",
                        command=command)
    # The liveness check runs outside the lock, so other connectors' starts and stops do not wait.
    deadline = time.monotonic() + HELPER_START_CHECK
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.1)
    if process.poll() is None:
        word = "restarted" if restarted else "started"
        return done(f"helper_{word}", f"{word}; log {log}", pid=process.pid, mode=mode)
    with service_lock(run_dir):
        state = _read_state(profile, results)
        entry = state.get(kind)
        if isinstance(entry, dict) and entry.get("pid") == process.pid:
            state.pop(kind)
            _write_state(profile, state, results)
    return done("helper_exited", f"the {kind} helper stopped at once (exit code {process.returncode}); "
                f"see {log}", pid=process.pid, exit_code=process.returncode, mode=mode)


def stop_helper(profile: HostProfile, kind: str) -> Result:
    """Stop the recorded helper and take it off the record. This install's unrecorded ones are
    stop_unrecorded's job (it reads every process's command line)."""
    results: list[Result] = []
    spec = _spec(profile, kind)
    if isinstance(spec, Result):
        return spec
    log = _log_path(profile, spec)
    with service_lock(profile.run_dir):
        state = _read_state(profile, results)
        entry = state.pop(kind, None)
        running = owned(entry)
        failed = stop_entry(profile, entry) if running else None
        if failed is not None:
            state[kind] = entry   # still alive: keep it, so a later stop tries again
        written = _write_state(profile, state, results)
    data = {"helper": kind, "log": str(log), "results": results}
    if failed is not None:
        return Result(failed.code, failed.detail, {**failed.data, **data})
    if not written:
        problem = results[-1]
        return Result(problem.code, problem.detail, data)
    if running:
        return Result("helper_stopped", f"stopped; log {log}", {**data, "pid": entry.get("pid")})
    return Result("helper_not_running", "not running", data)
