"""Shared state for the voice pill (stdlib only, safe across processes).

Used by the patched voice-mode server, the pill trigger and the pill backend,
which all run on one machine.  Every file lives under ``base`` (default
``~/.voicemode/pill``, mode 0700).  Writes are atomic (temp file + os.replace);
a missing, corrupt or half-written file reads as empty and never raises.

Pause is owned by a pid: if the owner dies (or its pid is reused), the pause
is ignored, so a crashed pill can never leave every voice session hanging.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_MAX_INTERRUPTION_BYTES = 64 * 1024
_KEEP_INTERRUPTIONS = 1000


def default_base() -> Path:
    return Path(os.path.expanduser("~")) / ".voicemode" / "pill"


def _base(base) -> Path:
    b = Path(base) if base is not None else default_base()
    # Best effort: is_paused() runs in the recording loop and must not raise.
    # A real write fails later, on its own, where the caller can handle it.
    try:
        b.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(b, 0o700)
    except OSError:
        pass
    return b


def _sub(base, name: str) -> Path:
    d = _base(base) / name
    d.mkdir(mode=0o700, exist_ok=True)
    return d


# -- process identity -------------------------------------------------------

def process_start(pid: int) -> str | None:
    """Opaque start-time token for pid, or None if the process is gone."""
    try:
        if sys.platform.startswith("linux"):
            with open(f"/proc/{int(pid)}/stat", "rb") as f:
                data = f.read().decode("ascii", "replace")
            # comm may contain spaces/parens: fields resume after the last ')'.
            # starttime is field 22 overall = index 19 after state (field 3).
            return data[data.rindex(")") + 2:].split()[19]
        out = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(int(pid))],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        return out or None
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def _alive_same(pid, start) -> bool:
    cur = process_start(pid)
    return cur is not None and cur == start


# -- file helpers -----------------------------------------------------------

def _write_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write_json(path: Path, obj) -> None:
    _write_atomic(path, json.dumps(obj))


def _read_json(path: Path, want=dict):
    try:
        obj = json.loads(path.read_text())
    except (OSError, ValueError):
        return want()
    return obj if isinstance(obj, want) else want()


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


# -- sessions ---------------------------------------------------------------

def _session_path(pid, base) -> Path:
    return _sub(base, "sessions") / f"{int(pid)}.json"


def register_session(pid: int, label: str, voice: str | None, base=None) -> None:
    _write_json(_session_path(pid, base), {
        "pid": int(pid), "label": label, "voice": voice,
        "start": process_start(pid), "started_at": time.time(),
        "last_voice_at": None,
    })


def touch_session(pid: int, voice: str | None = None, base=None,
                  label: str | None = None) -> None:
    path = _session_path(pid, base)
    s = _read_json(path)
    if not s:
        return
    s["last_voice_at"] = time.time()
    if voice is not None:
        s["voice"] = voice
    if label is not None:
        s["label"] = label
    _write_json(path, s)


def mark_stopped(pid: int, stopped: bool, base=None) -> None:
    """A stopped session stays listed: the pill closes only when its server exits."""
    path = _session_path(pid, base)
    s = _read_json(path)
    if s:
        s["stopped"] = stopped
        _write_json(path, s)


def unregister_session(pid: int, base=None) -> None:
    _unlink(_session_path(pid, base))


def live_sessions(base=None) -> list[dict]:
    out = []
    for path in sorted(_sub(base, "sessions").glob("*.json")):
        s = _read_json(path)
        pid = s.get("pid")
        if not isinstance(pid, int) or not _alive_same(pid, s.get("start")):
            _unlink(path)
            if isinstance(pid, int):  # a reused pid must not inherit these
                set_override(pid, None, None, base)
                take_stop(pid, base)
            continue
        out.append({k: s.get(k) for k in
                    ("pid", "label", "voice", "started_at", "last_voice_at", "stopped")})
    return out


# -- stop flag --------------------------------------------------------------

def request_stop(pid: int, base=None) -> None:
    (_sub(base, "stop") / str(int(pid))).touch()


def take_stop(pid: int, base=None) -> bool:
    try:
        (_sub(base, "stop") / str(int(pid))).unlink()
        return True
    except OSError:
        return False


# -- pause ------------------------------------------------------------------

def set_paused(on: bool, owner_pid: int, base=None) -> None:
    path = _base(base) / "paused.json"
    if on:
        _write_json(path, {"owner": int(owner_pid),
                           "start": process_start(owner_pid)})
    else:
        _unlink(path)


def is_paused(base=None) -> bool:
    path = _base(base) / "paused.json"
    p = _read_json(path)
    if not p:
        return False
    owner = p.get("owner")
    if isinstance(owner, int) and _alive_same(owner, p.get("start")):
        return True
    # Remove it: on non-Linux every owner check spawns ps, and the recording loop
    # calls this each chunk, so a crashed pill must cost one spawn, not one per chunk.
    _unlink(path)
    return False


async def wait_while_paused(until=None, poll: float = 0.25, base=None) -> None:
    """Sleep until resume, or until the optional until() says to stop waiting."""
    while is_paused(base) and not (until is not None and until()):
        await asyncio.sleep(poll)


# -- per-session voice/language override ------------------------------------

def set_override(pid: int, voice: str | None, language: str | None, base=None) -> None:
    path = _base(base) / f"override-{int(pid)}.json"
    data = {k: v for k, v in (("voice", voice), ("language", language)) if v}
    if data:
        _write_json(path, data)
    else:
        _unlink(path)


def get_override(pid: int, base=None) -> dict:
    return _read_json(_base(base) / f"override-{int(pid)}.json")


def effective_voice(pid: int, voice: str | None, base=None) -> str | None:
    return get_override(pid, base).get("voice") or voice


def language_note(pid: int, base=None) -> str:
    """Text for the agent, not spoken: a voice speaks only its own language."""
    language = get_override(pid, base).get("language")
    if not language:
        return ""
    return f"\n\nThe user selected {language}. Write your next messages in {language}."


# -- beeps switch, machine-wide ---------------------------------------------

def set_beeps(on: bool, base=None) -> None:
    _write_json(_base(base) / "beeps.json", {"on": bool(on)})


def beeps_choice(base=None) -> bool | None:
    """The pill's choice, or None if none was made and the env setting still decides."""
    on = _read_json(_base(base) / "beeps.json").get("on")
    return on if isinstance(on, bool) else None


def effective_beeps(enabled: bool | None, base=None) -> bool | None:
    """Off silences everything. On beats the env, not a caller's own False: a voice cut
    skips its chime on purpose."""
    choice = beeps_choice(base)
    if choice is None or enabled is False:
        return enabled
    return choice


def result_note(pid: int, base=None, already: str = "") -> str:
    """Everything the agent should read after a turn. The stop note covers a turn cut by the
    socket, which only says playback stopped; our rule to keep talking would otherwise win."""
    note = language_note(pid, base)
    if _read_json(_session_path(pid, base)).get("stopped") and STOPPED_MESSAGE not in already:
        note += "\n\n" + STOPPED_MESSAGE
    return note


# -- entering a voice turn --------------------------------------------------

STOPPED_MESSAGE = ("Voice was stopped from the pill. "
                   "Continue in text unless the user asks for voice again.")


async def enter(pid: int, label: str, voice: str | None, base=None) -> str | None:
    """Runs before the conch is taken: a waiting session must not hold the floor.

    Returns a message for the agent if this session was stopped, else None.
    """
    voice = effective_voice(pid, voice, base)
    current = _read_json(_session_path(pid, base))
    if not current or current.get("start") != process_start(pid):
        register_session(pid, label, voice, base)
    touch_session(pid, voice, base, label=label)
    await wait_while_paused(base=base)
    if take_stop(pid, base):
        mark_stopped(pid, True, base)
        return STOPPED_MESSAGE
    mark_stopped(pid, False, base)
    return None


# -- interruptions (append-only log of timestamps) --------------------------

def record_interruption(t: float, base=None) -> None:
    path = _base(base) / "interruptions.log"
    # Leading newline so a torn previous line cannot swallow this one.
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, f"\n{float(t)!r}\n".encode())
    finally:
        os.close(fd)
    try:
        if path.stat().st_size > _MAX_INTERRUPTION_BYTES:
            keep = _interruption_times(path)[-_KEEP_INTERRUPTIONS:]
            _write_atomic(path, "".join(f"\n{x!r}\n" for x in keep))
    except OSError:
        pass


def _interruption_times(path: Path) -> list[float]:
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            out.append(float(ln))
        except ValueError:
            pass
    return out


def interruptions_since(t: float, base=None) -> int:
    return sum(1 for x in _interruption_times(_base(base) / "interruptions.log")
               if x >= t)


# -- push-to-talk recency ---------------------------------------------------

def touch_ptt(base=None) -> None:
    path = _base(base) / "ptt"
    path.touch()
    os.utime(path, None)


def ptt_recent(window_s: float = 120.0, base=None) -> bool:
    try:
        return time.time() - (_base(base) / "ptt").stat().st_mtime <= window_s
    except OSError:
        return False


# -- UI state ---------------------------------------------------------------

def load_ui(base=None) -> dict:
    return _read_json(_base(base) / "ui.json")


def save_ui(ui: dict, base=None) -> None:
    _write_json(_base(base) / "ui.json", ui)


# -- control socket (shared by the barge trigger and the pill backend) -------

def control_socket_path() -> Path:
    # Upstream's variable is VOICEMODE_CONTROL_SOCKET (config.py).
    return Path(os.path.expanduser(os.getenv("VOICEMODE_CONTROL_SOCKET") or "~/.voicemode/control.sock"))


def _exchange(path: Path, payload: dict, want_reply: bool):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        s.connect(str(path))
        s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        if not want_reply:
            return None
        buf = b""
        while b"\n" not in buf and len(buf) < 65536:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf.split(b"\n", 1)[0])


def _status(path: Path) -> dict:
    """The server's status, or {}. The socket exists only during an active
    converse, so an absent one is the normal idle state."""
    if not path.exists():
        return {}
    try:
        reply = _exchange(path, {"command": "status"}, want_reply=True)
    except (OSError, ValueError):
        return {}
    return reply if isinstance(reply, dict) else {}


def send_control(path: Path, command: str) -> bool:
    """Fire-and-forget control command. True if it was delivered to a live socket."""
    if not path.exists():
        return False
    try:
        _exchange(path, {"command": command}, want_reply=False)
    except OSError:
        return False
    return True


def default_switch() -> Path:
    """Beside this file's folder when run from the repo's patches/; else next to a repo venv."""
    beside = Path(__file__).resolve().parent.parent / "voicemode-switch"
    return beside if beside.exists() else Path(sys.prefix).parent / "voicemode-switch"


def barge_control(action: str, switch=None) -> str:
    """Run ``voicemode-switch barge <action>``. The action is checked against a
    fixed list, so model output never reaches the command line."""
    action = (action or "").strip().lower()
    if action not in ("on", "off", "status"):
        return "action must be on, off or status"
    switch = Path(switch) if switch is not None else default_switch()
    if not switch.exists():
        return f"voicemode-switch not found at {switch}"
    try:
        # `on` may download the Silero model.
        done = subprocess.run([str(switch), "barge", action], capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"voice barge {action} failed: {e}"
    output = re.sub(r"\x1b\[[0-9;]*m", "", (done.stdout + done.stderr)).strip()
    if done.returncode != 0:
        return f"voice barge {action} failed: {output}"
    return output
