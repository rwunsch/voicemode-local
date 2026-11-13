"""State files, the services lock and connector leases.

Moved from FELIX voice.py and voicemode_native.py, which carried two copies.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from .procs import process_command
from .profile import HostProfile
from .result import Result


@contextlib.contextmanager
def exclusive(path):
    """Hold a lock file for the block; released at process end on both systems."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as lock:
        if sys.platform == "win32":
            import msvcrt

            while True:
                try:
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.2)
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock, fcntl.LOCK_EX)
            yield


@contextlib.contextmanager
def service_lock(run_dir):
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    with exclusive(run_dir / "services.lock"):
        yield


def atomic_json(path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".record-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write((json.dumps(value) + "\n").encode("utf-8"))
        for attempt in range(20):  # a reader's open handle blocks a replace on Windows
            try:
                temporary.replace(path)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.05)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path, default=None):
    """(value, None); a missing file is (default, None); a corrupt one (default, state_corrupt); an unreadable one (default, state_unreadable)."""
    path = Path(path)
    try:
        # utf-8-sig: Windows PowerShell 5.1 writes a byte-order mark, which plain utf-8 rejects.
        return json.loads(path.read_text(encoding="utf-8-sig")), None
    except FileNotFoundError:
        return default, None
    except ValueError:  # includes UnicodeDecodeError: the content is the problem
        return default, Result("state_corrupt", str(path))
    except OSError as error:  # a sharing violation or permission problem is not corruption
        return default, Result("state_unreadable", f"{path}: {error}")


def write_lease(profile: HostProfile, pid: int, data: dict) -> None:
    atomic_json(Path(profile.run_dir) / "clients" / str(pid), data)


def _remove(lease: Path) -> None:
    """Best effort: a lease held open (Windows) or a directory must not break the caller."""
    try:
        lease.unlink(missing_ok=True)
    except OSError:
        pass


def live_leases(profile: HostProfile, command_of=process_command) -> list[int]:
    """Leases whose launcher, or the voice server it started, still runs; remove the rest."""
    clients = Path(profile.run_dir) / "clients"
    clients.mkdir(parents=True, exist_ok=True)
    alive: list[int] = []
    for lease in clients.iterdir():
        if lease.name.startswith("."):
            continue  # being written
        try:
            pid = int(lease.name)
        except ValueError:
            _remove(lease)
            continue
        command = command_of(pid)
        record, _ = read_json(lease, {})
        child = record.get("child") if isinstance(record, dict) else None
        child_command = command_of(child) if isinstance(child, int) and not isinstance(child, bool) and child > 0 else None
        if (command and any(m in command for m in profile.lease_markers)) or (
                child_command and profile.voice_server_marker in child_command):
            alive.append(pid)
        else:
            _remove(lease)
    return alive


def read_state(profile: HostProfile, results: list) -> dict:
    """run_dir/state.json, read under the caller's lock; a corrupt or non-object file is a Result in
    `results` and read as empty. A missing file is empty."""
    path = Path(profile.run_dir) / "state.json"
    state, problem = read_json(path, {})
    if problem is not None:
        results.append(problem)
        return {}
    if not isinstance(state, dict):
        results.append(Result("state_corrupt", f"{path}: not a JSON object"))
        return {}
    return state


def write_state(profile: HostProfile, state: dict, results: list) -> bool:
    """Write run_dir/state.json whole; an OSError is Result("state_write_failed") in `results` and
    False. Anything else propagates."""
    path = Path(profile.run_dir) / "state.json"
    try:
        atomic_json(path, state)
    except OSError as error:
        results.append(Result("state_write_failed", f"{path}: {error}"))
        return False
    return True
