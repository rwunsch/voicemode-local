"""Cross-session voice queue for voicemode-local.

Replaces voice-mode's conch (fcntl flock) with a portable ticket-file FIFO
queue + atomic floor file. No fcntl anywhere — works unchanged on Windows.

Coordination state (must be on a LOCAL disk):
    ~/.voicemode/queue/<epoch-µs, 16-digit>-<pid>.json   one ticket per waiter
    ~/.voicemode/floor.json                                 current floor holder

Design: docs/superpowers/specs/2026-06-07-voice-session-queue-design.md
Installed into voice_mode/ by patches/apply.sh.
"""
import asyncio
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

QUEUE_ENABLED = os.getenv("VOICEMODE_QUEUE_ENABLED", "true").lower() in ("true", "1", "yes", "on")
# Timing constants below are all in seconds.
QUEUE_GRACE = float(os.getenv("VOICEMODE_QUEUE_GRACE", "90"))  # seconds
WAIT_SLICE = float(os.getenv("VOICEMODE_QUEUE_WAIT_SLICE", "50"))  # seconds
CHECK_INTERVAL = float(os.getenv("VOICEMODE_QUEUE_CHECK_INTERVAL", "0.5"))  # seconds
TICKET_STALE = float(os.getenv("VOICEMODE_QUEUE_TICKET_STALE", "30"))  # seconds
DEFAULT_BASE = Path.home() / ".voicemode"

FLOOR_NAME = "floor.json"


# ---------- process identity (pid + start time; PIDs get recycled) ----------

def process_start_time(pid: int) -> Optional[int]:
    """Opaque start-time token for a process, or None if unavailable.

    Linux: field 22 of /proc/<pid>/stat (clock ticks since boot).
    Windows: creation time from GetProcessTimes (FILETIME as int).
    macOS/other: None (liveness falls back to pid-only).
    """
    if sys.platform == "win32":
        import ctypes
        import ctypes.wintypes as wt
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return None
        try:
            ctime, etime, ktime, utime = (wt.FILETIME(), wt.FILETIME(),
                                          wt.FILETIME(), wt.FILETIME())
            if not k32.GetProcessTimes(h, ctypes.byref(ctime), ctypes.byref(etime),
                                       ctypes.byref(ktime), ctypes.byref(utime)):
                return None
            return (ctime.dwHighDateTime << 32) | ctime.dwLowDateTime
        finally:
            k32.CloseHandle(h)
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read()
        # comm field (2) may contain spaces/parens; fields resume after last ')'
        fields = data.rsplit(b")", 1)[1].split()
        return int(fields[19])  # field 22 overall == starttime
    except (OSError, IndexError, ValueError):
        return None


def pid_alive(pid: Optional[int], start_time: Optional[int] = None) -> bool:
    """Is the process alive AND (if start_time given) the same incarnation?

    PermissionError => alive (we may not be allowed to signal it).
    start_time mismatch => recycled PID => treated as dead.
    """
    if pid is None or pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            if code.value != STILL_ACTIVE:
                return False
        finally:
            k32.CloseHandle(h)
    else:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            pass  # exists, owned by someone else => alive
    if start_time is not None:
        actual = process_start_time(pid)
        if actual is not None and actual != start_time:
            return False
    return True


def _now_iso() -> str:
    return datetime.now().isoformat()


def _read_json(path: Path) -> Optional[dict]:
    """Parse a JSON file; None if missing or corrupt (corrupt files are removed —
    floor claims use os.link() which is atomic fail-if-exists, so corrupt ==
    disk error, not a partial write)."""
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError, ValueError):
        try:
            path.unlink()
        except OSError:
            pass
        return None


def _write_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


# NOTE concurrency contract: the ticket/floor primitives below are synchronous
# and NOT safe against concurrent calls from multiple asyncio tasks in the SAME
# process. Production code must go through QueueSession (added in a later task),
# which serializes all mutations behind one process-local asyncio.Lock. The
# cross-PROCESS safety comes from O_EXCL / os.replace / os.link semantics.

# ---------- tickets ----------

def _queue_dir(base: Path) -> Path:
    return base / "queue"


def list_tickets(base: Path) -> list:
    """All live tickets as [(name, data)] in FIFO order.

    Garbage-collects on the way: dead/recycled pids, stale last_seen
    (> TICKET_STALE, covers waiters whose LLM never re-called), corrupt JSON.
    """
    qdir = _queue_dir(base)
    if not qdir.is_dir():
        return []
    out = []
    for path in sorted(qdir.glob("*.json")):
        data = _read_json(path)  # removes corrupt files itself
        if data is None:
            continue
        if not pid_alive(data.get("pid"), data.get("start_time")):
            path.unlink(missing_ok=True)
            continue
        if time.time() - data.get("last_seen", 0) > TICKET_STALE:
            path.unlink(missing_ok=True)
            continue
        out.append((path.stem, data))
    return out


def create_ticket(base: Path, project: str, voice: str) -> str:
    """Create our ticket (one per pid — older same-pid tickets are removed to
    prevent the orphan-ticket deadlock when an LLM re-calls without `ticket`).
    Returns the ticket name (filename stem)."""
    qdir = _queue_dir(base)
    qdir.mkdir(parents=True, exist_ok=True)
    pid = os.getpid()
    for old in qdir.glob(f"*-{pid}.json"):
        old.unlink(missing_ok=True)
    name = f"{int(time.time() * 1_000_000):016d}-{pid}"
    data = {
        "pid": pid,
        "start_time": process_start_time(pid),
        "project": project,
        "voice": voice,
        "created": _now_iso(),
        "last_seen": time.time(),
    }
    fd = os.open(str(qdir / f"{name}.json"),
                 os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    try:
        os.write(fd, json.dumps(data, indent=2).encode())
    finally:
        os.close(fd)
    return name


def ticket_exists(base: Path, name: str) -> bool:
    return (_queue_dir(base) / f"{name}.json").is_file()


def heartbeat_ticket(base: Path, name: str) -> bool:
    """Update our ticket's last_seen. False if the ticket vanished."""
    path = _queue_dir(base) / f"{name}.json"
    data = _read_json(path)
    if data is None:
        return False
    data["last_seen"] = time.time()
    _write_json_atomic(path, data)
    return True


def delete_ticket(base: Path, name: str) -> None:
    (_queue_dir(base) / f"{name}.json").unlink(missing_ok=True)


def head_is_me(base: Path) -> bool:
    tickets = list_tickets(base)
    # pid-only comparison is safe ONLY because list_tickets has already GC'd
    # recycled-pid tickets (start_time mismatch); callers must not bypass it.
    return bool(tickets) and tickets[0][1].get("pid") == os.getpid()


# ---------- floor ----------

def _floor_path(base: Path) -> Path:
    return base / FLOOR_NAME


def floor_is_live(data: dict) -> bool:
    """A floor is live if its holder process exists (same incarnation) and has
    shown activity within QUEUE_GRACE seconds."""
    if not pid_alive(data.get("pid"), data.get("start_time")):
        return False
    return (time.time() - data.get("last_activity", 0)) <= QUEUE_GRACE


def try_claim_floor(base: Path, project: str, voice: str) -> bool:
    """Attempt to take the floor. True iff we are now the holder.

    Claim protocol (spec: 'Floor / Claim protocol'):
      1. Live floor -> False.
      2. Dead floor -> atomically rename it aside, re-verify the snapshot
         (a concurrent heartbeat may have raced our read; if the snapshot is
         live, put it back), else discard it.
      3. Claim by os.link(tmp, floor.json): atomic fail-if-exists WITH full
         content — readers can never observe a partial floor.
    """
    base.mkdir(parents=True, exist_ok=True)
    fpath = _floor_path(base)

    # Opportunistic GC: floor.stale/.tmp/.release orphans left by claimers
    # that crashed mid-protocol. In-flight files live for milliseconds, so
    # a 60s mtime threshold can never hit a live one.
    for orphan in base.glob("floor.*"):
        if orphan.name == FLOOR_NAME:
            continue
        try:
            if time.time() - orphan.stat().st_mtime > 60:
                orphan.unlink()
        except OSError:
            pass

    data = _read_json(fpath)
    if data is not None and floor_is_live(data):
        return False

    if fpath.exists():
        stale = base / f"floor.stale.{uuid.uuid4().hex}"
        try:
            os.rename(fpath, stale)
        except FileNotFoundError:
            pass  # someone else cleaned it up first
        else:
            snap = _read_json(stale)
            if snap is not None and floor_is_live(snap):
                # Heartbeat raced our read — the holder is alive. Put it back.
                # (Microsecond window where a third claimer could have linked a
                # new floor; rename overwrites it. Accepted residual race —
                # self-heals on the next call. See spec.)
                os.replace(stale, fpath)
                return False
            stale.unlink(missing_ok=True)

    me = {
        "pid": os.getpid(),
        "start_time": process_start_time(os.getpid()),
        "project": project,
        "voice": voice,
        "acquired": _now_iso(),
        "last_activity": time.time(),
    }
    tmp = base / f"floor.tmp.{os.getpid()}.{uuid.uuid4().hex}"
    tmp.write_text(json.dumps(me, indent=2))
    try:
        os.link(tmp, fpath)
        return True
    except FileExistsError:
        return False  # lost the race
    finally:
        tmp.unlink(missing_ok=True)


def floor_is_mine(base: Path) -> bool:
    data = _read_json(_floor_path(base))
    return (data is not None
            and data.get("pid") == os.getpid()
            and data.get("start_time") == process_start_time(os.getpid()))


def heartbeat_floor(base: Path) -> bool:
    """Conditionally update last_activity. False == we no longer hold the
    floor (stolen via grace expiry) — caller must demote itself and never
    write again."""
    fpath = _floor_path(base)
    data = _read_json(fpath)
    if data is None or data.get("pid") != os.getpid() \
            or data.get("start_time") != process_start_time(os.getpid()):
        return False
    # RMW is safe ONLY because the holder process is the floor's sole
    # field-writer (QueueSession serializes within the process); release is an
    # unlink, not a field write.
    data["last_activity"] = time.time()
    _write_json_atomic(fpath, data)
    return True


def release_floor(base: Path) -> None:
    """Delete the floor iff we hold it.

    Rename-based: atomically capture whatever floor.json currently is, then
    inspect the snapshot. If it was ours, discard it (released). If a thief
    had already claimed (grace-expiry race), put theirs back. Same residual
    put-back race class as try_claim_floor — documented in the spec.
    """
    fpath = _floor_path(base)
    snap = base / f"floor.release.{uuid.uuid4().hex}"
    try:
        os.rename(fpath, snap)
    except FileNotFoundError:
        return  # nothing to release
    data = _read_json(snap)
    if data is not None and (data.get("pid") != os.getpid()
            or data.get("start_time") != process_start_time(os.getpid())):
        os.replace(snap, fpath)  # not ours — put the rightful floor back
        return
    snap.unlink(missing_ok=True)
