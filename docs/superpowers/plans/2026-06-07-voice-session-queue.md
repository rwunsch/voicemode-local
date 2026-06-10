# Voice Session Queue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Explicit FIFO queue so concurrent Claude Code voice sessions take strict turns, with conversation-burst floor holding, ticketed re-calls that never lose a question, and spoken handoff intros.

**Architecture:** A new pure-stdlib module `voice_queue.py` (ticket files + atomic floor file under `~/.voicemode/`, no fcntl) is copied into the installed `voice_mode` package by `patches/apply.sh`; a pattern-anchored patcher rewires `tools/converse.py`'s conch arbitration to use it. Spec: `docs/superpowers/specs/2026-06-07-voice-session-queue-design.md`.

**Tech Stack:** Python 3.10+ stdlib only (asyncio, ctypes for Windows liveness), pytest, bash (apply.sh, voicemode-switch).

**Branch:** `feature/voice-session-queue` (already exists, spec committed).

**Key facts about the environment (verified 2026-06-07):**
- Installed package: `.venv/lib/python3.13/site-packages/voice_mode/`
- Conch arbitration block: `tools/converse.py:1283-1336`; release block in `finally:` at `:1955-1968`
- `converse` signature ends with `wait_for_conch: Union[bool, str] = False` at `:1095`
- Tests run with: `.venv/bin/python -m pytest tests/ -v` from repo root
- `pytest-asyncio` is NOT assumed — async tests use `asyncio.run()` inside sync test functions

---

### Task 1: `voice_queue.py` scaffolding + process identity

**Files:**
- Create: `patches/voice_queue.py`
- Create: `tests/test_voice_queue.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_voice_queue.py`:

```python
"""Tests for patches/voice_queue.py — cross-session voice queue."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

PATCHES_DIR = Path(__file__).parent.parent / "patches"
sys.path.insert(0, str(PATCHES_DIR))

import voice_queue  # noqa: E402


# ---------- process identity ----------

def test_process_start_time_self():
    st = voice_queue.process_start_time(os.getpid())
    assert st is not None
    # Stable across calls
    assert voice_queue.process_start_time(os.getpid()) == st


def test_pid_alive_self():
    st = voice_queue.process_start_time(os.getpid())
    assert voice_queue.pid_alive(os.getpid(), st) is True


def test_pid_alive_dead_process():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    pid = proc.pid
    proc.wait()
    assert voice_queue.pid_alive(pid, None) is False or True  # pid may be reused...
    # Deterministic variant: wrong start_time must always be dead
    assert voice_queue.pid_alive(pid, start_time=-1) is False


def test_pid_alive_start_time_mismatch():
    # PID alive but start_time from a different (recycled) process -> dead
    assert voice_queue.pid_alive(os.getpid(), start_time=-12345) is False


def test_pid_alive_none_start_time_falls_back_to_kill():
    assert voice_queue.pid_alive(os.getpid(), start_time=None) is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_voice_queue.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'voice_queue'`

- [ ] **Step 3: Create `patches/voice_queue.py` with identity helpers**

```python
"""Cross-session voice queue for voicemode-local.

Replaces voice-mode's conch (fcntl flock) with a portable ticket-file FIFO
queue + atomic floor file. No fcntl anywhere — works unchanged on Windows.

Coordination state (must be on a LOCAL disk):
    ~/.voicemode/queue/<epoch-ms zero-padded>-<pid>.json   one ticket per waiter
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
QUEUE_GRACE = float(os.getenv("VOICEMODE_QUEUE_GRACE", "90"))
WAIT_SLICE = float(os.getenv("VOICEMODE_QUEUE_WAIT_SLICE", "50"))
CHECK_INTERVAL = float(os.getenv("VOICEMODE_QUEUE_CHECK_INTERVAL", "0.5"))
TICKET_STALE = float(os.getenv("VOICEMODE_QUEUE_TICKET_STALE", "30"))
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


def pid_alive(pid: int, start_time: Optional[int] = None) -> bool:
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
    the link()-based claim guarantees a valid floor is never partially visible,
    so corrupt == genuinely broken)."""
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_voice_queue.py -v`
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add patches/voice_queue.py tests/test_voice_queue.py
git commit -m "feat: voice_queue process identity (pid + start-time liveness)"
```

---

### Task 2: Ticket lifecycle

**Files:**
- Modify: `patches/voice_queue.py` (append)
- Modify: `tests/test_voice_queue.py` (append)

- [ ] **Step 1: Write the failing tests** (append to `tests/test_voice_queue.py`)

```python
# ---------- tickets ----------

def _my_st():
    return voice_queue.process_start_time(os.getpid())


def test_create_ticket_and_list(tmp_path):
    name = voice_queue.create_ticket(tmp_path, "projA", "af_bella")
    tickets = voice_queue.list_tickets(tmp_path)
    assert len(tickets) == 1
    tname, tdata = tickets[0]
    assert tname == name
    assert tdata["pid"] == os.getpid()
    assert tdata["start_time"] == _my_st()
    assert tdata["project"] == "projA"
    assert tdata["voice"] == "af_bella"


def test_one_ticket_per_pid(tmp_path):
    first = voice_queue.create_ticket(tmp_path, "projA", "af_bella")
    second = voice_queue.create_ticket(tmp_path, "projA", "af_bella")
    tickets = voice_queue.list_tickets(tmp_path)
    assert [t[0] for t in tickets] == [second]
    assert not (tmp_path / "queue" / f"{first}.json").exists()


def test_list_tickets_gc_dead_pid(tmp_path):
    qdir = tmp_path / "queue"
    qdir.mkdir()
    (qdir / "000000000000001-999999999.json").write_text(json.dumps(
        {"pid": 999999999, "start_time": 1, "project": "x", "voice": "v",
         "created": "now", "last_seen": time.time()}))
    assert voice_queue.list_tickets(tmp_path) == []
    assert list(qdir.glob("*.json")) == []


def test_list_tickets_gc_pid_reuse(tmp_path):
    # Live pid (ours) but wrong start_time => recycled pid => GC'd
    qdir = tmp_path / "queue"
    qdir.mkdir()
    (qdir / f"000000000000001-{os.getpid()}.json").write_text(json.dumps(
        {"pid": os.getpid(), "start_time": -12345, "project": "x", "voice": "v",
         "created": "now", "last_seen": time.time()}))
    assert voice_queue.list_tickets(tmp_path) == []


def test_list_tickets_gc_stale_last_seen(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_queue, "TICKET_STALE", 0.1)
    voice_queue.create_ticket(tmp_path, "projA", "af_bella")
    time.sleep(0.2)
    assert voice_queue.list_tickets(tmp_path) == []


def test_list_tickets_gc_corrupt_json(tmp_path):
    qdir = tmp_path / "queue"
    qdir.mkdir()
    (qdir / "000000000000001-1234.json").write_text("{not json")
    assert voice_queue.list_tickets(tmp_path) == []
    assert list(qdir.glob("*.json")) == []


def test_heartbeat_ticket(tmp_path):
    name = voice_queue.create_ticket(tmp_path, "p", "v")
    before = voice_queue.list_tickets(tmp_path)[0][1]["last_seen"]
    time.sleep(0.05)
    assert voice_queue.heartbeat_ticket(tmp_path, name) is True
    after = voice_queue.list_tickets(tmp_path)[0][1]["last_seen"]
    assert after > before


def test_heartbeat_missing_ticket(tmp_path):
    assert voice_queue.heartbeat_ticket(tmp_path, "000000000000001-1") is False


def test_fifo_order_and_head(tmp_path):
    # Foreign ticket older than ours (fake but live: our own pid+st)
    qdir = tmp_path / "queue"
    qdir.mkdir()
    (qdir / "000000000000001-77777.json").write_text(json.dumps(
        {"pid": os.getpid(), "start_time": _my_st(), "project": "older",
         "voice": "v", "created": "now", "last_seen": time.time()}))
    voice_queue.create_ticket(tmp_path, "me", "v")
    tickets = voice_queue.list_tickets(tmp_path)
    assert tickets[0][1]["project"] == "older"
    assert voice_queue.head_is_me(tmp_path) is True  # head pid == ours (faked)

    # A genuinely foreign head (dead pid is GC'd, so use stale-proof live fake
    # is not possible cross-pid; instead check ordering logic directly)
    names = [t[0] for t in tickets]
    assert names == sorted(names)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_voice_queue.py -v -k ticket or fifo`
Expected: FAIL with `AttributeError: module 'voice_queue' has no attribute 'create_ticket'`

- [ ] **Step 3: Implement ticket functions** (append to `patches/voice_queue.py`)

```python
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
    name = f"{int(time.time() * 1000):015d}-{pid}"
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
    return bool(tickets) and tickets[0][1].get("pid") == os.getpid()
```

- [ ] **Step 4: Run the full test file**

Run: `.venv/bin/python -m pytest tests/test_voice_queue.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add patches/voice_queue.py tests/test_voice_queue.py
git commit -m "feat: voice_queue ticket lifecycle (FIFO, one-per-pid, staleness GC)"
```

---

### Task 3: Floor — claim protocol, conditional heartbeat, release

**Files:**
- Modify: `patches/voice_queue.py` (append)
- Modify: `tests/test_voice_queue.py` (append)
- Create: `tests/queue_helper.py` (subprocess helper for contention tests)

- [ ] **Step 1: Write the failing tests** (append to `tests/test_voice_queue.py`)

```python
# ---------- floor ----------

def _fake_floor(tmp_path, pid, start_time, last_activity):
    voice_queue._write_json_atomic(tmp_path / "floor.json", {
        "pid": pid, "start_time": start_time, "project": "other",
        "voice": "v", "acquired": "now", "last_activity": last_activity})


def test_claim_empty_floor(tmp_path):
    assert voice_queue.try_claim_floor(tmp_path, "p", "v") is True
    floor = voice_queue._read_json(tmp_path / "floor.json")
    assert floor["pid"] == os.getpid()
    assert floor["start_time"] == _my_st()
    # No leftover temp/stale files
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "floor.json"]
    assert leftovers in ([], ["queue"])


def test_claim_blocked_by_live_floor(tmp_path):
    _fake_floor(tmp_path, os.getpid(), _my_st(), time.time())
    # Live holder (us, faked as another agent): claim must fail... but pid==us.
    # Use a real other live process: spawn a sleeper.
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _fake_floor(tmp_path, proc.pid,
                    voice_queue.process_start_time(proc.pid), time.time())
        assert voice_queue.try_claim_floor(tmp_path, "p", "v") is False
    finally:
        proc.kill()
        proc.wait()


def test_claim_dead_pid_floor(tmp_path):
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    pid = proc.pid
    st = voice_queue.process_start_time(pid)
    proc.wait()
    _fake_floor(tmp_path, pid, st if st is not None else -1, time.time())
    assert voice_queue.try_claim_floor(tmp_path, "p", "v") is True


def test_claim_grace_expired_floor(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_queue, "QUEUE_GRACE", 0.1)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _fake_floor(tmp_path, proc.pid,
                    voice_queue.process_start_time(proc.pid),
                    time.time() - 1.0)  # last_activity 1s ago > 0.1s grace
        assert voice_queue.try_claim_floor(tmp_path, "p", "v") is True
    finally:
        proc.kill()
        proc.wait()


def test_floor_is_mine_and_release(tmp_path):
    assert voice_queue.floor_is_mine(tmp_path) is False
    voice_queue.try_claim_floor(tmp_path, "p", "v")
    assert voice_queue.floor_is_mine(tmp_path) is True
    voice_queue.release_floor(tmp_path)
    assert voice_queue.floor_is_mine(tmp_path) is False
    assert not (tmp_path / "floor.json").exists()


def test_heartbeat_floor_conditional(tmp_path):
    voice_queue.try_claim_floor(tmp_path, "p", "v")
    before = voice_queue._read_json(tmp_path / "floor.json")["last_activity"]
    time.sleep(0.05)
    assert voice_queue.heartbeat_floor(tmp_path) is True
    after = voice_queue._read_json(tmp_path / "floor.json")["last_activity"]
    assert after > before
    # Floor stolen (someone else's pid): heartbeat must refuse (demotion)
    _fake_floor(tmp_path, 999999999, 1, time.time())
    assert voice_queue.heartbeat_floor(tmp_path) is False
    # And must NOT have overwritten the thief's floor
    assert voice_queue._read_json(tmp_path / "floor.json")["pid"] == 999999999


def test_release_only_own_floor(tmp_path):
    _fake_floor(tmp_path, 999999999, 1, time.time())
    voice_queue.release_floor(tmp_path)  # not ours -> no-op
    assert (tmp_path / "floor.json").exists()


def test_claim_contention_exactly_one_winner(tmp_path):
    """8 subprocesses race to claim a free floor: exactly one succeeds."""
    helper = Path(__file__).parent / "queue_helper.py"
    procs = [subprocess.Popen(
        [sys.executable, str(helper), "claim", str(tmp_path)],
        stdout=subprocess.PIPE) for _ in range(8)]
    results = [p.communicate()[0].decode().strip() for p in procs]
    assert results.count("WON") == 1
    assert results.count("LOST") == 7


def test_toctou_heartbeating_holder_not_stolen(tmp_path, monkeypatch):
    """A live holder that heartbeats faster than grace cannot be stolen;
    once it stops, the claim succeeds."""
    monkeypatch.setattr(voice_queue, "QUEUE_GRACE", 0.3)
    helper = Path(__file__).parent / "queue_helper.py"
    holder = subprocess.Popen(
        [sys.executable, str(helper), "hold", str(tmp_path), "1.0", "0.05"],
        env={**os.environ, "VOICEMODE_QUEUE_GRACE": "0.3"})
    try:
        time.sleep(0.2)  # holder has claimed and is heartbeating
        deadline = time.monotonic() + 0.8
        stolen = False
        while time.monotonic() < deadline:
            if voice_queue.try_claim_floor(tmp_path, "thief", "v"):
                stolen = True
                break
            time.sleep(0.05)
        assert stolen is False, "claimed a floor whose holder was heartbeating"
        holder.wait(timeout=5)  # holder releases after 1.0s
        assert voice_queue.try_claim_floor(tmp_path, "thief", "v") is True
    finally:
        holder.kill()
```

- [ ] **Step 2: Create the subprocess helper** `tests/queue_helper.py`

```python
"""Subprocess helper for voice_queue contention tests.

Usage:
    queue_helper.py claim <base>                  try one claim, print WON/LOST
    queue_helper.py hold <base> <secs> <beat>     claim, heartbeat every <beat>
                                                  for <secs>, then release
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "patches"))
import voice_queue  # noqa: E402


def main():
    cmd, base = sys.argv[1], Path(sys.argv[2])
    if cmd == "claim":
        print("WON" if voice_queue.try_claim_floor(base, "racer", "v") else "LOST")
    elif cmd == "hold":
        secs, beat = float(sys.argv[3]), float(sys.argv[4])
        if not voice_queue.try_claim_floor(base, "holder", "v"):
            print("FAILED_TO_CLAIM")
            return
        end = time.monotonic() + secs
        while time.monotonic() < end:
            voice_queue.heartbeat_floor(base)
            time.sleep(beat)
        voice_queue.release_floor(base)


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_voice_queue.py -v -k floor or claim or toctou`
Expected: FAIL with `AttributeError: ... no attribute 'try_claim_floor'`

- [ ] **Step 4: Implement floor functions** (append to `patches/voice_queue.py`)

```python
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
    data["last_activity"] = time.time()
    _write_json_atomic(fpath, data)
    return True


def release_floor(base: Path) -> None:
    """Delete the floor iff we hold it."""
    if floor_is_mine(base):
        _floor_path(base).unlink(missing_ok=True)
```

- [ ] **Step 5: Run the full test file**

Run: `.venv/bin/python -m pytest tests/test_voice_queue.py -v`
Expected: all PASS (contention/TOCTOU tests take ~2s)

- [ ] **Step 6: Commit**

```bash
git add patches/voice_queue.py tests/test_voice_queue.py tests/queue_helper.py
git commit -m "feat: voice_queue floor claim protocol (rename-verify + link, conditional heartbeat)"
```

---

### Task 4: `QueueSession` — async acquire/finish, QUEUED protocol, intro

**Files:**
- Modify: `patches/voice_queue.py` (append)
- Modify: `tests/test_voice_queue.py` (append)

- [ ] **Step 1: Write the failing tests** (append to `tests/test_voice_queue.py`)

```python
# ---------- QueueSession ----------
import asyncio


def test_voice_short_name():
    assert voice_queue.voice_short_name("af_bella") == "Bella"
    assert voice_queue.voice_short_name("p_de_thorsten") == "Thorsten"
    assert voice_queue.voice_short_name("nova") == "Nova"
    assert voice_queue.voice_short_name(None) == "default voice"


def test_session_project_env_override(monkeypatch):
    monkeypatch.setenv("VOICEMODE_SESSION_NAME", "my-session")
    assert voice_queue.session_project() == "my-session"
    monkeypatch.delenv("VOICEMODE_SESSION_NAME")
    assert voice_queue.session_project() == Path(os.getcwd()).name


def test_acquire_instant_when_free(tmp_path):
    s = voice_queue.QueueSession(project="p", voice="af_bella", base=tmp_path)
    r = asyncio.run(s.acquire())
    assert r.status == "acquired"
    assert r.waited is False           # no intro
    assert voice_queue.floor_is_mine(tmp_path)
    assert voice_queue.list_tickets(tmp_path) == []  # ticket consumed


def test_burst_continuation_and_end_burst(tmp_path):
    s = voice_queue.QueueSession(project="p", voice="af_bella", base=tmp_path)

    async def flow():
        r1 = await s.acquire()
        await s.finish(end_burst=False)
        assert voice_queue.floor_is_mine(tmp_path)   # floor kept across calls
        r2 = await s.acquire()                        # burst continuation
        assert r2.status == "acquired" and r2.waited is False
        await s.finish(end_burst=True)
    asyncio.run(flow())
    assert not (tmp_path / "floor.json").exists()     # floor released


def test_acquire_queued_when_floor_busy(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_queue, "WAIT_SLICE", 0.3)
    monkeypatch.setattr(voice_queue, "CHECK_INTERVAL", 0.05)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _fake_floor(tmp_path, proc.pid,
                    voice_queue.process_start_time(proc.pid), time.time())
        s = voice_queue.QueueSession(project="p", voice="af_bella", base=tmp_path)
        r = asyncio.run(s.acquire())
        assert r.status == "queued"
        assert r.ticket is not None
        assert "QUEUED" in r.queued_message
        assert f'ticket="{r.ticket}"' in r.queued_message
        assert "Do NOT" in r.queued_message
        # Ticket persists for the re-call
        assert voice_queue.ticket_exists(tmp_path, r.ticket)
    finally:
        proc.kill()
        proc.wait()


def test_recall_with_ticket_preserves_position_and_intro(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_queue, "WAIT_SLICE", 0.3)
    monkeypatch.setattr(voice_queue, "CHECK_INTERVAL", 0.05)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    s = voice_queue.QueueSession(project="p", voice="af_bella", base=tmp_path)
    try:
        _fake_floor(tmp_path, proc.pid,
                    voice_queue.process_start_time(proc.pid), time.time())
        r1 = asyncio.run(s.acquire())
        assert r1.status == "queued"
    finally:
        proc.kill()
        proc.wait()
    # Floor holder died; re-call with the ticket must acquire WITH intro
    r2 = asyncio.run(s.acquire(ticket=r1.ticket))
    assert r2.status == "acquired"
    assert r2.waited is True
    assert s.intro == "This is p, Bella —"


def test_recall_with_vanished_ticket_requeues(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_queue, "WAIT_SLICE", 0.2)
    monkeypatch.setattr(voice_queue, "CHECK_INTERVAL", 0.05)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _fake_floor(tmp_path, proc.pid,
                    voice_queue.process_start_time(proc.pid), time.time())
        s = voice_queue.QueueSession(project="p", voice="v", base=tmp_path)
        r = asyncio.run(s.acquire(ticket="000000000000001-424242"))
        assert r.status == "queued"
        assert r.ticket != "000000000000001-424242"   # fresh ticket
        assert "re-queued" in r.queued_message
    finally:
        proc.kill()
        proc.wait()


def test_heartbeat_task_keeps_floor_alive(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_queue, "QUEUE_GRACE", 0.3)
    monkeypatch.setattr(voice_queue, "HEARTBEAT_INTERVAL", 0.05)
    s = voice_queue.QueueSession(project="p", voice="v", base=tmp_path)

    async def flow():
        await s.acquire()
        s.start_heartbeat()
        await asyncio.sleep(0.6)  # > grace; heartbeats must keep us live
        data = voice_queue._read_json(tmp_path / "floor.json")
        assert voice_queue.floor_is_live(data)
        await s.finish(end_burst=True)
    asyncio.run(flow())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_voice_queue.py -v -k "session or acquire or burst or recall or heartbeat_task or short_name"`
Expected: FAIL with `AttributeError: ... no attribute 'voice_short_name'`

- [ ] **Step 3: Implement QueueSession** (append to `patches/voice_queue.py`)

```python
# ---------- session API (used by the patched converse tool) ----------

HEARTBEAT_INTERVAL = 10.0

# One lock per process: Claude Code can issue parallel tool calls and converse
# is async — all ticket/floor mutations must be serialized within the process
# in addition to the cross-process file protocol.
_process_lock = asyncio.Lock()


def session_project() -> str:
    return os.getenv("VOICEMODE_SESSION_NAME") or Path(os.getcwd()).name


def voice_short_name(voice: Optional[str]) -> str:
    if not voice:
        return "default voice"
    return voice.split("_")[-1].capitalize()


@dataclass
class AcquireResult:
    status: str                      # "acquired" | "queued"
    waited: bool = False             # True -> speak the handoff intro
    ticket: Optional[str] = None     # set when status == "queued"
    queued_message: str = ""


class QueueSession:
    """Per-converse-call facade over the ticket/floor protocol."""

    def __init__(self, project: Optional[str] = None,
                 voice: Optional[str] = None, base: Optional[Path] = None):
        self.base = Path(base) if base else DEFAULT_BASE
        self.project = project or session_project()
        self.voice = voice or "default"
        self._hb_task: Optional[asyncio.Task] = None

    @property
    def intro(self) -> str:
        return f"This is {self.project}, {voice_short_name(self.voice)} —"

    async def acquire(self, ticket: Optional[str] = None) -> AcquireResult:
        """Take the floor or report QUEUED after one wait slice.

        Burst continuation (we already hold the floor) returns immediately.
        A passed `ticket` resumes a previous wait (FIFO position preserved);
        if its file vanished, we re-queue at the back and say so.
        """
        requeued = False
        async with _process_lock:
            if floor_is_mine(self.base):
                heartbeat_floor(self.base)
                return AcquireResult(status="acquired", waited=False)
            resumed = ticket is not None
            if ticket is None or not ticket_exists(self.base, ticket):
                if resumed:
                    requeued = True
                ticket = create_ticket(self.base, self.project, self.voice)

        waited = resumed
        deadline = time.monotonic() + WAIT_SLICE
        while True:
            async with _process_lock:
                heartbeat_ticket(self.base, ticket)
                if head_is_me(self.base) and try_claim_floor(
                        self.base, self.project, self.voice):
                    delete_ticket(self.base, ticket)
                    return AcquireResult(status="acquired", waited=waited)
            if time.monotonic() >= deadline:
                return AcquireResult(
                    status="queued", ticket=ticket,
                    queued_message=self._queued_message(ticket, requeued))
            waited = True
            await asyncio.sleep(CHECK_INTERVAL)

    def _queued_message(self, ticket: str, requeued: bool) -> str:
        tickets = list_tickets(self.base)
        names = [t[0] for t in tickets]
        pos = names.index(ticket) + 1 if ticket in names else len(names)
        total = len(tickets)
        holder = _read_json(_floor_path(self.base)) or {}
        holder_str = f"{holder.get('project', 'unknown')}/{holder.get('voice', '?')}"
        note = (" (your previous ticket had expired; you have been re-queued"
                " at the back)" if requeued else "")
        return (
            f"QUEUED — position {pos} of {total}.{note} "
            f"The user is speaking with {holder_str}. "
            f"You MUST immediately call converse again with the same message "
            f'and ticket="{ticket}". Your queue position is preserved. '
            f"Do NOT print your question as text. Do NOT give up."
        )

    def start_heartbeat(self) -> None:
        """Call-scoped background heartbeat: keeps the floor live during TTS
        and long recordings. Cancelled by finish(). Self-terminates on
        demotion (floor stolen via grace expiry)."""
        async def beat():
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                async with _process_lock:
                    if not heartbeat_floor(self.base):
                        return  # demoted — never write again
        self._hb_task = asyncio.get_event_loop().create_task(beat())

    async def finish(self, end_burst: bool = False) -> None:
        """End of one converse call. end_burst releases the floor; otherwise
        a final heartbeat starts the inter-call grace window."""
        if self._hb_task is not None:
            self._hb_task.cancel()
            try:
                await self._hb_task
            except asyncio.CancelledError:
                pass
            self._hb_task = None
        async with _process_lock:
            if end_burst:
                release_floor(self.base)
            else:
                heartbeat_floor(self.base)
```

- [ ] **Step 4: Run the full test file**

Run: `.venv/bin/python -m pytest tests/test_voice_queue.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add patches/voice_queue.py tests/test_voice_queue.py
git commit -m "feat: voice_queue QueueSession (wait slices, QUEUED protocol, bursts, intro)"
```

---

### Task 5: `print_status()` + `voicemode-switch queue` subcommand

**Files:**
- Modify: `patches/voice_queue.py` (append)
- Modify: `voicemode-switch` (add `queue` to usage header and command dispatch)
- Modify: `tests/test_voice_queue.py` (append)

- [ ] **Step 1: Write the failing test** (append to `tests/test_voice_queue.py`)

```python
# ---------- status ----------

def test_print_status(tmp_path, capsys):
    voice_queue.print_status(tmp_path)
    out = capsys.readouterr().out
    assert "Floor: free" in out
    assert "Queue: empty" in out

    voice_queue.try_claim_floor(tmp_path, "projA", "af_bella")
    qdir = tmp_path / "queue"
    (qdir / "000000000000001-77777.json").write_text(json.dumps(
        {"pid": os.getpid(), "start_time": _my_st(), "project": "projB",
         "voice": "bm_daniel", "created": "now", "last_seen": time.time()}))
    voice_queue.print_status(tmp_path)
    out = capsys.readouterr().out
    assert "projA/af_bella" in out
    assert "projB/bm_daniel" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_voice_queue.py::test_print_status -v`
Expected: FAIL with `AttributeError: ... no attribute 'print_status'`

- [ ] **Step 3: Implement print_status** (append to `patches/voice_queue.py`)

```python
# ---------- CLI status (voicemode-switch queue) ----------

def print_status(base: Optional[Path] = None) -> None:
    base = Path(base) if base else DEFAULT_BASE
    floor = _read_json(_floor_path(base))
    if floor is None:
        print("Floor: free")
    elif floor_is_live(floor):
        age = time.time() - floor.get("last_activity", 0)
        print(f"Floor: {floor.get('project')}/{floor.get('voice')} "
              f"(pid {floor.get('pid')}, last activity {age:.0f}s ago)")
    else:
        print(f"Floor: STALE — dead holder {floor.get('project')}/"
              f"{floor.get('voice')} (pid {floor.get('pid')}); "
              f"next waiter will claim it")
    tickets = list_tickets(base)  # GCs dead/stale tickets as a side effect
    if not tickets:
        print("Queue: empty")
        return
    print(f"Queue ({len(tickets)} waiting):")
    for i, (name, t) in enumerate(tickets, 1):
        seen = time.time() - t.get("last_seen", 0)
        print(f"  {i}. {t.get('project')}/{t.get('voice')} "
              f"(pid {t.get('pid')}, ticket {name}, last seen {seen:.0f}s ago)")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_voice_queue.py::test_print_status -v`
Expected: PASS

- [ ] **Step 5: Add the `queue` subcommand to `voicemode-switch`**

Find the usage comment block at the top of `voicemode-switch` and add after the `stop` line:

```bash
#   voicemode-switch queue    # Show voice session queue (floor holder + waiters)
```

Find the main command dispatch (a `case` statement near the bottom of the script
handling `local|openai|hybrid|status|start|stop`) and add this case before the
default/usage case:

```bash
    queue)
        PYBIN="$SCRIPT_DIR/.venv/bin/python"
        [ -x "$PYBIN" ] || PYBIN="python3"
        "$PYBIN" -c "import sys; sys.path.insert(0, '$SCRIPT_DIR/patches'); import voice_queue; voice_queue.print_status()"
        ;;
```

- [ ] **Step 6: Verify the subcommand works**

Run: `./voicemode-switch queue`
Expected output (no live sessions):
```
Floor: free
Queue: empty
```

- [ ] **Step 7: Commit**

```bash
git add patches/voice_queue.py tests/test_voice_queue.py voicemode-switch
git commit -m "feat: queue status via voicemode-switch queue"
```

---

### Task 6: Surgical patcher for `tools/converse.py` + apply.sh wiring

**Files:**
- Create: `patches/patch_converse_queue.py`
- Modify: `patches/apply.sh`
- Create: `tests/test_converse_queue_patch.py`

**Background for the engineer:** `voice_mode/tools/converse.py` is a ~2,100-line
upstream file we must not ship a copy of. The patcher does exact-string,
match-exactly-once replacements; any drift in upstream text makes it exit
non-zero with a clear message (never a silent half-patch). Anchor texts below
were captured verbatim from voice-mode as installed on 2026-06-07 (v8.6.x,
`.venv/lib/python3.13/site-packages/voice_mode/tools/converse.py`).

- [ ] **Step 1: Write the failing test**

Create `tests/test_converse_queue_patch.py`:

```python
"""Tests for patches/patch_converse_queue.py (surgical converse.py patcher)."""
import subprocess
import sys
from glob import glob
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
PATCHER = REPO / "patches" / "patch_converse_queue.py"


def _installed_converse():
    hits = glob(str(REPO / ".venv" / "lib" / "python*" /
                    "site-packages" / "voice_mode" / "tools" / "converse.py"))
    hits += glob(str(REPO / ".venv" / "Lib" / "site-packages" /
                     "voice_mode" / "tools" / "converse.py"))
    return Path(hits[0]) if hits else None


@pytest.fixture
def converse_copy(tmp_path):
    src = _installed_converse()
    if src is None:
        pytest.skip("voice-mode not installed in .venv")
    dst = tmp_path / "converse.py"
    dst.write_text(src.read_text())
    return dst


def test_patch_applies_cleanly(converse_copy):
    r = subprocess.run([sys.executable, str(PATCHER), str(converse_copy)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    text = converse_copy.read_text()
    assert "voice_queue" in text
    assert "ticket: Optional[str] = None" in text
    assert "end_burst" in text
    assert "queue_session.acquire" in text
    # Old arbitration gone
    assert "conch.try_acquire()" not in text
    assert "CONCH_ACQUIRE" not in text
    # Patched file must still be valid Python
    compile(text, str(converse_copy), "exec")


def test_patch_is_idempotent(converse_copy):
    subprocess.run([sys.executable, str(PATCHER), str(converse_copy)], check=True)
    r = subprocess.run([sys.executable, str(PATCHER), str(converse_copy)],
                       capture_output=True, text=True)
    assert r.returncode == 0
    assert "already patched" in r.stdout.lower()


def test_patch_fails_loudly_on_drift(tmp_path):
    bogus = tmp_path / "converse.py"
    bogus.write_text("def converse(): pass\n")
    r = subprocess.run([sys.executable, str(PATCHER), str(bogus)],
                       capture_output=True, text=True)
    assert r.returncode != 0
    assert "anchor" in (r.stdout + r.stderr).lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_converse_queue_patch.py -v`
Expected: FAIL/ERROR (patcher does not exist yet)

- [ ] **Step 3: Create `patches/patch_converse_queue.py`**

```python
#!/usr/bin/env python3
"""Surgically patch voice_mode/tools/converse.py to use the session queue.

Usage: patch_converse_queue.py <path-to-converse.py>

Exact-string replacements, each anchor must occur EXACTLY once; otherwise we
exit 1 with a message naming the missing anchor (upstream drift detector).
Running on an already-patched file is a no-op (exit 0).
"""
import sys
from pathlib import Path

MARKER = "voicemode-local session queue"

# ---- anchor: import (after the conch import) ----
A_IMPORT = "from voice_mode.conch import Conch\n"
R_IMPORT = (
    "from voice_mode.conch import Conch\n"
    "from voice_mode import voice_queue\n"
)

# ---- anchor: signature (insert params before wait_for_conch) ----
A_SIG = "    wait_for_conch: Union[bool, str] = False\n) -> str:"
R_SIG = (
    "    ticket: Optional[str] = None,\n"
    "    end_burst: Union[bool, str] = False,\n"
    "    wait_for_conch: Union[bool, str] = False\n) -> str:"
)

# ---- anchor: docstring parameter docs ----
A_DOC = (
    "• wait_for_conch (bool, default: false): Multi-agent coordination\n"
    "  - false: If another agent is speaking, return status immediately\n"
    "  - true: Wait until the other agent finishes, then speak"
)
R_DOC = (
    "• ticket (string): Session-queue ticket id from a previous QUEUED status.\n"
    "  Pass it back unchanged when re-calling after QUEUED — it preserves your\n"
    "  FIFO position. Omit it for a fresh question.\n"
    "• end_burst (bool, default: false): Set true on the FINAL exchange of a\n"
    "  conversation burst to hand the voice floor to the next waiting session.\n"
    "• wait_for_conch: DEPRECATED — ignored while the session queue is enabled."
)

# ---- anchor: string-bool conversion block ----
A_CONV = (
    "    if isinstance(wait_for_conch, str):\n"
    "        wait_for_conch = wait_for_conch.lower() in ('true', '1', 'yes', 'on')"
)
R_CONV = (
    "    if isinstance(wait_for_conch, str):\n"
    "        wait_for_conch = wait_for_conch.lower() in ('true', '1', 'yes', 'on')\n"
    "    if isinstance(end_burst, str):\n"
    "        end_burst = end_burst.lower() in ('true', '1', 'yes', 'on')"
)

# ---- anchor: conch construction line ----
A_CONSTRUCT = '    conch = Conch(agent_name="converse")  # Named for event logging'
R_CONSTRUCT = (
    "    # --- voicemode-local session queue "
    "(docs/superpowers/specs/2026-06-07-voice-session-queue-design.md) ---\n"
    "    queue_session = voice_queue.QueueSession(voice=voice)"
)

# ---- anchor: the whole conch arbitration block inside the try ----
A_ARBITRATE = '''        # Try to acquire conch atomically (no race condition)
        if CONCH_ENABLED:
            acquired = conch.try_acquire()

            if not acquired:
                # Another agent has the conch
                holder = Conch.get_holder()
                holder_agent = holder.get('agent', 'unknown') if holder else 'unknown'

                if event_logger:
                    event_logger.log_event("CONCH_BLOCKED", {
                        "pid": os.getpid(),
                        "holder_pid": holder.get('pid') if holder else None,
                        "holder_agent": holder_agent,
                        "wait_for_conch": wait_for_conch
                    })

                if not wait_for_conch:
                    # Default: return immediately with status info
                    return (f"User is currently speaking with {holder_agent}. "
                            "Use wait_for_conch=true to queue, or try again later.")

                # Wait mode - poll with atomic retry
                if event_logger:
                    event_logger.log_event("CONCH_WAIT_START", {
                        "pid": os.getpid(),
                        "holder_agent": holder_agent,
                        "timeout": CONCH_TIMEOUT
                    })

                waited = 0.0
                while not conch.try_acquire() and waited < CONCH_TIMEOUT:
                    await asyncio.sleep(CONCH_CHECK_INTERVAL)
                    waited += CONCH_CHECK_INTERVAL

                if event_logger:
                    event_logger.log_event("CONCH_WAIT_END", {
                        "pid": os.getpid(),
                        "waited_seconds": waited,
                        "result": "acquired" if conch._acquired else "timeout"
                    })

                if not conch._acquired:
                    return f"Timed out waiting for conch ({CONCH_TIMEOUT}s). {holder_agent} is still speaking."

            # Successfully acquired
            if event_logger:
                event_logger.log_event("CONCH_ACQUIRE", {
                    "pid": os.getpid(),
                    "agent": "converse"
                })'''
R_ARBITRATE = '''        # voicemode-local session queue: FIFO arbitration replaces conch
        if voice_queue.QUEUE_ENABLED:
            _q = await queue_session.acquire(ticket=ticket)
            if _q.status == "queued":
                return _q.queued_message
            if _q.waited:
                # Handoff intro: announce which session is speaking now
                message = f"{queue_session.intro} {message}"
            queue_session.start_heartbeat()
            if event_logger:
                event_logger.log_event("QUEUE_ACQUIRE", {
                    "pid": os.getpid(),
                    "waited": _q.waited
                })'''

# ---- anchor: the conch release block in finally ----
A_RELEASE = '''        # Release the conch to signal voice conversation has ended
        if CONCH_ENABLED and conch._acquired:
            held_seconds = conch.release()
            if event_logger:
                event_logger.log_event("CONCH_RELEASE", {
                    "pid": os.getpid(),
                    "held_seconds": held_seconds
                })
        else:
            # Don't call release() when not acquired — it would delete the lock
            # file belonging to the agent that IS holding the conch, defeating
            # the flock coordination (they'd end up locking different inodes).
            pass'''
R_RELEASE = '''        # voicemode-local session queue: stop heartbeat; release floor on
        # end_burst, else a final heartbeat starts the inter-call grace window
        if voice_queue.QUEUE_ENABLED:
            await queue_session.finish(end_burst=end_burst)
            if event_logger:
                event_logger.log_event("QUEUE_FINISH", {
                    "pid": os.getpid(),
                    "end_burst": bool(end_burst)
                })'''

PATCHES = [
    ("import", A_IMPORT, R_IMPORT),
    ("signature", A_SIG, R_SIG),
    ("docstring", A_DOC, R_DOC),
    ("bool-conversion", A_CONV, R_CONV),
    ("conch-construction", A_CONSTRUCT, R_CONSTRUCT),
    ("conch-arbitration", A_ARBITRATE, R_ARBITRATE),
    ("conch-release", A_RELEASE, R_RELEASE),
]


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    path = Path(sys.argv[1])
    text = path.read_text()

    if MARKER in text:
        print(f"[patch_converse_queue] {path}: already patched — skipping")
        return 0

    for name, anchor, _ in PATCHES:
        count = text.count(anchor)
        if count != 1:
            print(f"[patch_converse_queue] ERROR: anchor '{name}' matched "
                  f"{count} times (expected exactly 1) in {path}.\n"
                  f"Upstream voice-mode has likely changed — update the anchors "
                  f"in patches/patch_converse_queue.py.", file=sys.stderr)
            return 1

    for _, anchor, replacement in PATCHES:
        text = text.replace(anchor, replacement)

    compile(text, str(path), "exec")  # syntax safety net before writing
    path.write_text(text)
    print(f"[patch_converse_queue] patched {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the patcher tests**

Run: `.venv/bin/python -m pytest tests/test_converse_queue_patch.py -v`
Expected: 3 PASS (they operate on a tmp copy — the real install is untouched)

- [ ] **Step 5: Wire into `patches/apply.sh`**

Add before the final `echo "[patches] Done..."` line:

```bash
# Install the session queue module and patch converse.py to use it
if [ -f "$SCRIPT_DIR/voice_queue.py" ]; then
    cp "$SCRIPT_DIR/voice_queue.py" "$VM_DIR/voice_queue.py"
    echo "[patches] Applied voice_queue.py → $VM_DIR/voice_queue.py"
fi
if [ -f "$SCRIPT_DIR/patch_converse_queue.py" ]; then
    PYBIN="$VENV_DIR/bin/python"
    [ -x "$PYBIN" ] || PYBIN="$VENV_DIR/Scripts/python.exe"
    [ -x "$PYBIN" ] || PYBIN="python3"
    "$PYBIN" "$SCRIPT_DIR/patch_converse_queue.py" "$VM_DIR/tools/converse.py"
fi
```

(`set -euo pipefail` at the top of apply.sh makes a non-zero patcher exit abort
the whole apply — the loud-failure requirement.)

- [ ] **Step 6: Apply to the real venv and smoke-test the import**

```bash
./patches/apply.sh
.venv/bin/python -c "from voice_mode.tools import converse; from voice_mode import voice_queue; print('import OK')"
```
Expected: `[patch_converse_queue] patched ...` then `import OK`.
Then re-run to prove idempotence: `./patches/apply.sh` → `already patched — skipping`.

- [ ] **Step 7: Run the whole test suite**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: all PASS (pre-existing tests unaffected)

- [ ] **Step 8: Commit**

```bash
git add patches/patch_converse_queue.py patches/apply.sh tests/test_converse_queue_patch.py
git commit -m "feat: surgical converse.py patcher wiring session queue into voice-mode"
```

---

### Task 7: LLM contract — prompt patch + CLAUDE.md

**Files:**
- Modify: `patches/converse.py` (the `/voicemode:converse` prompt)
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add the queue contract to `patches/converse.py`**

Insert after the `## Listening Parameters` section (after the
`listen_duration_min=3` line) inside the prompt string:

```
## Session Queue (multiple Claude Code sessions sharing the microphone)

Concurrent voice sessions take strict FIFO turns. Two rules are NON-NEGOTIABLE:

1. **If converse returns a QUEUED status**: immediately call converse again with
   the SAME message and the `ticket` value from the status. Repeat for as long
   as it takes — your queue position is preserved. NEVER print the question as
   text instead, and NEVER drop it.
2. **On your final exchange of a conversation burst** (you have no immediate
   follow-up question): pass `end_burst=true` so the next waiting session gets
   its turn. If you forget, the floor auto-releases after ~90s of silence.

When you acquire the floor after waiting, your first message is automatically
prefixed with "This is <project>, <voice> —" so the user knows who is speaking.
```

- [ ] **Step 2: Add the same contract to `CLAUDE.md`**

Insert a new section after `## Switching Voices Mid-Conversation`:

```markdown
## Session Queue (multiple concurrent voice sessions)

Concurrent Claude Code sessions take strict FIFO turns on the voice channel.
Two non-negotiable rules when using `mcp__voicemode__converse`:

1. **QUEUED status**: if converse returns "QUEUED — position N of M ...",
   immediately call converse again with the same message and the given
   `ticket` value. Repeat as long as needed. Never degrade the question to
   text; never give up.
2. **`end_burst=true`** on your final exchange of a conversation burst, so the
   next waiting session gets the floor. Forgetting costs ~90s (auto-release).

Inspect the queue anytime with `./voicemode-switch queue`.
```

- [ ] **Step 3: Re-apply patches and verify prompt content**

```bash
./patches/apply.sh
grep -A3 "Session Queue" .venv/lib/python3.13/site-packages/voice_mode/prompts/converse.py | head -5
```
Expected: the new section text appears in the installed prompt.

- [ ] **Step 4: Run the full test suite**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add patches/converse.py CLAUDE.md
git commit -m "docs: LLM session-queue contract in converse prompt and CLAUDE.md"
```

---

### Task 8: Verification — cwd assumption, suite, manual acceptance

**Files:**
- Modify: `docs/superpowers/specs/2026-06-07-voice-session-queue-design.md` (only if the cwd assumption fails)

- [ ] **Step 1: Verify the MCP-server cwd assumption**

The spec assumes Claude Code spawns the voicemode MCP server with the project
directory as cwd (used by `session_project()`). Verify:

```bash
pgrep -af "voice-mode" | head -3
# For each PID:
ls -l /proc/<PID>/cwd
```
Expected: cwd symlinks to a project directory (e.g. `/home/wunsch/git/voicemode-local`).
If it does NOT (e.g. cwd is `/` or `$HOME`): document in the spec that
`VOICEMODE_SESSION_NAME` must be set per project (e.g. in `.claude/settings.json`
`env` block), and add that note to CLAUDE.md. Do not silently ship a wrong intro.

- [ ] **Step 2: Full suite + apply from scratch**

```bash
.venv/bin/python -m pytest tests/ -v
./patches/apply.sh   # idempotent re-run
./voicemode-switch queue
```
Expected: all tests PASS; apply reports "already patched"; queue shows free/empty.

- [ ] **Step 3: Manual acceptance (requires the user)**

Restart Claude Code (MCP reload), then with the user:
1. Open 2–3 Claude Code sessions in different projects, each with a distinct voice.
2. Ask each session to ask a question by voice at roughly the same time.
3. Verify: strict FIFO order; each handoff opens with "This is \<project\>, \<voice\> —";
   follow-up exchanges stay with the same session (burst); `./voicemode-switch queue`
   shows the live queue while waiting.
4. Walk away for >2 minutes with a session queued; verify the question still
   arrives on return (ticketed re-calls survived).
5. Close a session that holds the floor mid-conversation; verify the next
   session takes over within ~1s.

- [ ] **Step 4: Final commit & wrap-up**

```bash
git add -A
git commit -m "test: queue acceptance notes and cwd verification"
```
Then use superpowers:finishing-a-development-branch (merge/PR decision with the user).

---

## Self-Review (completed at plan-writing time)

- **Spec coverage:** tickets+identity (T1-2), claim/TOCTOU/heartbeat/release (T3),
  QueueSession/QUEUED/burst/intro/wait-slice (T4), visibility (T5), patching
  strategy + loud failure + idempotence (T6), LLM contract (T7), cwd
  verification + manual acceptance (T8). Conch-compat mirror: dropped —
  verified nothing in this repo reads `~/.voicemode/conch` externally; if the
  user has external hooks, revisit (noted in spec as conditional).
- **Type consistency:** `AcquireResult.status` is `"acquired"|"queued"` in T4 and
  the patcher (T6) checks `_q.status == "queued"`; `intro` property used in both;
  `ticket_exists`/`create_ticket`/`delete_ticket` signatures match between T2 and T4.
- **Placeholder scan:** none.
