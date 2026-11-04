"""Shared state for the voice pill (stdlib only, safe across processes).

Used by the patched voice-mode server, the pill trigger and the pill backend,
which all run on one machine.  Every file lives under ``base`` (default
``~/.voicemode/pill``, mode 0700).  Writes are atomic (temp file + os.replace);
a missing, corrupt or half-written file reads as empty and never raises.

Pause is owned by a pid: if the owner dies (or its pid is reused), the pause
is ignored, so a crashed pill can never leave every voice session hanging.
"""
from __future__ import annotations

import json
import os
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
    b.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
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


def touch_session(pid: int, voice: str | None = None, base=None) -> None:
    path = _session_path(pid, base)
    s = _read_json(path)
    if not s:
        return
    s["last_voice_at"] = time.time()
    if voice is not None:
        s["voice"] = voice
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
            continue
        out.append({k: s.get(k) for k in
                    ("pid", "label", "voice", "started_at", "last_voice_at")})
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
    p = _read_json(_base(base) / "paused.json")
    owner = p.get("owner")
    return isinstance(owner, int) and _alive_same(owner, p.get("start"))


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
