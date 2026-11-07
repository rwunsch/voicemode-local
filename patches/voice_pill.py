"""Backend for the voice pill: one JSON snapshot line out, one JSON command line in.

Run by a window wrapper as a child process (``python voice_pill.py --stdio``, or
``python -m voice_mode.voice_pill --stdio``).  Prefer the file path: ``-m`` first
imports the whole voice_mode package.  This module is stdlib only (no numpy, no
voice_mode.server, no voice_mode.conch, which would pull in psutil) because the
machine's audio stutters when idle helpers compete for CPU.  It exits when stdin
closes, so a closed window ends it and a Pause it owns cannot outlive it.  On
Windows stdin is read by a thread (select takes only sockets there) and the lock
is an msvcrt byte lock; POSIX keeps select and flock.

Data sources, all read directly (never by spawning the voicemode CLI). Upstream's
files are read where upstream keeps them; the pill's own follow the base dir
(VOICEMODE_BASE_DIR, else ~/.voicemode; see voice_pill_state):
  control socket   status: speaking / listening   (absent between turns: normal)
  ~/.voicemode/conch, conch.queue.d/*.json         who holds the floor, who waits
  ~/.voicemode/voicemode.env                       barge flag, beeps default
  <base>/pill/...                                  sessions, pause, overrides, ui
  ~/.claude/sessions/<pid>.json                    Wake: is the woken session busy yet
                                                   (session_wake, read only)

The conch file formats are those of voice-mode 8.12.0 (conch.py, conch_queue.py).
Installed as voice_mode/voice_pill.py by patches/apply.sh.
"""
import errno
import json
import os
import platform as _platform
import queue
import select
import signal
import shutil
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

try:  # beside this file, installed or not; never via the heavy voice_mode package
    import voice_pill_state as st
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import voice_pill_state as st

try:  # Wake: wake the last voice session. Absent on an install that never copied it.
    import session_wake as sw
except ImportError:
    sw = None

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
try:
    import msvcrt
except ImportError:  # POSIX
    msvcrt = None

IDLE_INTERVAL_S = 1.0
TURN_INTERVAL_S = 0.25
# Windows byte-range locks are mandatory: lock a byte far past any content.
# Kept under 2**31 in case the CRT treats the offset as 32-bit (not checked).
LOCK_OFFSET = 1 << 30


class Context:
    """Paths left unset are resolved on every access, so they follow the base dir."""

    def __init__(self, base=None, home=None, sock=None, switch=None, dismissed=None,
                 barge_thread=None):
        self.base, self.home, self.sock = base, home, sock
        self.switch = switch              # barge command: None = the setting, else voicemode-switch
        self.dismissed = dismissed        # session keys hidden by Dismiss
        self.barge_thread = barge_thread
        self.wake = None                  # the last Wake: label, ok, detail, at, claude_pid

    base = property(lambda self: Path(self._base) if self._base else st.default_base(),
                    lambda self, v: setattr(self, "_base", v), doc="pill state dir")
    home = property(lambda self: Path(self._home) if self._home else st.upstream_home(),
                    lambda self, v: setattr(self, "_home", v),
                    doc="upstream's ~/.voicemode: conch, voicemode.env")
    sock = property(lambda self: Path(self._sock) if self._sock else st.control_socket_path(),
                    lambda self, v: setattr(self, "_sock", v), doc="control socket")


# -- conch files -------------------------------------------------------------

def pid_alive(pid) -> bool:
    return st.pid_exists(pid)  # never os.kill(pid, 0) on Windows: it terminates


def _expired(iso) -> bool:
    """True if the ISO time has passed; unparseable or missing counts as not expired."""
    try:
        t = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return False
    return datetime.now(t.tzinfo) > t


def _label(entry: dict) -> str:
    project = entry.get("project_path")
    return (os.path.basename(str(project).rstrip("/")) if project else "") or entry.get("agent") or "voice"


def read_conch(home) -> dict:
    """{"holder": {pid,label,voice}|None, "waiters": [{pid,label,seq}] in queue order}."""
    home = Path(home)
    holder = None
    data = st._read_json(home / "conch")
    pid = data.get("pid")
    if pid_alive(pid) and not (data.get("held") and _expired(data.get("expires"))):
        holder = {"pid": pid, "label": _label(data), "voice": data.get("voice")}
    waiters = []
    try:
        files = list((home / "conch.queue.d").glob("*.json"))
    except OSError:
        files = []
    for f in files:
        w = st._read_json(f)
        wpid = w.get("pid")
        live = pid_alive(wpid) if wpid is not None else (w.get("expires") and not _expired(w["expires"]))
        if live and isinstance(w.get("seq"), int):
            waiters.append({"pid": wpid, "label": _label(w), "seq": w["seq"]})
    waiters.sort(key=lambda w: w["seq"])
    return {"holder": holder, "waiters": waiters}


# -- snapshot ----------------------------------------------------------------

def session_keys(sessions) -> frozenset:
    return frozenset((s["pid"], s.get("started_at")) for s in sessions)


def detect_platform() -> str:
    if sys.platform == "darwin":
        return "mac"
    if sys.platform.startswith("win"):
        return "windows"
    # voice-mode inside WSL2 is shown by a Windows-side window.
    if os.environ.get("WSL_DISTRO_NAME") or "microsoft" in _platform.uname().release.lower():
        return "windows"
    return "linux"


def _floor(sessions, conch, last_floor):
    """(floor, is_holder): the conch holder, else the last holder if still registered."""
    labels = {s["pid"]: s.get("label") for s in sessions}
    holder = conch.get("holder")
    if holder:
        return {"pid": holder["pid"], "label": labels.get(holder["pid"]) or holder["label"]}, True
    if last_floor and last_floor.get("pid") in labels:
        return {"pid": last_floor["pid"], "label": labels[last_floor["pid"]] or last_floor.get("label")}, False
    return None, False


GERMAN_PREFIXES = ("dm_", "p_de_")   # the German voices: Kokoro dm_martin, Piper p_de_*
GERMAN_VOICE = "dm_martin"


def voice_language(voice) -> str:
    return "German" if isinstance(voice, str) and voice.startswith(GERMAN_PREFIXES) else "English"


def _floor_voice(floor, is_holder, sessions, conch, overrides):
    ov = overrides.get(floor["pid"], {})
    own = next((s for s in sessions if s["pid"] == floor["pid"]), {})
    held = (conch.get("holder") or {}).get("voice") if is_holder else None
    return ov.get("voice") or own.get("voice") or held


def build_snapshot(status, sessions, conch, *, paused=False, ptt=False, dismissed=None,
                   last_floor=None, overrides=None, barge=None, ui=None, platform=None, beeps=False,
                   default_voice=st.FALLBACK_VOICE, play=None, wake=None) -> dict:
    overrides = overrides or {}
    floor, is_holder = _floor(sessions, conch, last_floor)
    new = session_keys(sessions) - (dismissed or frozenset())
    visible = bool(new) if dismissed is not None else bool(sessions or ptt)
    if paused:
        phase = "paused"
    elif status.get("holding"):
        # The push-to-talk key is down (patch_ptt_instant's status field): the press
        # has cut the reply, so this outranks speaking and listening.
        phase = "holding"
    elif status.get("speaking"):
        phase = "speaking"
    elif status.get("listening"):
        phase = "listening"
    elif floor and any(s["pid"] == floor["pid"] and s.get("stopped") for s in sessions):
        phase = "stopped"
    else:
        phase = "working" if sessions else "idle"
    positions = {w["pid"]: i + 1 for i, w in enumerate(conch.get("waiters", []))}
    rows = []
    for s in sessions:
        pid = s["pid"]
        if s.get("stopped"):
            state, pos = "stopped", None
        elif is_holder and pid == floor["pid"]:
            state, pos = "talking", None
        elif pid in positions:
            state, pos = "waiting", positions[pid]
        else:
            state, pos = "working", None
        rows.append({"pid": pid, "label": s.get("label"), "state": state, "position": pos})
    voice = language = None
    if floor:
        voice = _floor_voice(floor, is_holder, sessions, conch, overrides)
        language = overrides.get(floor["pid"], {}).get("language")
    voice = voice or default_voice          # nobody has chosen: the configured default
    language = language or voice_language(voice)
    return {"visible": visible, "phase": phase, "floor": floor, "sessions": rows,
            "voice": voice, "language": language,
            "barge": barge or {"enabled": False, "available": False, "count": 0},
            "beeps": beeps,
            "platform": platform or detect_platform(), "ui": ui or {},
            "play": play, "wake": wake}


def _barge_enabled(home) -> bool:
    # The toggle (voicemode-switch barge) writes voicemode.env: read it from there first.
    return (st.file_first_setting("VOICEMODE_VOICE_BARGE", "", home) or "").strip() == "true"


def _beeps_on(ctx) -> bool:
    """The pill's choice, else voice-mode's own setting: on unless the env says otherwise."""
    choice = st.beeps_choice(ctx.base)
    if choice is not None:
        return choice
    value = (st.setting("VOICEMODE_AUDIO_FEEDBACK", "true", ctx.home) or "").strip().lower()
    return value in ("true", "1", "yes", "on")


def _midnight() -> float:
    return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _remember_floor(ctx, conch):
    holder = conch.get("holder")
    if not holder:
        return st._read_json(ctx.base / "last_floor.json")
    record = {"pid": holder["pid"], "label": holder["label"]}
    if st._read_json(ctx.base / "last_floor.json") != record:
        try:
            st._write_json(ctx.base / "last_floor.json", record)
        except OSError:
            pass
    return record


# -- Wake (session_wake; the pill's old Play) -------------------------------------------------------

WAKE_ANSWER_S = 5.0     # a woken session that is not busy by then did not answer
WAKE_SHOW_S = 10.0      # how long a Wake result stays in the pill
WAKE_FAIL_SHOW_S = 6.0


def _play_target(ctx, sessions):
    """The session Wake would wake, or None (no session, setting off, module missing)."""
    if sw is None:
        return None
    try:
        if not sw.enabled(ctx.home):
            return None
        return sw.last_voice_session(home=ctx.home, base=ctx.base, sessions=sessions)
    except Exception:  # noqa: BLE001 - a broken lookup hides Wake, never the pill
        return None


def _play(ctx) -> None:
    now = time.monotonic()
    target = _play_target(ctx, st.live_sessions(ctx.base))
    if target is None:
        ctx.wake = {"label": None, "ok": False, "detail": "no session", "at": now, "claude_pid": None}
        return
    ok, detail = sw.wake(target.claude_pid, "wake")
    print(f"voice_pill: wake -> {target.label}: {'sent' if ok else detail}", file=sys.stderr, flush=True)
    ctx.wake = {"label": target.label, "ok": ok, "detail": detail, "at": now,
                "claude_pid": target.claude_pid, "answered": False}


def wake_view(ctx, now=None):
    """The Wake result to show, or None once it has been shown long enough."""
    w = ctx.wake
    if not w:
        return None
    now = time.monotonic() if now is None else now
    age = now - w["at"]
    if w["label"] is None:
        return {"text": "No voice session to wake", "ok": False} if age < WAKE_FAIL_SHOW_S else None
    if not w["ok"]:
        return ({"text": f"Can't wake {w['label']}: {w['detail']}", "ok": False}
                if age < WAKE_FAIL_SHOW_S else None)
    if age >= WAKE_SHOW_S:
        return None
    if not w.get("answered"):
        try:
            w["answered"] = sw.registry_status(w["claude_pid"]) == "busy"
        except Exception:  # noqa: BLE001
            pass
    if w.get("answered"):
        return {"text": f"Woke {w['label']}", "ok": True}
    if age < WAKE_ANSWER_S:
        return {"text": f"Waking {w['label']}\u2026 listen for \"Listening.\"", "ok": True}
    return {"text": f"No answer from {w['label']}", "ok": False}


def gather(ctx) -> dict:
    sessions = st.live_sessions(ctx.base)
    play = _play_target(ctx, sessions)
    conch = read_conch(ctx.home)
    barge = {"enabled": _barge_enabled(ctx.home), "available": st.barge_available(),
             "count": st.interruptions_since(_midnight(), ctx.base)}
    return build_snapshot(
        st._status(ctx.sock), sessions, conch, paused=st.is_paused(ctx.base),
        ptt=st.ptt_recent(base=ctx.base), dismissed=ctx.dismissed,
        last_floor=_remember_floor(ctx, conch),
        overrides={s["pid"]: st.get_override(s["pid"], ctx.base) for s in sessions},
        barge=barge, ui=st.load_ui(ctx.base), beeps=_beeps_on(ctx),
        default_voice=st.default_voice(ctx.home),
        play={"label": play.label} if play else None, wake=wake_view(ctx))


# -- commands ----------------------------------------------------------------

def _set_override(ctx, floor, **change):
    cur = st.get_override(floor["pid"], ctx.base)
    cur.update(change)
    st.set_override(floor["pid"], cur.get("voice"), cur.get("language"), ctx.base)


def _toggle_barge(action, switch):
    print(f"voice barge {action}: {st.barge_control(action, switch)}", file=sys.stderr, flush=True)


def handle_command(msg, ctx=None) -> None:
    """Apply one command. Anything malformed or unknown is ignored."""
    ctx = ctx or Context()
    if not isinstance(msg, dict):
        return
    cmd = msg.get("cmd")
    if cmd == "interrupt":
        st.send_control(ctx.sock, "skip_forward")
    elif cmd == "pause":
        # Our own pid owns the pause (mic held closed); the socket pause holds TTS.
        # Upstream stops a paused turn after VOICEMODE_CONTROL_PAUSE_TIMEOUT (300 s).
        st.set_paused(True, os.getpid(), ctx.base)
        st.send_control(ctx.sock, "pause")
    elif cmd == "resume":
        st.set_paused(False, os.getpid(), ctx.base)
        st.send_control(ctx.sock, "resume")
    elif cmd == "stop":
        floor, is_holder = _floor(st.live_sessions(ctx.base), read_conch(ctx.home),
                                  st._read_json(ctx.base / "last_floor.json"))
        if floor:
            # A live socket belongs to the holder's turn; only a holder may be cut by it.
            st.mark_stopped(floor["pid"], True, ctx.base)  # first: the cut turn's result reads it
            if not (is_holder and st.send_control(ctx.sock, "stop")):
                st.request_stop(floor["pid"], ctx.base)
    elif cmd == "dismiss":
        ctx.dismissed = session_keys(st.live_sessions(ctx.base))
    elif cmd == "set_barge" and isinstance(msg.get("on"), bool):
        if ctx.barge_thread is None or not ctx.barge_thread.is_alive():
            # `on` can download a model: never block the snapshot loop on it.
            ctx.barge_thread = threading.Thread(
                target=_toggle_barge, args=("on" if msg["on"] else "off", ctx.switch), daemon=True)
            ctx.barge_thread.start()
    elif cmd == "set_beeps" and isinstance(msg.get("on"), bool):
        st.set_beeps(msg["on"], ctx.base)
    elif cmd in ("set_voice", "set_language"):
        key = "voice" if cmd == "set_voice" else "language"
        sessions, conch = st.live_sessions(ctx.base), read_conch(ctx.home)
        floor, is_holder = _floor(sessions, conch, st._read_json(ctx.base / "last_floor.json"))
        if floor and isinstance(msg.get(key), str):
            change = {key: msg[key]}
            if key == "language" and msg[key] in ("English", "German"):
                # A voice speaks only its own language: move the voice with the language.
                cur = _floor_voice(floor, is_holder, sessions, conch,
                                   {floor["pid"]: st.get_override(floor["pid"], ctx.base)}) \
                    or st.default_voice(ctx.home)
                if voice_language(cur) != msg[key]:
                    default = st.default_voice(ctx.home)
                    change["voice"] = (GERMAN_VOICE if msg[key] == "German" else
                                       default if voice_language(default) == "English" else st.FALLBACK_VOICE)
            _set_override(ctx, floor, **change)
    elif cmd in ("wake", "play"):   # "play" is the old name; keep it working
        if sw is not None:
            _play(ctx)
    elif cmd == "save_ui" and isinstance(msg.get("ui"), dict):
        st.save_ui(msg["ui"], ctx.base)


# -- process -----------------------------------------------------------------

def acquire_lock(base):
    """Open file holding the single-instance lock, or None if another backend has it."""
    f = open(st._base(base) / "backend.lock", "a+")
    try:
        if fcntl is not None:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        elif msvcrt is not None:
            f.seek(LOCK_OFFSET)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        f.close()
        return None
    return f


def release_lock(f) -> None:
    """Unlock, then close: Windows releases a closed handle's locks only eventually."""
    if fcntl is None and msvcrt is not None:
        try:
            f.seek(LOCK_OFFSET)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    f.close()


def _release_pause(ctx):
    """If this process owns the pause, lift it, and tell the server (harmless without a socket)."""
    if st._read_json(ctx.base / "paused.json").get("owner") == os.getpid():
        st.set_paused(False, os.getpid(), ctx.base)
        st.send_control(ctx.sock, "resume")


def _emit(ctx, out) -> float:
    snap = gather(ctx)
    out.write(json.dumps(snap) + "\n")
    out.flush()
    return TURN_INTERVAL_S if snap["phase"] in ("speaking", "listening", "holding") else IDLE_INTERVAL_S


class SelectInput:
    """POSIX: wait on the fd itself."""

    def __init__(self, fd):
        self._fd = fd

    def wait(self, timeout):
        """Bytes read, b"" at end of input, or None if nothing came in time."""
        ready, _, _ = select.select([self._fd], [], [], timeout)
        return os.read(self._fd, 65536) if ready else None


class ThreadInput:
    """Windows: select() takes only sockets, so a daemon thread reads the pipe."""

    def __init__(self, fd):
        self._queue = queue.Queue()
        self._thread = threading.Thread(target=self._read, args=(fd,), daemon=True,
                                        name="voice-pill-stdin")
        self._thread.start()

    def _read(self, fd):
        while True:
            try:
                chunk = os.read(fd, 65536)
            except OSError:  # a broken pipe is the window gone: same as EOF
                chunk = b""
            self._queue.put(chunk)
            if not chunk:
                return

    def wait(self, timeout):
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None


def _input_source(fd):
    return ThreadInput(fd) if st._on_windows() else SelectInput(fd)


def run_stdio(ctx, stdin_fd=0, out=None) -> None:
    out = out or sys.stdout
    source = _input_source(stdin_fd)
    buf, interval = b"", _emit(ctx, out)
    next_at = time.monotonic() + interval
    while True:
        chunk = source.wait(max(0.0, next_at - time.monotonic()))
        sent = False
        if chunk is not None:
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    handle_command(json.loads(line), ctx)
                except Exception as e:  # one bad command must not end the pill
                    print(f"voice_pill: command failed: {e}", file=sys.stderr)
                sent = True
        if sent or time.monotonic() >= next_at:
            interval = _emit(ctx, out)
            next_at = time.monotonic() + interval


def _install_stop_signals() -> None:
    """The wrapper stops us with SIGTERM (Ctrl-Break on Windows): treat it like Ctrl-C so
    the finally runs. Windows' TerminateProcess sends no signal at all; stdin EOF is the
    clean exit there. Signals this platform lacks, or a non-main thread, are skipped."""
    for name in ("SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, signal.default_int_handler)
        except (ValueError, OSError):
            pass


def _log_prune(msg: str) -> None:
    print(f"voice_pill: {msg}", file=sys.stderr, flush=True)


def main(argv=None) -> int:
    ctx = Context()
    print(f"voice_pill: started pid={os.getpid()} base={ctx.base} home={ctx.home} sock={ctx.sock} "
          f"TZ={os.environ.get('TZ', '')}", file=sys.stderr, flush=True)
    st.prune_log = _log_prune
    lock = acquire_lock(ctx.base)
    if lock is None:
        print("voice_pill: another backend is running", file=sys.stderr)
        return 1
    st.enable_backend_mode()  # this process may sleep on a busy file; the server may not
    _install_stop_signals()
    try:
        run_stdio(ctx)
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    except OSError as e:
        # Windows reports a write to a closed pipe as EINVAL, not EPIPE.
        if not (st._on_windows() and e.errno == errno.EINVAL):
            raise
    finally:
        _release_pause(ctx)
        release_lock(lock)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
