"""Shared state for the voice pill (stdlib only, safe across processes).

Used by the patched voice-mode server, the pill trigger and the pill backend,
which all run on one machine.  Every file lives under ``base`` (default
``<base dir>/pill``, mode 0700), where the base dir is upstream's
``VOICEMODE_BASE_DIR``, else ``~/.voicemode`` -- resolved on every call, never
cached, because FELIX gives each runtime its own.  Writes are atomic (temp file +
os.replace); a missing, corrupt or half-written file reads as empty and never
raises.  On Windows a file another process has open cannot be replaced or
deleted for a moment, so writes and deletes retry, and a file that could not be
read is left alone rather than treated as gone.  Only the pill backend
(``enable_backend_mode``) retries reads, and writes for about half a second;
elsewhere -- the voice server's recording and event loops -- a read happens
once and a write is tried at most twice, with one 25 ms sleep.

Upstream's own files stay where upstream keeps them, in the home folder's
.voicemode whatever the base dir (conch.py:133 the conch, config.py:33
voicemode.env): ``upstream_home()``.  Settings (``setting()``) come from the
process environment first, then that voicemode.env, read as UTF-8 with CRLF
tolerated -- except a setting the pill itself changes, read from where its
change lands (``file_first_setting()``).

Pause is owned by a pid: if the owner dies (or its pid is reused), the pause
is ignored, so a crashed pill can never leave every voice session hanging.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

_MAX_INTERRUPTION_BYTES = 64 * 1024
_KEEP_INTERRUPTIONS = 1000

# Windows sharing violations: a reader's open handle blocks os.replace/unlink.
_RETRIES = 20          # the pill backend: about half a second
_SERVER_ATTEMPTS = 2   # anywhere else (the voice server): one 25 ms sleep at most
_RETRY_S = 0.025
_BACKEND = False       # set by the pill backend's own process only

logger = logging.getLogger("voicemode.voice_pill_state")
_WARNED = set()


def enable_backend_mode(on: bool = True) -> None:
    """For the pill backend's own process, never the voice server's: reads retry, and
    writes retry longer. A sleep there costs nothing; in the server it stalls a turn."""
    global _BACKEND
    _BACKEND = bool(on)


def _warn_once(message: str) -> None:
    if message not in _WARNED:
        _WARNED.add(message)
        logger.warning(message)


def _on_windows() -> bool:
    return sys.platform == "win32"


def _retry_sleep(seconds: float) -> None:
    time.sleep(seconds)


def _retrying(fn, *args):
    """Call fn; on Windows retry a PermissionError (file busy): about half a second in
    the pill backend, else one retry after 25 ms."""
    if not _on_windows():
        return fn(*args)
    attempts = _RETRIES if _BACKEND else _SERVER_ATTEMPTS
    for attempt in range(attempts):
        try:
            return fn(*args)
        except PermissionError:
            if attempt == attempts - 1:
                raise
            _retry_sleep(_RETRY_S)


# -- where things live ------------------------------------------------------

def upstream_home() -> Path:
    """Where upstream keeps the conch and voicemode.env, whatever the base dir."""
    return Path.home() / ".voicemode"


def voicemode_base() -> Path:
    """Upstream's base dir (config.py:545: VOICEMODE_BASE_DIR, variables then ~ expanded).

    From the environment, else from upstream_home()'s voicemode.env (the server loads
    that file into its environment first, config.py:521-527), else ~/.voicemode. Read
    on every call; an empty setting means unset. Limit: upstream also loads project
    .voicemode.env files found walking up from its working folder; the pill backend
    has no session working folder, so those are not followed.
    """
    raw = os.environ.get("VOICEMODE_BASE_DIR") or _env_view().get("VOICEMODE_BASE_DIR")
    if raw:
        return Path(os.path.expanduser(os.path.expandvars(raw)))
    return upstream_home()


def default_base() -> Path:
    return voicemode_base() / "pill"


def env_file(home=None) -> Path:
    """voicemode.env, or upstream's old name .voicemode.env when only that exists."""
    h = Path(home) if home is not None else upstream_home()
    path, old = h / "voicemode.env", h / ".voicemode.env"
    return old if not path.exists() and old.exists() else path


_ENV_CACHE = {}  # path -> (stamp, parsed): re-read only when it changes
# mtime moves on a coarse clock tick, so a file this fresh may change again without
# its stamp changing: read it, but do not cache it.
_ENV_FRESH_NS = 1_000_000_000


def read_env_file(home=None) -> dict:
    """KEY=value pairs from voicemode.env (see _parse_env), a copy the caller may change."""
    return dict(_env_view(home))


def _env_view(home=None) -> dict:
    """The cached parse of voicemode.env, else upstream's old name; never modify it.
    One stat per call when unchanged: the base dir is resolved on every is_paused()."""
    h = Path(home) if home is not None else upstream_home()
    for path in (h / "voicemode.env", h / ".voicemode.env"):
        try:
            info = os.stat(path)
        except OSError:
            continue
        return _cached_parse(path, info)
    return {}


def _read_env_path(path) -> dict:
    try:
        info = os.stat(path)
    except OSError:
        return {}
    return _cached_parse(path, info)


def _cached_parse(path, info) -> dict:
    # The inode catches an atomic replace; ctime any metadata change mtime missed.
    stamp = (info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size)
    key = str(path)
    hit = _ENV_CACHE.get(key)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    try:
        text = _read_policy(path).decode("utf-8-sig", errors="replace")
    except OSError:
        return {}
    parsed = _parse_env(text)
    if time.time_ns() - info.st_mtime_ns > _ENV_FRESH_NS:
        _ENV_CACHE[key] = (stamp, parsed)
    else:
        _ENV_CACHE.pop(key, None)
    return parsed


def _parse_env(text: str) -> dict:
    """UTF-8 (BOM allowed), CRLF tolerated, comments and ``export`` skipped, surrounding
    quotes dropped, the last assignment wins."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def setting(name: str, default=None, home=None):
    """The process environment first (FELIX passes settings that way), then voicemode.env."""
    if name in os.environ:
        return os.environ[name]
    return _env_view(home).get(name, default)


FALLBACK_VOICE = "am_puck"


def default_voice(home=None) -> str:
    """The voice a session gets with no choice: first of VOICEMODE_VOICES, else
    VOICEMODE_TTS_VOICE, else am_puck."""
    for name in ("VOICEMODE_VOICES", "VOICEMODE_TTS_VOICE"):
        for item in (setting(name, "", home) or "").split(","):
            if item.strip():
                return item.strip()
    return FALLBACK_VOICE


def file_first_setting(name: str, default=None, home=None):
    """For a setting the pill itself changes by writing voicemode.env: the file wins when
    it has the key, and the environment is only the default."""
    file = _env_view(home)
    if name in file:
        return file[name]
    return os.environ.get(name, default)


def pill_enabled(home=None) -> bool:
    return (setting("VOICEMODE_PILL", "", home) or "").strip() == "true"


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
    if _on_windows():
        # OpenProcess + GetProcessTimes; never ps, /proc or os.kill(pid, 0).
        try:
            return _control_transport().process_start_time(int(pid))
        except (OSError, ValueError, AttributeError):
            return None
    try:
        if sys.platform.startswith("linux"):
            with open(f"/proc/{int(pid)}/stat", "rb") as f:
                data = f.read().decode("ascii", "replace")
            # comm may contain spaces/parens: fields resume after the last ')'.
            # starttime is field 22 overall = index 19 after state (field 3).
            return data[data.rindex(")") + 2:].split()[19]
        # lstart prints local time in the caller's locale: pin both, or the same process gets a
        # different token from a process with another TZ (or after the zone changes).
        out = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(int(pid))],
            capture_output=True, text=True, timeout=5,
            env={**os.environ, "TZ": "UTC", "LC_ALL": "C"},
        ).stdout.strip()
        return out or None
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def pid_exists(pid) -> bool:
    """Is there a process with this pid? Never os.kill(pid, 0) on Windows: it terminates."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if _on_windows():
        return windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _not_same_reason(pid, start):
    """None if pid is the process recorded; else why not. A record without a start time never
    matches, so a reused pid cannot inherit it. If only the current start is unknown (ps
    failed), an alive pid is kept: losing a live session is worse."""
    cur = process_start(pid)  # first, as before: same spawns on a Mac as before
    if start is None:
        return "no start token recorded"
    if cur is not None:
        return None if cur == start else f"start token differs: stored {start!r}, now {cur!r}"
    return None if pid_exists(pid) else "process gone"


def _alive_same(pid, start) -> bool:
    return _not_same_reason(pid, start) is None


prune_log = None  # the backend sets a function here; it reports each session file deleted


def windows_pid_alive(pid: int) -> bool:
    """Liveness on Windows: a live process we may query, or one we may not (it exists)."""
    try:
        return bool(_control_transport().process_alive(int(pid)))
    except (OSError, ValueError, AttributeError):
        return False


# -- file helpers -----------------------------------------------------------

def _write_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        _retrying(os.replace, tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write_json(path: Path, obj) -> None:
    _write_atomic(path, json.dumps(obj))


def _read_bytes(path) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _read_policy(path) -> bytes:
    """One read, or retried reads in the pill backend only (see enable_backend_mode)."""
    return _retrying(_read_bytes, path) if _BACKEND else _read_bytes(path)


def _read_json_state(path: Path, want=dict):
    """(object, status): status is ok, missing, invalid (corrupt or the wrong shape) or
    unreadable (it exists but could not be read: busy on Windows, or no access)."""
    try:
        raw = _read_policy(path)
    except FileNotFoundError:
        return want(), "missing"
    except OSError:
        return want(), "unreadable"
    try:
        obj = json.loads(raw.decode("utf-8"))
    except ValueError:
        return want(), "invalid"
    return (obj, "ok") if isinstance(obj, want) else (want(), "invalid")


def _read_json(path: Path, want=dict):
    return _read_json_state(path, want)[0]


def _unlink(path: Path) -> None:
    try:
        _retrying(os.unlink, path)
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
        s, status = _read_json_state(path)
        if status == "unreadable":  # busy for a moment, or no access: never delete it
            continue
        pid = s.get("pid")
        why = "pid is not an integer" if not isinstance(pid, int) else _not_same_reason(pid, s.get("start"))
        if why:
            if prune_log:
                try:
                    prune_log(f"pruned session {path.name} (pid {pid!r}): {why}")
                except Exception:
                    pass
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
        _retrying(os.unlink, _sub(base, "stop") / str(int(pid)))
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
    p, status = _read_json_state(path)
    if status == "unreadable":
        # Not known to be paused, and not known to be stale: keep the file.
        return False
    if not p:
        return False
    owner = p.get("owner")
    if isinstance(owner, int) and _alive_same(owner, p.get("start")):
        return True
    # Remove it: on non-Linux every owner check spawns ps, and the recording loop
    # calls this each chunk, so a crashed pill must cost one spawn, not one per chunk.
    # One attempt, no retry sleeps: this is the recording loop.
    try:
        os.unlink(path)
    except OSError:
        pass
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


BARGE_ON_NOTE = ("Interrupt by voice is on: the user can talk over you to stop you. Tell them "
                 "once, in a few words, that they can switch it off or on at any time by asking "
                 "(the voice_barge tool) or in the voice pill.")
BARGE_OFF_NOTE = ("Interrupt by voice is off. Tell the user once, in a few words, that they can "
                  "switch it on or off at any time by asking (the voice_barge tool) or in the "
                  "voice pill.")


def capture_backend() -> str | None:
    """How voice barge-in can hear the mic: "parec" (PulseAudio/PipeWire, Linux and WSL),
    else "sounddevice" (PortAudio, Windows and macOS), else None. Found, not imported:
    loading PortAudio here would cost every caller."""
    if shutil.which("parec") is not None:
        return "parec"
    if importlib.util.find_spec("sounddevice") is not None:
        return "sounddevice"
    return None


def barge_mode(home=None) -> str | None:
    """"on", "off" or "auto" for interrupt by voice, or None where it cannot run (no mic
    reader). auto (Robert, 2026-11-10): the trigger runs, and listens only while the
    output route is headphones (voice_barge.output_route), decided at each stretch.

    voicemode.env wins when it sets VOICEMODE_VOICE_BARGE: the switch writes it while the
    server runs, and the server's environment is from start-up. Without the key, the
    environment decides: FELIX passes the setting there and writes no file."""
    if capture_backend() is None:
        return None
    settings = read_env_file(home)
    value = settings.get("VOICEMODE_VOICE_BARGE")
    if value is None:
        value = os.environ.get("VOICEMODE_VOICE_BARGE", "")
    value = value.strip().strip('"').lower()
    return {"true": "on", "auto": "auto"}.get(value, "off")


def barge_state(home=None) -> bool | None:
    """Whether the trigger should run (on or auto), or None where it cannot run."""
    mode = barge_mode(home)
    return None if mode is None else mode in ("on", "auto")


def _barge_note_once(pid: int, base) -> str:
    """Said once per session, at its first turn (Robert, 2026-11-05)."""
    path = _session_path(pid, base)
    s = _read_json(path)
    if not s or s.get("barge_told"):
        return ""
    state = barge_state()
    if state is None:
        return ""
    s["barge_told"] = True
    _write_json(path, s)
    return "\n\n" + (BARGE_ON_NOTE if state else BARGE_OFF_NOTE)


def result_note(pid: int, base=None, already: str = "") -> str:
    """Everything the agent should read after a turn. The stop note covers a turn cut by the
    socket, which only says playback stopped; our rule to keep talking would otherwise win."""
    note = language_note(pid, base)
    if _read_json(_session_path(pid, base)).get("stopped") and STOPPED_MESSAGE not in already:
        note += "\n\n" + STOPPED_MESSAGE
    return note + _barge_note_once(pid, base)


# -- entering a voice turn --------------------------------------------------

STOPPED_MESSAGE = ("Voice was stopped from the pill. "
                   "Continue in text unless the user asks for voice again.")


# -- closed marker: the user's close sticks until voice restarts from scratch --
# (Robert, 2026-11-10) The pill opens with the first voice session and closes with the
# last; closed by the user, it stays closed until no voice session is left and a new one
# starts. "From scratch" = no other live registered session when one enters its first turn.

def _closed_path(base) -> Path:
    return _base(base) / "closed"


def pill_closed(base=None) -> bool:
    return _closed_path(base).exists()


def set_pill_closed(closed: bool, base=None) -> None:
    if closed:
        _write_atomic(_closed_path(base), str(time.time()))
    else:
        _unlink(_closed_path(base))


# Set by the server (pill_autostart): called with the base after a session has entered a
# turn, to bring the window up when it should be up. Never allowed to cost the turn.
autostart_hook = None


def _default_autostart():
    """pill_autostart.ensure inside the installed voice_mode package; None anywhere else
    (this module loaded on its own, as in tests, must never start a real window)."""
    if __package__ != "voice_mode":
        return None
    try:
        from voice_mode import pill_autostart
    except ImportError:
        return None
    return pill_autostart.ensure


async def enter(pid: int, label: str, voice: str | None, base=None) -> str | None:
    """Runs before the conch is taken: a waiting session must not hold the floor.

    Returns a message for the agent if this session was stopped, else None.
    """
    voice = effective_voice(pid, voice, base)
    current = _read_json(_session_path(pid, base))
    if not current or current.get("start") != process_start(pid):
        # Before registering: is anyone else still here? If not, voice restarts from scratch.
        if not any(s["pid"] != pid for s in live_sessions(base)):
            set_pill_closed(False, base)
        register_session(pid, label, voice, base)
    touch_session(pid, voice, base, label=label)
    hook = autostart_hook if autostart_hook is not None else _default_autostart()
    if hook is not None:
        try:
            hook(base)
        except Exception as e:
            _warn_once(f"voice pill autostart failed: {e}")
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
        lines = _read_policy(path).decode("utf-8", errors="replace").splitlines()
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
    # Upstream's variable is VOICEMODE_CONTROL_SOCKET, default BASE_DIR/control.sock (config.py).
    explicit = os.getenv("VOICEMODE_CONTROL_SOCKET")
    if explicit:
        return Path(os.path.expanduser(os.path.expandvars(explicit)))
    return voicemode_base() / "control.sock"


def _use_pipe() -> bool:
    """No AF_UNIX (Windows): the control channel is a named pipe, with no file."""
    return not hasattr(socket, "AF_UNIX")


# Where the transport may come from: the installed voice_mode package, or the
# file beside this one (patches/ in the repo; voice_mode/ itself once installed).
# Never a bare `control_transport` found elsewhere on sys.path.
_TRANSPORT_FILE = Path(__file__).with_name("control_transport.py")
_TRANSPORT = None


def _control_transport():
    """The named-pipe transport, for Pythons without AF_UNIX (Windows)."""
    global _TRANSPORT
    installed = sys.modules.get("voice_mode.control_transport")
    if installed is not None:
        return installed
    if _TRANSPORT is None:
        if not _TRANSPORT_FILE.is_file():  # an install that never copied the module
            raise OSError(f"no control transport at {_TRANSPORT_FILE}")
        spec = importlib.util.spec_from_file_location("_vml_control_transport", _TRANSPORT_FILE)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _TRANSPORT = mod
    return _TRANSPORT


def _connect(path: Path, timeout: float):
    if _use_pipe():
        # Owner-checked: refuses a pipe that is not the current user's.
        return _control_transport().connect(path, timeout)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(timeout)
        s.connect(str(path))
    except BaseException:
        s.close()
        raise
    return s


def _exchange(path: Path, payload: dict, want_reply: bool):
    with _connect(path, 0.3) as s:
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
    if not _use_pipe() and not path.exists():
        return {}
    try:
        reply = _exchange(path, {"command": "status"}, want_reply=True)
    except (OSError, ValueError):
        return {}
    return reply if isinstance(reply, dict) else {}


def send_control(path: Path, command: str) -> bool:
    """Fire-and-forget control command. True if it was delivered to a live socket."""
    if not _use_pipe() and not path.exists():
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


BARGE_COMMAND_SETTING = "VOICEMODE_BARGE_CONTROL_COMMAND"


_PROJECT_DIR = ".voicemode"  # upstream's per-project folder name, below any parent


def _env_files_upstream_loads() -> list:
    """Every file upstream's load_voicemode_env may copy into os.environ: the global
    voicemode.env (either name) and project files from the working folder up. Wider than
    upstream's own walk (it stops at the first find), and upstream's own list is added
    when voice_mode.config is loaded here."""
    home = upstream_home()
    files = [home / "voicemode.env", home / ".voicemode.env"]
    try:
        here = Path.cwd()
    except OSError:
        here = None
    if here is not None:
        for d in (here, *here.parents):
            files += [d / ".voicemode.env", d / _PROJECT_DIR / "voicemode.env"]
    config = sys.modules.get("voice_mode.config")
    find = getattr(config, "find_voicemode_env_files", None)
    if callable(find):
        try:
            files += [Path(f) for f in find()]
        except Exception:  # noqa: BLE001 - our own list still applies
            pass
    return files


def setting_came_from_a_file(name: str) -> bool:
    """True when any voicemode.env file upstream loads holds ``name``. A model can write
    those files (upstream's update_config takes any key), and the server copies them into
    os.environ (load_voicemode_env, on start and on config_reload), so a setting that
    names a program to run is trusted from the real environment only."""
    return any(name in _read_env_path(f) for f in _env_files_upstream_loads())


def trusted_env(name: str) -> str:
    """``name`` from the environment, or "" (one warning) when a voicemode.env holds it."""
    raw = (os.environ.get(name) or "").strip()
    if raw and setting_came_from_a_file(name):
        _warn_once(f"{name} is set in a voicemode.env file, which a model can write; "
                   "ignoring it. Pass it in the environment only.")
        return ""
    return raw


def _command_came_from_a_file() -> bool:
    return setting_came_from_a_file(BARGE_COMMAND_SETTING)


def barge_command(switch=None):
    """(argv prefix, error). The action is appended to the prefix.

    An explicit ``switch`` wins; else VOICEMODE_BARGE_CONTROL_COMMAND, read from the
    environment only -- never voicemode.env, which upstream's update_config tool lets a
    model write: a JSON list of strings, executable first, or a bare path to one
    executable; else ``voicemode-switch barge``.

    The voice server copies voicemode.env into its own environment (load_voicemode_env,
    on start and on config_reload), so the environment alone proves nothing there: if
    any file upstream loads holds the key, the setting is refused with one warning.
    """
    if switch is None:
        raw = (os.environ.get(BARGE_COMMAND_SETTING) or "").strip()
        if raw and _command_came_from_a_file():
            _warn_once(f"{BARGE_COMMAND_SETTING} is set in a voicemode.env file, which a model "
                       "can write; ignoring it and using voicemode-switch. Pass it in the "
                       "environment only.")
            raw = ""
        if raw:
            if raw.startswith("["):
                try:
                    argv = json.loads(raw)
                except ValueError:
                    argv = None
                if not (isinstance(argv, list) and argv and all(isinstance(a, str) and a for a in argv)):
                    return None, f"{BARGE_COMMAND_SETTING} must be a JSON list of strings or a path"
            elif raw.startswith("{"):
                return None, f"{BARGE_COMMAND_SETTING} must be a JSON list of strings or a path"
            else:
                argv = [raw]
            exe = Path(argv[0])
            if not (exe.exists() or (not exe.is_absolute() and shutil.which(argv[0]))):
                return None, f"barge control command not found: {argv[0]}"
            return argv, None
        switch = default_switch()
    switch = Path(switch)
    if not switch.exists():
        return None, f"voicemode-switch not found at {switch}"
    return [str(switch), "barge"], None


BARGE_PYTHON_SETTING = "VOICEMODE_BARGE_PYTHON"
BARGE_PROBE_TIMEOUT = 10.0
_BARGE_PROBE: dict = {"result": None, "thread": None, "started": False}
_BARGE_PROBE_LOCK = threading.Lock()


def _python_setting_came_from_a_file() -> bool:
    """Same reasoning as barge_command: a model can write voicemode.env, so an
    interpreter named there is never trusted."""
    return setting_came_from_a_file(BARGE_PYTHON_SETTING)


def barge_interpreter() -> str | None:
    """The python that runs the barge trigger: VOICEMODE_BARGE_PYTHON from the environment
    only (refused when a voicemode.env holds it), else the venv beside voicemode-switch."""
    raw = (os.environ.get(BARGE_PYTHON_SETTING) or "").strip()
    if raw and not _python_setting_came_from_a_file():
        return raw if Path(raw).exists() else None
    venv = default_switch().parent / ".venv"
    exe = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return str(exe) if exe.exists() else None


def _probe_barge() -> None:
    result = False
    try:
        py = barge_interpreter()
        if py:
            out = subprocess.run([py, "-m", "voice_mode.voice_barge", "status"],
                                 capture_output=True, text=True, timeout=BARGE_PROBE_TIMEOUT)
            line = (out.stdout or "").strip().splitlines()
            result = bool(line) and not line[0].startswith("cannot run")
    except Exception:  # noqa: BLE001 - any failure means we cannot say the mic is reachable
        result = False
    _BARGE_PROBE["result"] = result


def _barge_probe_reset() -> None:
    with _BARGE_PROBE_LOCK:
        _BARGE_PROBE.update(result=None, thread=None, started=False)


def barge_available() -> bool:
    """Can the interpreter that runs the trigger hear the mic? In-process capture_backend()
    first; otherwise ask the trigger's python once, in a thread, and cache the answer for
    the process lifetime. Unknown-yet counts as unavailable."""
    if capture_backend() is not None:
        return True
    with _BARGE_PROBE_LOCK:
        if not _BARGE_PROBE["started"]:
            _BARGE_PROBE["started"] = True
            t = threading.Thread(target=_probe_barge, daemon=True)
            _BARGE_PROBE["thread"] = t
            t.start()
    return _BARGE_PROBE["result"] is True


def barge_control(action: str, switch=None) -> str:
    """Run the barge control command with ``<action>``, by default
    ``voicemode-switch barge <action>``. The action is checked against a fixed
    list and the command comes from the environment or the caller only, so
    model output never reaches the command line."""
    action = (action or "").strip().lower()
    if action not in ("on", "off", "auto", "status"):
        return "action must be on, off, auto or status"
    argv, error = barge_command(switch)
    if error:
        return error
    extra = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if _on_windows() else {}
    try:
        # `on` may download the Silero model. No stdin: the voice server's is the MCP pipe, and on
        # Windows a child inheriting it blocks at start-up while a thread reads it (2026-11-06).
        done = subprocess.run([*argv, action], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=180, **extra)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"voice barge {action} failed: {e}"
    output = re.sub(r"\x1b\[[0-9;]*m", "", (done.stdout + done.stderr)).strip()
    if done.returncode != 0:
        return f"voice barge {action} failed: {output}"
    return output
