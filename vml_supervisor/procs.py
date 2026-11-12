"""Process and port primitives: command lines, process trees, listeners, signals.

Moved from FELIX's voice.py (the origin; the newer fixes) and voicemode_native.py. `owned` keeps
the type checks from voicemode_native.py. psutil is optional and imported lazily, so the package
imports without it; on Windows the functions that need it say so through their return value
(None, a one-pid tree, an empty set) rather than raising at import.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import signal
import socket
import subprocess
import sys
import time

WINDOWS = sys.platform == "win32"
LOOPBACK = "127.0.0.1"
STOP_SIGNALS = {getattr(signal, name) for name in ("SIGTERM", "SIGINT", "SIGHUP") if hasattr(signal, name)}


def _psutil():
    try:
        import psutil
    except ImportError:
        return None
    return psutil


def _psutil_process(pid):
    psutil = _psutil()
    if psutil is None:
        return None
    try:
        return psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def port_free(port) -> bool:
    """True only when nothing answers on the port and it can be bound on loopback."""
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
        probe.settimeout(0.5)
        if probe.connect_ex((LOOPBACK, port)) == 0:
            return False
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
        try:
            probe.bind((LOOPBACK, port))
        except OSError:
            return False
    return True


def listener_pids(port) -> set[int]:
    """Process ids listening on a TCP port: psutil on Windows, lsof elsewhere."""
    if WINDOWS:
        psutil = _psutil()
        if psutil is None:
            return set()
        return {item.pid for item in psutil.net_connections("tcp")
                if item.status == psutil.CONN_LISTEN and item.laddr and item.laddr.port == port and item.pid}
    # By full path where it can be: a Mac keeps it in /usr/sbin, most Linux systems in /usr/bin.
    known = [path for path in ("/usr/sbin/lsof", "/usr/bin/lsof") if os.path.exists(path)]
    lsof = known[0] if known else shutil.which("lsof") or "lsof"
    result = subprocess.run([lsof, "-nP", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"],
                            capture_output=True, text=True, check=False)
    return {int(line) for line in result.stdout.split() if line.isdigit()}


def process_command(pid) -> str | None:
    """A process's command line, or None when it is gone."""
    if WINDOWS:
        psutil = _psutil()
        process = _psutil_process(pid)
        if psutil is None or process is None:
            return None
        try:
            return " ".join(process.cmdline()) or None
        except psutil.Error:
            return None
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return None
    result = subprocess.run(["/bin/ps", "-ww", "-o", "command=", "-p", str(pid)],
                            capture_output=True, text=True, check=False)
    return result.stdout.strip() or None


def process_tree(pid) -> set[int]:
    """A process and everything it started. On Windows a virtual environment's python.exe starts
    the real interpreter as a child, and it is the child that listens (FELIX, VM, 2026-11-04)."""
    if not WINDOWS:
        return {pid}
    psutil = _psutil()
    process = _psutil_process(pid)
    if psutil is None:
        return {pid}
    if process is None:
        return set()
    try:
        return {pid, *(child.pid for child in process.children(recursive=True))}
    except psutil.Error:
        return {pid}


def _alive(pid) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # a zombie still answers signal 0
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


def kill_tree(pid, timeout) -> None:
    """Stop a process and its children. Windows: kill the tree through psutil and wait. POSIX:
    SIGTERM, then SIGKILL once `timeout` seconds pass."""
    if WINDOWS:
        psutil = _psutil()
        process = _psutil_process(pid)
        if psutil is None or process is None:
            return
        try:
            members = [*process.children(recursive=True), process]
        except psutil.Error:
            members = [process]
        for member in members:
            with contextlib.suppress(psutil.Error):
                member.kill()
        psutil.wait_procs(members, timeout=timeout)
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return
        time.sleep(0.05)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGKILL)


def owned(entry, command_of=process_command) -> bool:
    """A recorded service counts as ours only while its command still matches. Anything that is
    not a dict with a positive int pid (a bool included) is not ours."""
    if not isinstance(entry, dict):
        return False
    pid = entry.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    command = command_of(pid)
    if not command or entry.get("marker", "\0") not in command:
        return False
    return entry.get("port") is None or f"--port {entry['port']}" in command


def unblock_stop_signals() -> None:
    # A child inherits the blocked-signal mask; the service must still stop on SIGTERM.
    if not WINDOWS:
        signal.pthread_sigmask(signal.SIG_UNBLOCK, STOP_SIGNALS)


@contextlib.contextmanager
def stop_signals_held():
    """Hold stop signals until a new service is recorded, so cleanup can find it. Windows has no
    signal mask, and nothing delivers these signals to a process there."""
    if WINDOWS:
        yield
        return
    blocked = signal.pthread_sigmask(signal.SIG_BLOCK, STOP_SIGNALS)
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, blocked)


def connector_options() -> dict:
    """The voice server talks to the app over this process's own input and output. A Mac passes
    those on by itself. On Windows, Python passes on nothing unless named, and the server then
    never hears a request (laptop, 2026-11-04: `initialize` got no answer in 120 s)."""
    if not WINDOWS:
        return {}
    return {"stdin": sys.stdin, "stdout": sys.stdout, "stderr": sys.stderr,
            "creationflags": subprocess.CREATE_NO_WINDOW}


def background_options() -> dict:
    """How to start a process that outlives this one, with no window."""
    if WINDOWS:
        return {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}
