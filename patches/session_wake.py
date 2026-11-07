"""Wake an idle Claude Code session so it starts a voice turn (stdlib only).

Used by push-to-talk (a hold of the key while no voice turn is running: the
native Windows sender in ``ptt_control_client --stdin`` and the WSL relay) and
by the voice pill's Play button. The Mac keeps its own Terminal typing wake
(ptt_listener_macos).

How (Robert's decision, 2026-11-07): Claude Code's own session socket. This
protocol is UNDOCUMENTED -- read from the Claude Code binary's strings and
validated once (n=1, WSL, Claude Code 2.1.319: an idle session started a turn
1.8 s after the exchange). It is used by permission of the user, for his own
sessions only, and may change in any Claude Code release:

* registry: ``<claude dir>/sessions/<claude-pid>.json`` with ``status`` (idle,
  busy, waiting, shell), ``messagingSocketPath``, ``peerProtocol`` (1) and
  ``procStart``; the claude dir is ``CLAUDE_CONFIG_DIR``, else ``~/.claude``;
* token: ``<claude dir>/sessions/<pid>.<hash>.key`` = ``{"peerToken", "procStart"}``,
  mode 600;
* on ONE connection, two newline-delimited JSON lines:
  ``{"type":"auth","token":...}`` then
  ``{"type":"user","message":{"role":"user","content":<text>}}``. Nothing comes
  back. The session shows it as "Another Claude session sent a message".

Safety rules, all enforced here:

* refuses unless the registry says ``idle`` (``waiting`` is a permission prompt:
  refused too), ``peerProtocol == 1``, the registry's process start matches the
  live process (Linux; a stale entry for a reused pid is refused), the key file
  is private and from the same process start, and the socket is a socket owned
  by this user with no group/other access, in a directory nobody else can write;
* one fixed message per reason (``MESSAGES``); callers cannot pass text;
* the token is never printed, logged or returned;
* at most one send every ``RATE_LIMIT_S`` seconds per process;
* ``VOICEMODE_SESSION_WAKE=off`` (environment or voicemode.env) turns it off;
* never raises: ``wake`` returns ``(ok, detail)``.

Finding the session: the voice-mode MCP server's parent IS the claude process
(verified n=5 on WSL). "The last voice session" is found as the Mac listener
does it: the newest conch holder in voice-mode's events log that is still alive,
then the pill's session records by last voice use. Each voice-server pid is
mapped up its parents (at most ``MAX_PARENT_HOPS``, for launchers in between) to
the first pid that has a registry entry.

Windows native: UNVERIFIED. The binary's strings show a named pipe
(``\\\\.\\pipe\\...``) and "auth optional on this platform". When the registry's
``messagingSocketPath`` is a pipe name it is opened with control_transport's
owner-checked pipe client (same user, integrity at least ours), and the same two
lines are sent. Not run against a real Windows Claude Code; there is no process
start check (format unknown) and no key file permission check (NTFS ACLs) there.

Size: no cap was found in the binary for this path, so ``MAX_MESSAGE_BYTES`` is
our own conservative limit. The Mac's "long lines are taken for a paste" was
about typed keystrokes, not this socket; it does not apply here.

Installed into voice_mode/session_wake.py by patches/apply.py.
"""
from __future__ import annotations

import glob
import importlib.util
import json
import logging
import os
import socket
import stat
import subprocess
import sys
import threading
import time
from collections import namedtuple
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("voicemode.session_wake")

SETTING = "VOICEMODE_SESSION_WAKE"
PEER_PROTOCOL = 1
RATE_LIMIT_S = 5.0
SEND_TIMEOUT_S = 1.0
MAX_MESSAGE_BYTES = 400
MAX_PARENT_HOPS = 4
LOG_MAX_BYTES = 256 * 1024
EVENTS_TAIL_BYTES = 256 * 1024

# Each names a message: voice-mode 8.12's converse refuses a call with neither
# `message` nor `turns` ("converse needs either `message` ..."), seen live on a Mac
# 2026-11-07. With skip_tts=true the message is not spoken and converse listens at
# once (8.12 converse.py: should_skip_tts makes TTS a no-op).
MESSAGES = {
    "ptt": ('Push-to-talk: the user is holding the key to speak to you. Call the voicemode '
            'converse tool now with message="Listening.", skip_tts=true and '
            'wait_for_response=true to listen, then answer them by voice.'),
    "play": ("Voice pill Play: the user wants to talk. Call the voicemode converse tool now "
             "with a short greeting as the message and wait_for_response=true, then keep "
             "the voice conversation going."),
}

STATUS_REASONS = {
    "busy": "busy",
    "waiting": "waiting for a permission answer",
    "shell": "running a shell command",
}

_lock = threading.Lock()
_last_sent: Optional[float] = None


def _now() -> float:
    return time.monotonic()


# -- helpers from the files beside this one ----------------------------------

_STATE_FILE = Path(__file__).with_name("voice_pill_state.py")
_STATE = None
_TRANSPORT_FILE = Path(__file__).with_name("control_transport.py")
_TRANSPORT = None


def _load_beside(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _pill_state():
    """voice_pill_state: the installed voice_mode's, else the file beside this one."""
    global _STATE
    for name in ("voice_mode.voice_pill_state", "voice_pill_state"):
        if name in sys.modules:
            return sys.modules[name]
    if _STATE is None:
        _STATE = _load_beside(_STATE_FILE, "_vml_wake_pill_state")
    return _STATE


def _control_transport():
    global _TRANSPORT
    installed = sys.modules.get("voice_mode.control_transport")
    if installed is not None:
        return installed
    if _TRANSPORT is None:
        if not _TRANSPORT_FILE.is_file():
            raise OSError(f"no control transport at {_TRANSPORT_FILE}")
        _TRANSPORT = _load_beside(_TRANSPORT_FILE, "_vml_wake_control_transport")
    return _TRANSPORT


# -- settings and places -----------------------------------------------------

def enabled(home=None) -> bool:
    """On unless VOICEMODE_SESSION_WAKE is off/false/0/no (environment, then voicemode.env)."""
    value = _pill_state().setting(SETTING, "on", home)
    return str(value or "on").strip().lower() not in ("off", "false", "0", "no")


def claude_dir() -> Path:
    explicit = os.environ.get("CLAUDE_CONFIG_DIR")
    if explicit:
        return Path(os.path.expanduser(explicit))
    return Path.home() / ".claude"


def _sessions_dir(cdir=None) -> Path:
    return Path(cdir) / "sessions" if cdir is not None else claude_dir() / "sessions"


def read_registry(pid, cdir=None) -> Optional[dict]:
    try:
        data = json.loads((_sessions_dir(cdir) / f"{int(pid)}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def registry_status(pid, cdir=None) -> Optional[str]:
    reg = read_registry(pid, cdir)
    return reg.get("status") if reg else None


def is_pipe(path: str) -> bool:
    p = str(path).replace("/", "\\").lower()
    return p.startswith("\\\\.\\pipe\\")


# -- process tree ------------------------------------------------------------

def parent_pid(pid) -> Optional[int]:
    """Parent of pid, or None. Linux /proc; Windows Toolhelp (UNVERIFIED); else ps."""
    try:
        pid = int(pid)
        if sys.platform.startswith("linux"):
            with open(f"/proc/{pid}/stat", "rb") as f:
                data = f.read().decode("ascii", "replace")
            return int(data[data.rindex(")") + 2:].split()[1])
        if sys.platform == "win32":
            return _win_parent_pid(pid)
        out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=2).stdout.strip()
        return int(out) if out else None
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def _win_parent_pid(pid: int) -> Optional[int]:
    """UNVERIFIED: the parent pid from a Toolhelp process snapshot."""
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == wintypes.HANDLE(-1).value:
        return None
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.th32ProcessID == pid:
                return int(entry.th32ParentProcessID) or None
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
        return None
    finally:
        k32.CloseHandle(snap)


def claude_pid_for(voice_pid, cdir=None, parent: Callable = parent_pid) -> Optional[int]:
    """The first ancestor of the voice server that has a Claude Code registry entry."""
    pid = voice_pid
    for _ in range(MAX_PARENT_HOPS):
        pid = parent(pid)
        if not isinstance(pid, int) or pid <= 1:
            return None
        reg = read_registry(pid, cdir)
        if reg and reg.get("pid") == pid:
            return pid
    return None


# -- the last voice session --------------------------------------------------

# A namedtuple, not a dataclass: this file is also loaded by path, outside sys.modules.
Target = namedtuple("Target", "voice_pid claude_pid label")


def conch_holder_pids(home=None) -> list:
    """Voice-server pids from the conch events log, newest first, each once."""
    h = Path(home) if home is not None else _pill_state().upstream_home()
    files = sorted(glob.glob(str(h / "logs" / "events" / "voicemode_events_*.jsonl")), reverse=True)
    out = []
    for path in files[:2]:
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - EVENTS_TAIL_BYTES))
                lines = f.read().decode("utf-8", "replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if '"CONCH_' not in line:
                continue
            try:
                pid = json.loads(line)["data"]["pid"]
            except (ValueError, KeyError, TypeError):
                continue
            if isinstance(pid, int) and pid not in out:
                out.append(pid)
    return out


def _registry_label(claude_pid, cdir=None) -> str:
    reg = read_registry(claude_pid, cdir) or {}
    name = reg.get("name")
    if isinstance(name, str) and name:
        return name
    cwd = reg.get("cwd")
    return os.path.basename(str(cwd).rstrip("/\\")) if cwd else f"claude {claude_pid}"


def last_voice_session(home=None, base=None, cdir=None, parent: Callable = parent_pid,
                       sessions=None) -> Optional[Target]:
    st = _pill_state()
    if sessions is None:
        try:
            sessions = st.live_sessions(base)
        except Exception:  # noqa: BLE001 - the events log may still find one
            sessions = []
    labels = {s.get("pid"): s.get("label") for s in sessions}
    by_use = sorted(sessions, key=lambda s: s.get("last_voice_at") or s.get("started_at") or 0,
                    reverse=True)
    seen = set()
    for vpid in conch_holder_pids(home) + [s.get("pid") for s in by_use]:
        if not isinstance(vpid, int) or vpid in seen:
            continue
        seen.add(vpid)
        if not st.pid_exists(vpid):
            continue
        cpid = claude_pid_for(vpid, cdir, parent)
        if cpid:
            return Target(vpid, cpid, labels.get(vpid) or _registry_label(cpid, cdir))
    return None


# -- checks ------------------------------------------------------------------

class Refused(Exception):
    """A reason not to send; the message is shown to the user and logged."""


def _check_proc_start(pid, reg) -> None:
    """Linux only: a registry entry for a reused pid is stale."""
    if not sys.platform.startswith("linux"):
        return
    current = _pill_state().process_start(pid)
    if current is None or str(reg.get("procStart")) != str(current):
        raise Refused("stale registry entry (the process start differs)")


def _private_file(path: Path, what: str) -> None:
    if os.name != "posix":
        return
    info = os.stat(path)
    if info.st_uid != os.getuid():
        raise Refused(f"{what} owned by another user")
    if info.st_mode & 0o077:
        raise Refused(f"{what} is readable by others")


def _peer_token(pid, reg, cdir=None) -> str:
    keys = sorted(_sessions_dir(cdir).glob(f"{int(pid)}.*.key"))
    if not keys:
        raise Refused("no key file")
    for path in keys:
        _private_file(path, "key file")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        token = data.get("peerToken")
        if str(data.get("procStart")) == str(reg.get("procStart")) and isinstance(token, str) and token:
            return token
    raise Refused("no key file for this process start")


def _check_socket(path: str) -> None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise Refused("no messaging socket") from None
    if not stat.S_ISSOCK(info.st_mode):
        raise Refused("messaging socket path is not a socket")
    if info.st_uid != os.getuid():
        raise Refused("messaging socket owned by another user")
    if info.st_mode & 0o077:
        raise Refused("messaging socket is open to other users")
    parent = os.stat(os.path.dirname(os.path.abspath(path)))
    if parent.st_uid != os.getuid() or parent.st_mode & 0o022:
        raise Refused("messaging socket directory is writable by others")


# -- sending -----------------------------------------------------------------

def _payload(token: str, text: str) -> bytes:
    lines = [{"type": "auth", "token": token},
             {"type": "user", "message": {"role": "user", "content": text}}]
    return "".join(json.dumps(x) + "\n" for x in lines).encode("utf-8")


def _send_unix(path: str, data: bytes) -> None:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(SEND_TIMEOUT_S)
        s.connect(path)
        s.sendall(data)
    finally:
        s.close()


def _connect_pipe(name: str, timeout: float):
    """UNVERIFIED: control_transport's owner-checked pipe client, on Claude Code's pipe."""
    return _control_transport()._win_connect(name, timeout)


def _send_pipe(name: str, data: bytes) -> None:
    try:
        conn = _connect_pipe(name, SEND_TIMEOUT_S)
    except PermissionError as e:
        raise Refused("refused: " + _one_line(e)) from None
    except FileNotFoundError:
        raise Refused("no messaging pipe") from None
    with conn:
        conn.sendall(data)


def _one_line(error: BaseException) -> str:
    text = error.strerror if isinstance(error, OSError) and error.strerror else str(error)
    return " ".join(str(text or type(error).__name__).split())


# -- wake --------------------------------------------------------------------

def _attempt(target_claude_pid, reason, cdir, home, base):
    """(ok, detail, claude_pid, label). Raises Refused for a reason not to send."""
    global _last_sent
    if not enabled(home):
        raise Refused(f"off ({SETTING}=off)")
    text = MESSAGES.get(reason)
    if text is None:
        raise Refused(f"unknown reason {reason!r}")
    label = None
    pid = target_claude_pid
    if pid is None:
        target = last_voice_session(home=home, base=base, cdir=cdir)
        if target is None:
            raise Refused("no voice session to wake")
        pid, label = target.claude_pid, target.label
    reg = read_registry(pid, cdir)
    if reg is None or reg.get("pid") != pid:
        raise Refused("no registry entry for that session")
    label = label or _registry_label(pid, cdir)
    if reg.get("peerProtocol") != PEER_PROTOCOL:
        raise Refused(f"peer protocol {reg.get('peerProtocol')!r}, need {PEER_PROTOCOL}")
    status = reg.get("status")
    if status != "idle":
        raise Refused(STATUS_REASONS.get(status, f"unknown status {status!r}"))
    _check_proc_start(pid, reg)
    path = reg.get("messagingSocketPath")
    if not isinstance(path, str) or not path:
        raise Refused("no messaging socket")
    pipe = is_pipe(path)
    if not pipe:
        _check_socket(path)
    token = _peer_token(pid, reg, cdir)
    data = _payload(token, text)
    with _lock:
        if _last_sent is not None and _now() - _last_sent < RATE_LIMIT_S:
            raise Refused(f"rate limited (one wake per {RATE_LIMIT_S:g} s)")
        try:
            if pipe:
                _send_pipe(path, data)
            else:
                _send_unix(path, data)
        except Refused:
            raise
        except (OSError, socket.timeout) as e:
            raise Refused("send failed: " + _one_line(e)) from None
        _last_sent = _now()
    return pid, label


def wake(target_claude_pid=None, reason="ptt", *, cdir=None, home=None, base=None):
    """Ask an idle Claude Code session to start a voice turn. Returns (ok, detail).

    ``target_claude_pid`` None means the last voice session. ``reason`` picks the
    fixed message: "ptt" or "play". Never raises; never reveals the token.
    """
    pid, label = target_claude_pid, None
    try:
        pid, label = _attempt(target_claude_pid, reason, cdir, home, base)
        ok, detail = True, "sent"
    except Refused as e:
        ok, detail = False, str(e)
    except Exception as e:  # noqa: BLE001 - a wake must never break a key listener or the pill
        ok, detail = False, "failed: " + type(e).__name__
    _log(reason, pid, label, ok, detail)
    return ok, detail


def _log(reason, pid, label, ok, detail) -> None:
    line = (f"{datetime.now().isoformat(timespec='seconds')} reason={reason} "
            f"claude_pid={pid} label={label!r} ok={ok} detail={detail}")
    logger.info("session wake: %s", line)
    try:
        d = _pill_state().upstream_home() / "logs"
        d.mkdir(parents=True, exist_ok=True)
        path = d / "session-wake.log"
        try:
            if path.stat().st_size > LOG_MAX_BYTES:
                os.replace(path, path.with_suffix(".log.1"))
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001 - logging must not fail a wake
        logger.debug("session wake log failed", exc_info=True)
