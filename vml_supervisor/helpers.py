"""The helpers: the barge-in trigger and the push-to-talk listener, one each per install, beside the
speech services and stopped with them.

Moved from FELIX voice.py (454f20d0e: start_helper, stop_unrecorded, running_commands, the stop
half of stop_barge, HELPER_START_CHECK; stop_entry is services.stop_entry, one copy). The
commands come from the profile's HelperSpec; FELIX's ensure_barge / ensure_ptt become profile
data.

HelperSpec
- argv: the command. "{mode}" in an argument is replaced by the mode; with no mode (None, the
  helper's default) every argument holding "{mode}" is dropped, so put it in one argument
  ("--mode={mode}" or a positional "{mode}").
- marker: must be non-empty and appear in argv (procs.owned matches it), or nothing is started.
- log: a file name in run_dir, or an absolute path.
- platforms: sys.platform values ("win32", "darwin", "linux"); empty means every platform.
- needs_mode: False means the mode is ignored (recorded as None), so a mode change never
  restarts it.
- status_argv (optional): the command that prints the helper's own status line ({mode} filled
  as in argv). When set, start_helper and stop_helper run it after a successful action
  (STATUS_TIMEOUT seconds, cwd=working_dir, the helper's environment) and put its first non-empty
  output line (stdout, else stderr, whatever the exit code) in Result.detail; that is
  FELIX-voice's point 8. Otherwise, or when it fails, detail is a state word and the log path.

Nothing here raises for an expected failure.

Result codes
- start_helper: helper_started, helper_running (owned, same mode: left alone), helper_restarted
  (the mode changed), helper_failed (the lock, OSError, a failed spawn, an old helper that would
  not die, or a record write: nothing new is left running), helper_exited (it stopped within
  HELPER_START_CHECK: not recorded), helper_not_started (only_if said no, under the lock),
  helper_not_for_platform, helper_command_bad (empty marker),
  helper_command_unowned (the marker is not in the command), unknown_helper.
- stop_helper: helper_stopped, helper_not_running, kill_incomplete (the entry is kept, so a later
  stop tries again), state_write_failed (also when run_dir cannot be created or locked),
  unknown_helper.
- stop_unrecorded (a list): helper_unrecorded_stopped (one per process), kill_incomplete,
  psutil_missing / process_list_unavailable (the process list could not be read: nothing was
  looked for), helper_command_bad (empty marker: nothing is looked for), unknown_helper.
- start_helper and stop_helper return one Result; what else happened is in data["results"]:
  hook_failed / hook_bad_return (a hook; its default was used), state_corrupt (read as empty),
  state_write_failed, job_unavailable (Windows: started without the job), breakaway_refused,
  job_assign_failed, resume_failed, kill_incomplete, helper_status_failed, and stop_unrecorded's
  codes.
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
from .state import read_state, service_lock, write_state
from .winjob import SpawnError, services_job, spawn

PLATFORM = sys.platform
HELPER_START_CHECK = 3.0   # seconds a new trigger or listener must stay alive before it counts as started
STATUS_TIMEOUT = 10.0      # seconds a helper's status_argv may take
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


def running_commands() -> tuple[list[tuple[int, str]], Result | None]:
    """Every process's (pid, command line), for finding helpers started but no longer on record,
    and a Result when the list could not be read (then the list is empty and proves nothing)."""
    if WINDOWS:
        try:
            import psutil
        except ImportError:
            return [], Result("psutil_missing", "psutil is not installed; the process list cannot be read")
        found = []
        for item in psutil.process_iter(["pid", "cmdline"]):
            with contextlib.suppress(psutil.Error, TypeError):
                found.append((item.info["pid"], subprocess.list2cmdline(item.info["cmdline"] or [])))
        return found, None
    try:
        done = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, check=False)
    except OSError as error:
        return [], Result("process_list_unavailable", f"ps: {error}")
    if done.returncode:
        return [], Result("process_list_unavailable", f"ps exited with {done.returncode}: "
                          f"{(done.stderr or '').strip()}", {"returncode": done.returncode})
    found = []
    for line in done.stdout.splitlines():
        pid, _, command = line.strip().partition(" ")
        if pid.isdigit():
            found.append((int(pid), command.strip()))
    return found, None


def _spec(profile: HostProfile, kind: str) -> HelperSpec | Result:
    spec = profile.helpers.get(kind)
    if spec is None:
        return Result("unknown_helper", kind, {"helper": kind})
    return spec


def _log_path(profile: HostProfile, spec: HelperSpec) -> Path:
    log = Path(spec.log)
    return log if log.is_absolute() else Path(profile.run_dir) / log


def stop_unrecorded(profile: HostProfile, kind: str) -> list[Result]:
    """Stop this install's helpers of this kind that run without a record (review, 2026-11-11: a
    watchdog from before an update cleared state.json without stopping them, and the next start
    added a second key listener). Only processes whose command holds both the helper's marker and
    profile.root followed by a separator count, so a sibling such as voice-old never matches; the
    folder is case-folded on Windows. Reads every process's command line: call it when starting or
    switching a helper off, not on every look. A recorded helper of this kind is stopped too, so
    the caller pops it from the record first."""
    spec = _spec(profile, kind)
    if isinstance(spec, Result):
        return [spec]
    if not spec.marker:
        return [Result("helper_command_bad", f"{kind}: the marker is empty, which would match every "
                       "process of this install; nothing was stopped", {"helper": kind})]
    found, problem = running_commands()
    if problem is not None:
        return [problem]
    folder = str(profile.root).rstrip("\\/") + os.sep
    folder = folder.lower() if WINDOWS else folder
    marker = spec.marker.lower()
    results: list[Result] = []
    for pid, command in found:
        text = command.lower() if WINDOWS else command
        if pid == os.getpid() or marker not in command.lower() or folder not in text:
            continue
        results.append(Result("helper_unrecorded_stopped", f"{kind}: process {pid}: {command}",
                              {"helper": kind, "pid": pid}))
        failed = stop_entry(profile, {"pid": pid, "port": None, "marker": spec.marker})
        if failed is not None:
            results.append(failed)
    return results


def _fill(argv, mode: str | None) -> list[str]:
    if mode is None:
        return [str(a) for a in argv if "{mode}" not in str(a)]
    return [str(a).replace("{mode}", mode) for a in argv]


def _environment(profile: HostProfile, hooks: Hooks, kind: str, mode, state: dict, results: list) -> dict:
    """The profile's (or its environment hook's) variables, with this helper's mode and the ports of
    the services running now, over the few the operating system needs."""
    return _runtime_and_environment(profile, hooks, kind, mode, state, results)[1]


def _runtime_and_environment(profile: HostProfile, hooks: Hooks, kind: str, mode, state: dict,
                             results: list) -> tuple[str, dict]:
    (runtime, _current), problem = hook_calls.resolve_runtime(hooks, profile)
    if problem is not None:
        results.append(problem)
    ports = {name: entry["port"] for name, entry in state.items()
             if name in profile.services and isinstance(entry, dict) and isinstance(entry.get("port"), int)
             and not isinstance(entry["port"], bool)}
    env, problem = hook_calls.environment(hooks, profile, runtime, ports, {kind: mode})
    if problem is not None:
        results.append(problem)
    return runtime, service_environment(env)


def _status_line(profile: HostProfile, spec: HelperSpec, kind: str, mode, env: dict, results: list) -> str | None:
    """The first non-empty line the helper's status_argv prints; None (with helper_status_failed in
    results) when it cannot run, times out or prints nothing; None without a status_argv."""
    if not spec.status_argv:
        return None
    command = _fill(spec.status_argv, mode)
    try:
        done = subprocess.run(command, env=env, cwd=str(profile.working_dir), stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, check=False, timeout=STATUS_TIMEOUT,
                              **background_options())
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        results.append(Result("helper_status_failed", f"{kind}: {command}: {error}", {"helper": kind}))
        return None
    for text in (done.stdout or "", done.stderr or ""):
        for line in text.splitlines():
            if line.strip():
                return line.strip()
    results.append(Result("helper_status_failed", f"{kind}: {command} printed nothing (exit code "
                          f"{done.returncode})", {"helper": kind, "returncode": done.returncode}))
    return None


def _lock(stack: contextlib.ExitStack, run_dir, held: bool) -> OSError | None:
    """Take the service lock (and hold stop signals) on the stack; the OSError when run_dir cannot
    be created or locked."""
    try:
        stack.enter_context(service_lock(run_dir))
    except OSError as error:
        return error
    if held:
        stack.enter_context(stop_signals_held())
    return None


def start_helper(profile: HostProfile, kind: str, mode=_FROM_HOOK, hooks: Hooks | None = None,
                 only_if=None) -> Result:
    """Start the helper once per install, as a child of this process (in the services' Job Object
    on Windows), and record it in state.json. Restarted when its mode changed, because a helper
    reads its mode only when it starts.

    mode omitted: ask the read_mode hook (its failure is in data["results"]).
    mode=None: the helper's default mode. A string: that mode.
    only_if: a callable checked under the lock before anything is started (the host passes "a
    connector lease is live"); False gives helper_not_started and nothing is started.

    Under the lock with stop signals held: an owned entry in the same mode is left alone; otherwise
    the old one is stopped, then this install's unrecorded ones; if any of them survives, nothing
    new is started (two key listeners must never run). The new one is recorded at once (so a stop
    during the check below finds it). Outside the lock: it must stay alive HELPER_START_CHECK
    seconds, or it is taken off the record and reported. A helper that cannot run never stops
    voice: every failure is a Result."""
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
    command = _fill(spec.argv, mode)
    if not spec.marker:
        return done("helper_command_bad", f"{kind}: the marker is empty, which would match any process",
                    command=command)
    if spec.marker not in " ".join(command):
        return done("helper_command_unowned", f"{kind}: the marker {spec.marker!r} is not in the command",
                    command=command)
    run_dir = Path(profile.run_dir)
    working_dir = Path(profile.working_dir)
    restarted = False
    with contextlib.ExitStack() as stack:
        error = _lock(stack, run_dir, held=True)
        if error is not None:
            return done("helper_failed", f"the {kind} helper could not be started: {run_dir}: {error}")
        state = read_state(profile, results)
        current = state.pop(kind, None)
        if current and owned(current) and current.get("mode") == mode:
            running = current
        elif only_if is not None and not only_if():
            return done("helper_not_started", f"the {kind} helper was not started: no connector is left")
        else:
            running = None
        if running is None:
            survivors = []
            if current:
                restarted = owned(current)
                failed = stop_entry(profile, current)
                if failed is not None:
                    results.append(failed)
                    survivors.append(failed)
                    state[kind] = current   # still alive: keep it on record, so a later stop tries again
            unrecorded = stop_unrecorded(profile, kind)
            results.extend(unrecorded)
            survivors.extend(r for r in unrecorded if r.code == "kill_incomplete")
            if survivors:
                write_state(profile, state, results)
                return done("helper_failed", f"the {kind} helper was not started: an old one is still running "
                            f"({survivors[0].detail})", command=command)
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
                runtime, env = _runtime_and_environment(profile, hooks, kind, mode, state, results)
                with log.open("a") as output:
                    # The child must not inherit the held stop signals, or it could only be killed.
                    process, spawned = spawn(command, job, env=env, cwd=str(working_dir), stdin=subprocess.DEVNULL,
                                             stdout=output, stderr=output, **background_options(), **extra)
            except SpawnError as error:
                results.append(error.result)
                write_state(profile, state, results)
                return done("helper_failed", f"the {kind} helper could not be started: {error.result.detail}",
                            command=command)
            except OSError as error:
                write_state(profile, state, results)
                return done("helper_failed", f"the {kind} helper could not be started: {error}", command=command)
            if spawned.code not in ("no_job", "breakaway"):
                results.append(spawned)
            state[kind] = {"pid": process.pid, "port": None, "marker": spec.marker, "log": str(log),
                           "mode": mode, "command": command, "runtime": runtime}
            try:
                written = write_state(profile, state, results)
            except BaseException:
                # Untracked, it would run beside the next one: stop it, then let the interrupt go on.
                stop_entry(profile, state.pop(kind))
                raise
            if not written:
                failed = stop_entry(profile, state.pop(kind))
                if failed is not None:
                    results.append(failed)
                return done("helper_failed", f"the {kind} helper was stopped: its record could not be written",
                            command=command)
    if running is not None:
        # Outside the lock: status_argv may take STATUS_TIMEOUT seconds (final review I7).
        env = _environment(profile, hooks, kind, mode, state, results)
        line = _status_line(profile, spec, kind, mode, env, results)
        return done("helper_running", line or f"already running; log {log}", pid=running.get("pid"), mode=mode)
    # The liveness check runs outside the lock, so other connectors' starts and stops do not wait.
    deadline = time.monotonic() + HELPER_START_CHECK
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.1)
    if process.poll() is None:
        word = "restarted" if restarted else "started"
        line = _status_line(profile, spec, kind, mode, env, results)
        return done(f"helper_{word}", line or f"{word}; log {log}", pid=process.pid, mode=mode)
    with contextlib.ExitStack() as stack:
        error = _lock(stack, run_dir, held=False)
        if error is not None:
            results.append(Result("state_write_failed", f"{run_dir}: {error}"))
        else:
            state = read_state(profile, results)
            entry = state.get(kind)
            if isinstance(entry, dict) and entry.get("pid") == process.pid:
                state.pop(kind)
                write_state(profile, state, results)
    return done("helper_exited", f"the {kind} helper stopped at once (exit code {process.returncode}); "
                f"see {log}", pid=process.pid, exit_code=process.returncode, mode=mode)


def stop_helper(profile: HostProfile, kind: str, hooks: Hooks | None = None) -> Result:
    """Stop the recorded helper and take it off the record, under the lock with stop signals held.
    This install's unrecorded ones are stop_unrecorded's job (it reads every process's command
    line). With a status_argv, detail is the helper's own status line afterwards."""
    results: list[Result] = []
    spec = _spec(profile, kind)
    if isinstance(spec, Result):
        return spec
    log = _log_path(profile, spec)
    data = {"helper": kind, "log": str(log), "results": results}
    run_dir = Path(profile.run_dir)
    with contextlib.ExitStack() as stack:
        error = _lock(stack, run_dir, held=True)
        if error is not None:
            return Result("state_write_failed", f"the {kind} helper could not be stopped: {run_dir}: {error}", data)
        state = read_state(profile, results)
        entry = state.pop(kind, None)
        running = owned(entry)
        failed = stop_entry(profile, entry) if running else None
        if failed is not None:
            state[kind] = entry   # still alive: keep it, so a later stop tries again
        written = write_state(profile, state, results)
    if failed is not None:
        return Result(failed.code, failed.detail, {**failed.data, **data})
    if not written:
        problem = results[-1]
        return Result(problem.code, problem.detail, data)
    word = "stopped" if running else "not running"
    line = None
    if spec.status_argv:
        mode = entry.get("mode") if isinstance(entry, dict) else None
        hooks = _hooks(profile, hooks, results)
        line = _status_line(profile, spec, kind, mode, _environment(profile, hooks, kind, mode, state, results),
                            results)
    if running:
        return Result("helper_stopped", line or f"stopped; log {log}", {**data, "pid": entry.get("pid")})
    return Result("helper_not_running", line or word, data)
