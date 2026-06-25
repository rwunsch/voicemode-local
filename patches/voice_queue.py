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
QUEUE_GRACE = float(os.getenv("VOICEMODE_QUEUE_GRACE", "30"))  # seconds (must stay > HEARTBEAT_INTERVAL)
# Max continuous floor hold before a session yields to waiters. The yield is
# evaluated both at the next exchange boundary (acquire) AND when the holder
# pauses between exchanges (finish/burst_pause) — the latter is what lets a
# session that pauses to run a long tool call hand the mic off immediately
# instead of holding through the whole grace window. No effect when nobody is
# queued. Default 0 = strict FIFO (hand off to any waiter the moment we pause);
# set higher to allow sustained bursts up to that many seconds; set very high to
# restore pure burst-until-grace behavior.
MAX_HOLD = float(os.getenv("VOICEMODE_QUEUE_MAX_HOLD", "0"))  # seconds
WAIT_SLICE = float(os.getenv("VOICEMODE_QUEUE_WAIT_SLICE", "50"))  # seconds
CHECK_INTERVAL = float(os.getenv("VOICEMODE_QUEUE_CHECK_INTERVAL", "0.5"))  # seconds
TICKET_STALE = float(os.getenv("VOICEMODE_QUEUE_TICKET_STALE", "30"))  # seconds
# Upper bound on how long an in-exchange floor stays live without a heartbeat
# refresh. in_exchange keeps the floor live through a long TTS turn that blocks
# the event loop (so the heartbeat can't run), but a process whose loop is stuck
# forever must not hold the floor permanently. Past this staleness the holder is
# treated as loop-WEDGED and the floor becomes reclaimable.
IN_EXCHANGE_MAX = float(os.getenv("VOICEMODE_QUEUE_IN_EXCHANGE_MAX", "180"))  # seconds
# Wall-clock ceiling on a SINGLE in-exchange period, measured from exchange_started
# (reset at the top of every converse call) — independent of heartbeat freshness.
# IN_EXCHANGE_MAX only fires when the loop is BLOCKED (heartbeat starved, so
# last_activity ages). The complementary failure is a LIVE loop whose converse
# coroutine is wedged (e.g. a recording loop that never returns): the background
# heartbeat keeps refreshing last_activity every HEARTBEAT_INTERVAL, so staleness
# never accumulates and the floor would starve waiters forever (observed 2026-06-14
# — a hung listen held the floor 9+ min while two sessions queued). This bound caps
# such a holder. Must exceed the longest legitimate single call (TTS playback +
# listen_duration_max + STT), hence comfortably above IN_EXCHANGE_MAX.
EXCHANGE_WEDGE_MAX = float(os.getenv("VOICEMODE_QUEUE_EXCHANGE_WEDGE_MAX", "240"))  # seconds
# When other sessions are waiting, how long a holder will LISTEN to a SILENT
# user before yielding the mic (no-speech timeout), so it doesn't grip the
# floor for the full listen_duration_max while others queue. Applies ONLY while
# speech has not started — once the user speaks, the recording is never cut
# short by this (normal silence detection ends it). 0 disables (full window).
LISTEN_CAP = float(os.getenv("VOICEMODE_QUEUE_LISTEN_CAP", "8"))  # seconds
DEFAULT_BASE = Path.home() / ".voicemode"

# Per-session human label: Claude writes a short name to
# <base>/session_names/<CLAUDE_CODE_SESSION_ID>.txt at voice-mode start so
# concurrent sessions in the same repo are distinguishable on handoff. The MCP
# server inherits its launching session's CLAUDE_CODE_SESSION_ID, so it reads
# the same file. session_project() resolves env → this file → folder name.
SESSION_NAMES_DIR = "session_names"
SESSION_NAME_MAX_AGE = float(
    os.getenv("VOICEMODE_SESSION_NAME_MAX_AGE", str(7 * 24 * 3600)))  # seconds

FLOOR_NAME = "floor.json"

# Structured per-session logging — one JSONL line per queue event, appended to
# <base>/logs/queue.log by every session so all concurrent sessions interleave
# in a single tailable file (`tail -f ~/.voicemode/logs/queue.log` or
# `voicemode-switch queue-log`). Best-effort: logging never raises into the
# voice path. Disable with VOICEMODE_QUEUE_LOG=false.
QUEUE_LOG = os.getenv("VOICEMODE_QUEUE_LOG", "true").lower() in ("true", "1", "yes", "on")
LOG_NAME = "queue.log"


def _log(event: str, base: Path, **fields) -> None:
    """Append one JSONL event line; swallow all errors (debug aid, never fatal)."""
    if not QUEUE_LOG:
        return
    try:
        logdir = base / "logs"
        logdir.mkdir(parents=True, exist_ok=True)
        rec = {"ts": _now_iso(), "pid": os.getpid(), "event": event}
        rec.update(fields)
        with open(logdir / LOG_NAME, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


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


def floor_state(data: dict) -> tuple:
    """Classify a floor holder. Returns (state, detail) where state is one of:

      "live"       — holder owns the channel and is healthy; not reclaimable.
      "dead"       — holder process is gone (or PID recycled to another
                     incarnation); reclaimable.
      "wedged"     — holder is ALIVE but stuck mid-exchange past a wedge bound
                     (loop blocked or a single exchange running too long);
                     reclaimable. This is the case earlier reported as "dead"
                     even though the pid was alive — hence its own state.
      "idle_stale" — holder is alive and between turns, but quiet past
                     QUEUE_GRACE; a waiter should take over. Reclaimable.

    Single source of truth: floor_is_live() == (state == "live").

    `in_exchange` marks a holder actively inside a converse call (speaking or
    recording). Such a holder stays live even when last_activity is stale up to
    IN_EXCHANGE_MAX — a long TTS turn blocks the event loop so the heartbeat that
    refreshes last_activity may not run. Past that ceiling the holder is treated
    as wedged (loop stuck) and the floor is reclaimable, so a hung-but-alive
    process can't starve waiters forever. QUEUE_GRACE judges the *inter-exchange*
    gap (in_exchange False), where the holder is off the channel between turns
    and a waiter should take over once it goes quiet.
    """
    if not pid_alive(data.get("pid"), data.get("start_time")):
        return ("dead", "holder process gone (or PID recycled)")
    age = time.time() - data.get("last_activity", 0)
    if data.get("in_exchange"):
        # Live through a long (loop-blocking) TTS turn, but not forever. Two
        # independent wedge bounds — EITHER tripping makes the floor reclaimable:
        #   1. staleness: no heartbeat for IN_EXCHANGE_MAX -> the event loop is
        #      BLOCKED (sd write) and can't run the beat task.
        #   2. wall-clock: this single exchange has run past EXCHANGE_WEDGE_MAX ->
        #      the loop is alive (heartbeat keeps last_activity fresh) but the
        #      converse coroutine is wedged, so staleness never accumulates.
        if age > IN_EXCHANGE_MAX:
            return ("wedged", f"alive but no heartbeat for {age:.0f}s "
                    f"(event loop blocked, > IN_EXCHANGE_MAX={IN_EXCHANGE_MAX:.0f}s)")
        started = data.get("exchange_started")
        if started is not None and time.time() - started > EXCHANGE_WEDGE_MAX:
            return ("wedged", f"alive but one exchange has run "
                    f"{time.time() - started:.0f}s (> EXCHANGE_WEDGE_MAX="
                    f"{EXCHANGE_WEDGE_MAX:.0f}s — converse coroutine stuck)")
        return ("live", f"in exchange, {age:.0f}s since last activity")
    if age <= QUEUE_GRACE:
        return ("live", f"between turns, {age:.0f}s into the {QUEUE_GRACE:.0f}s grace")
    return ("idle_stale", f"between turns, idle {age:.0f}s (past "
            f"QUEUE_GRACE={QUEUE_GRACE:.0f}s)")


def floor_is_live(data: dict) -> bool:
    """True iff the floor holder owns the channel and is healthy. Thin wrapper
    over floor_state() so claim/release and status all agree on liveness."""
    return floor_state(data)[0] == "live"


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
        "acquired_epoch": time.time(),  # hold-start for MAX_HOLD; fixed across a burst
        "last_activity": time.time(),
        "in_exchange": True,  # claimed inside a converse call about to do audio
        "exchange_started": time.time(),  # wall-clock start of this exchange (EXCHANGE_WEDGE_MAX)
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


def heartbeat_floor(base: Path, in_exchange: Optional[bool] = None) -> bool:
    """Conditionally update last_activity. False == we no longer hold the
    floor (stolen via grace expiry) — caller must demote itself and never
    write again. `in_exchange`, when not None, also sets the exchange-phase flag
    (True on enter, False on the pause between exchanges)."""
    fpath = _floor_path(base)
    data = _read_json(fpath)
    if data is None or data.get("pid") != os.getpid() \
            or data.get("start_time") != process_start_time(os.getpid()):
        return False
    # RMW is safe ONLY because the holder process is the floor's sole
    # field-writer (QueueSession serializes within the process); release is an
    # unlink, not a field write.
    data["last_activity"] = time.time()
    if in_exchange is not None:
        data["in_exchange"] = in_exchange
        # Entering an exchange (re)starts its wall-clock wedge timer. Burst
        # continuation calls heartbeat_floor(in_exchange=True) at the top of each
        # converse call, so each fresh call gets a fresh EXCHANGE_WEDGE_MAX window.
        if in_exchange:
            data["exchange_started"] = data["last_activity"]
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


# ---------- cross-process AUDIO TOKEN (physical-device exclusivity) ----------
# Re-introduced 2026-06-25 DEFAULT-OFF after a regression: when this ran
# unconditionally on the converse hot path, sessions acquired the floor but
# never produced TTS (voice went silent). It is now gated behind
# VOICEMODE_AUDIO_TOKEN_ENABLED (default false) so the hot path is byte-identical
# to the known-good behaviour unless explicitly enabled, and _ensure_audio() is
# hardened to never raise or hang into converse. Enable + validate with a REAL
# converse before making it default.
#
# Purpose: the floor serializes who may SPEAK, but on WSL all sessions share one
# PulseAudio RDPSink with a ~1s+ tunnel buffer, so two processes opening output
# streams close together overlap acoustically -> stutter. The token is a narrower
# guard under the floor: a process takes it after acquiring the floor and releases
# it at finish(), so only one voice-mode holds the device at a time. Self-limiting
# (a holder wedged past AUDIO_TOKEN_MAX is reclaimable) and non-blocking (a waiter
# proceeds after AUDIO_TOKEN_WAIT — it already owns the floor).
AUDIO_TOKEN_ENABLED = os.getenv(
    "VOICEMODE_AUDIO_TOKEN_ENABLED", "false").lower() in ("true", "1", "yes", "on")
AUDIO_TOKEN_NAME = "audio.token"
AUDIO_TOKEN_MAX = float(os.getenv("VOICEMODE_AUDIO_TOKEN_MAX", "180"))  # seconds
AUDIO_TOKEN_WAIT = float(os.getenv("VOICEMODE_AUDIO_TOKEN_WAIT", "3"))  # seconds


def _audio_path(base: Path) -> Path:
    return base / AUDIO_TOKEN_NAME


def audio_token_is_live(data: dict) -> bool:
    """A token is live iff its holder exists (same incarnation) AND has held it
    less than AUDIO_TOKEN_MAX — so a wedged holder can never own the device
    forever."""
    if not pid_alive(data.get("pid"), data.get("start_time")):
        return False
    return time.time() - data.get("acquired_epoch", 0) <= AUDIO_TOKEN_MAX


def try_acquire_audio(base: Path, project: str, voice: str) -> bool:
    """One atomic attempt to take the audio token. True iff we now hold it.

    Idempotent: if we already hold it, refresh acquired_epoch and return True.
    Same claim protocol as try_claim_floor (rename-aside-verify + os.link)."""
    base.mkdir(parents=True, exist_ok=True)
    apath = _audio_path(base)

    for orphan in base.glob("audio.token.*"):
        try:
            if time.time() - orphan.stat().st_mtime > 60:
                orphan.unlink()
        except OSError:
            pass

    data = _read_json(apath)
    if (data is not None and data.get("pid") == os.getpid()
            and data.get("start_time") == process_start_time(os.getpid())):
        data["acquired_epoch"] = time.time()  # refresh our own hold
        _write_json_atomic(apath, data)
        return True
    if data is not None and audio_token_is_live(data):
        return False  # someone else holds a live token

    if apath.exists():
        stale = base / f"audio.token.stale.{uuid.uuid4().hex}"
        try:
            os.rename(apath, stale)
        except FileNotFoundError:
            pass
        else:
            snap = _read_json(stale)
            if snap is not None and audio_token_is_live(snap):
                os.replace(stale, apath)  # raced a live refresh — put it back
                return False
            stale.unlink(missing_ok=True)

    me = {
        "pid": os.getpid(),
        "start_time": process_start_time(os.getpid()),
        "project": project,
        "voice": voice,
        "acquired_epoch": time.time(),
    }
    tmp = base / f"audio.token.tmp.{os.getpid()}.{uuid.uuid4().hex}"
    tmp.write_text(json.dumps(me, indent=2))
    try:
        os.link(tmp, apath)
        return True
    except FileExistsError:
        return False
    finally:
        tmp.unlink(missing_ok=True)


def release_audio(base: Path) -> None:
    """Release the audio token iff we hold it (rename-aside-verify, like
    release_floor). Safe to call when we never held it."""
    apath = _audio_path(base)
    snap = base / f"audio.token.release.{uuid.uuid4().hex}"
    try:
        os.rename(apath, snap)
    except FileNotFoundError:
        return
    data = _read_json(snap)
    if data is not None and (data.get("pid") != os.getpid()
            or data.get("start_time") != process_start_time(os.getpid())):
        os.replace(snap, apath)  # not ours — put it back
        return
    snap.unlink(missing_ok=True)


# ---------- session API (used by the patched converse tool) ----------

HEARTBEAT_INTERVAL = 10.0  # seconds: call-scoped floor heartbeat cadence

# One lock per process: Claude Code can issue parallel tool calls and converse
# is async — all ticket/floor mutations must be serialized within the process
# in addition to the cross-process file protocol.
_process_lock = asyncio.Lock()


def _gc_session_names(base: Path) -> None:
    """Best-effort: drop label files older than SESSION_NAME_MAX_AGE so the
    directory doesn't accumulate one file per historical session. Never raises."""
    try:
        d = base / SESSION_NAMES_DIR
        if not d.is_dir():
            return
        cutoff = time.time() - SESSION_NAME_MAX_AGE
        for f in d.glob("*.txt"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)
            except OSError:
                pass
    except Exception:
        pass


def session_project(base: Optional[Path] = None) -> str:
    """Resolve this session's voice label.

    1. VOICEMODE_SESSION_NAME env (explicit / launch-time override) — wins.
    2. <base>/session_names/<CLAUDE_CODE_SESSION_ID>.txt — the label Claude
       wrote at voice-mode start; the MCP server inherited the same session id
       at spawn, so it keys on its own CLAUDE_CODE_SESSION_ID.
    3. cwd folder name (previous behaviour).
    File read is best-effort and never raises into the voice path.
    """
    name = os.getenv("VOICEMODE_SESSION_NAME")
    if name and name.strip():
        return name.strip()
    sid = os.getenv("CLAUDE_CODE_SESSION_ID")
    if sid:
        base = Path(base) if base else DEFAULT_BASE
        _gc_session_names(base)
        try:
            label = (base / SESSION_NAMES_DIR / f"{sid}.txt").read_text().strip()
            if label:
                return label
        except OSError:
            pass
    return Path(os.getcwd()).name


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
        self.project = project or session_project(self.base)
        self.voice = voice or "default"
        self._hb_task: Optional[asyncio.Task] = None
        self._acquired = False

    @property
    def intro(self) -> str:
        return f"This is {self.project}, {voice_short_name(self.voice)} —"

    def _should_yield_floor(self):
        """For a floor we currently hold, decide whether to hand it off.

        Returns (yield: bool, reason: str|None, held: float, waiters: int).
        Keep the floor iff nobody else is waiting, OR the floor is still live and
        we are within the MAX_HOLD budget. Otherwise yield — either the budget is
        spent ("max_hold") or the floor went stale ("grace") while someone waits.
        Caller must hold _process_lock. `held` is measured from the current floor
        acquisition (acquired_epoch), so it spans the whole burst, not one call.
        """
        floor = _read_json(_floor_path(self.base)) or {}
        waiters = [t for t in list_tickets(self.base)
                   if t[1].get("pid") != os.getpid()]
        live = floor_is_live(floor)
        held = time.time() - floor.get(
            "acquired_epoch", floor.get("last_activity", 0))
        if not waiters or (live and held < MAX_HOLD):
            return False, None, round(held, 1), len(waiters)
        return True, ("max_hold" if live else "grace"), round(held, 1), len(waiters)

    async def acquire(self, ticket: Optional[str] = None) -> AcquireResult:
        """Take the floor or report QUEUED after one wait slice.

        Burst continuation (we already hold the floor) returns immediately.
        A passed `ticket` resumes a previous wait (FIFO position preserved);
        if its file vanished, we re-queue at the back and say so.
        """
        requeued = False
        _log("acquire_call", self.base, project=self.project, voice=self.voice,
             ticket_in=ticket)
        async with _process_lock:
            if floor_is_mine(self.base):
                # Burst continuation — but yield at this exchange boundary if a
                # waiter exists and our hold budget is spent (max_hold) or our
                # grace already expired (a stale floor we must not illegitimately
                # self-refresh). With no waiters we always keep talking.
                yld, reason, held, nwait = self._should_yield_floor()
                if not yld:
                    heartbeat_floor(self.base, in_exchange=True)
                    self._acquired = True
                    _log("acquired_burst", self.base, project=self.project,
                         voice=self.voice, held=held, waiting=nwait)
                    if AUDIO_TOKEN_ENABLED:
                        await self._ensure_audio()
                    return AcquireResult(status="acquired", waited=False)
                # Hand off the floor and fall through to re-queue at the back.
                _log("burst_yield", self.base, project=self.project,
                     voice=self.voice, held=held, waiting=nwait, reason=reason)
                release_floor(self.base)
                self._acquired = False
            resumed = ticket is not None
            if ticket is None or not ticket_exists(self.base, ticket):
                if resumed:
                    requeued = True
                ticket = create_ticket(self.base, self.project, self.voice)
                _log("ticket_created", self.base, project=self.project,
                     voice=self.voice, ticket=ticket, requeued=requeued)

        waited = resumed
        deadline = time.monotonic() + WAIT_SLICE
        while True:
            async with _process_lock:
                heartbeat_ticket(self.base, ticket)
                if head_is_me(self.base) and try_claim_floor(
                        self.base, self.project, self.voice):
                    delete_ticket(self.base, ticket)
                    self._acquired = True
                    _log("acquired_floor", self.base, project=self.project,
                         voice=self.voice, ticket=ticket, waited=waited)
                    if AUDIO_TOKEN_ENABLED:
                        await self._ensure_audio()
                    return AcquireResult(status="acquired", waited=waited)
            if time.monotonic() >= deadline:
                msg = self._queued_message(ticket, requeued)
                holder = _read_json(_floor_path(self.base)) or {}
                _log("queued", self.base, project=self.project, voice=self.voice,
                     ticket=ticket, holder=holder.get("project"),
                     waiting=len(list_tickets(self.base)))
                return AcquireResult(status="queued", ticket=ticket,
                                     queued_message=msg)
            waited = True
            await asyncio.sleep(CHECK_INTERVAL)

    async def _ensure_audio(self) -> None:
        """Take the audio token now that we hold the floor, so no other voice-mode
        opens an output stream while we speak. Waits up to AUDIO_TOKEN_WAIT for a
        previous holder's token (and its RDP drain) to clear, then proceeds — we
        own the floor, so we are the rightful speaker. HARDENED: a hard monotonic
        deadline + bounded iteration cap guarantee it returns, and the whole body
        is wrapped so it can NEVER raise or hang into converse (the 2026-06-25
        no-voice regression). Best-effort device exclusivity only."""
        try:
            deadline = time.monotonic() + max(0.0, AUDIO_TOKEN_WAIT)
            # iteration backstop: even if the clock misbehaves, never loop forever
            for _ in range(int(max(1, AUDIO_TOKEN_WAIT / max(CHECK_INTERVAL, 0.05))) + 2):
                try:
                    if try_acquire_audio(self.base, self.project, self.voice):
                        return
                except Exception:
                    return  # token machinery unavailable -> never block audio
                if time.monotonic() >= deadline:
                    _log("audio_token_timeout", self.base, project=self.project,
                         voice=self.voice)
                    return
                await asyncio.sleep(CHECK_INTERVAL)
        except Exception:
            return  # absolutely never propagate into converse

    def _queued_message(self, ticket: str, requeued: bool) -> str:
        tickets = list_tickets(self.base)
        names = [t[0] for t in tickets]
        total = len(tickets)
        pos = names.index(ticket) + 1 if ticket in names else total + 1
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

    def effective_no_speech_timeout(self) -> Optional[float]:
        """No-speech timeout for the recording loop (see LISTEN_CAP).

        Returns LISTEN_CAP when other sessions are queued, else None (solo
        conversation, or feature disabled). The timeout may only end a
        recording in which speech NEVER started; once the user speaks, normal
        silence detection ends the recording — active speech is never
        truncated (capping listen_duration_max instead was the 2026-06-11
        mid-sentence cutoff bug). Caller holds the floor when this is called."""
        if LISTEN_CAP <= 0:
            return None
        waiters = [t for t in list_tickets(self.base)
                   if t[1].get("pid") != os.getpid()]
        return LISTEN_CAP if waiters else None

    def start_heartbeat(self) -> None:
        """Call-scoped background heartbeat: keeps the floor live during TTS
        and long recordings. Cancelled by finish(). Self-terminates on
        demotion (floor stolen via grace expiry)."""
        if self._hb_task is not None and not self._hb_task.done():
            return  # already beating — don't spawn a second heartbeat task
        async def beat():
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                async with _process_lock:
                    if not heartbeat_floor(self.base):
                        _log("demoted", self.base, project=self.project,
                             voice=self.voice)
                        return  # demoted — never write again
        self._hb_task = asyncio.get_running_loop().create_task(beat())

    async def finish(self, end_burst: bool = False) -> None:
        """End of one converse call. end_burst releases the floor; otherwise
        a final heartbeat starts the inter-call grace window.

        No-op on the floor if this session never acquired it (e.g. acquire
        returned QUEUED)."""
        if self._hb_task is not None:
            self._hb_task.cancel()
            try:
                await self._hb_task
            except asyncio.CancelledError:
                pass
            self._hb_task = None
        if AUDIO_TOKEN_ENABLED:
            # Free the audio device when a call ends (safe no-op if we never held
            # it; never raises). Off by default -> known-good behaviour untouched.
            try:
                release_audio(self.base)
            except Exception:
                pass
        if not self._acquired:
            return
        async with _process_lock:
            if end_burst:
                release_floor(self.base)
                _log("released", self.base, project=self.project,
                     voice=self.voice)
                return
            # Not an explicit end: normally we keep the floor and let the grace
            # window cover the gap to our next exchange. But if another session
            # is waiting and our hold budget is spent, hand off NOW — at the
            # pause — rather than holding the mic through a long off-channel
            # excursion (e.g. a multi-minute tool call) until grace expires.
            yld, reason, held, nwait = self._should_yield_floor()
            if yld:
                release_floor(self.base)
                self._acquired = False
                _log("burst_yield", self.base, project=self.project,
                     voice=self.voice, held=held, waiting=nwait,
                     reason=f"pause_{reason}")
            elif heartbeat_floor(self.base, in_exchange=False):
                # Leave the exchange: from here the inter-call grace window (not
                # in_exchange) governs liveness until our next converse call.
                _log("burst_pause", self.base, project=self.project,
                     voice=self.voice, held=held, waiting=nwait)
            else:
                # Floor was stolen mid-exchange (residual claim race): demote
                # ourselves so we never write to the thief's floor again.
                self._acquired = False
                _log("demoted", self.base, project=self.project,
                     voice=self.voice)


# ---------- CLI status (voicemode-switch queue) ----------

def force_reset(base: Optional[Path] = None) -> str:
    """Operator escape hatch: clear the floor and all waiting tickets.

    For when a holder is WEDGED (alive but stuck mid-exchange) and you want the
    channel back immediately instead of waiting out the wedge bound. Safe to call
    anytime — the next converse just re-claims a free floor. Returns a summary.
    """
    base = Path(base) if base else DEFAULT_BASE
    fpath = _floor_path(base)
    floor = _read_json(fpath)
    removed_floor = False
    # Rename-aside then unlink (same atomic discipline as release_floor).
    if fpath.exists():
        snap = base / f"floor.reset.{uuid.uuid4().hex}"
        try:
            os.rename(fpath, snap)
            snap.unlink(missing_ok=True)
            removed_floor = True
        except FileNotFoundError:
            pass
    n_tickets = 0
    qdir = _queue_dir(base)
    if qdir.is_dir():
        for t in qdir.glob("*.json"):
            try:
                t.unlink()
                n_tickets += 1
            except OSError:
                pass
    who = (f"{floor.get('project', '?')}/{floor.get('voice', '?')} "
           f"(pid {floor.get('pid')})") if floor else "none"
    return (f"floor reset: cleared floor (was: {who}) and {n_tickets} "
            f"queued ticket(s). Channel is now free.")


def print_status(base: Optional[Path] = None) -> None:
    base = Path(base) if base else DEFAULT_BASE
    floor = _read_json(_floor_path(base))
    if floor is None:
        print("Floor: free")
    else:
        state, detail = floor_state(floor)
        who = (f"{floor.get('project', 'unknown')}/{floor.get('voice', '?')} "
               f"(pid {floor.get('pid')})")
        if state == "live":
            print(f"Floor: {who} — {detail}")
        elif state == "dead":
            print(f"Floor: RECLAIMABLE — dead holder {who}: {detail}; "
                  f"next waiter will claim it")
        elif state == "wedged":
            print(f"Floor: RECLAIMABLE — WEDGED holder {who} is ALIVE but stuck: "
                  f"{detail}; next waiter will claim it. "
                  f"(`voicemode-switch floor reset` to clear now)")
        else:  # idle_stale
            print(f"Floor: RECLAIMABLE — idle holder {who}: {detail}; "
                  f"next waiter will claim it")
    tickets = list_tickets(base)  # GCs dead/stale tickets as a side effect
    if not tickets:
        print("Queue: empty")
        return
    print(f"Queue ({len(tickets)} waiting):")
    for i, (name, t) in enumerate(tickets, 1):
        seen = time.time() - t.get("last_seen", 0)
        print(f"  {i}. {t.get('project', 'unknown')}/{t.get('voice', '?')} "
              f"(pid {t.get('pid')}, ticket {name}, last seen {seen:.0f}s ago)")
