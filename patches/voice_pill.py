"""Backend for the voice pill: one JSON snapshot line out, one JSON command line in.

Run by a window wrapper as a child process (``python voice_pill.py --stdio``, or
``python -m voice_mode.voice_pill --stdio``).  Prefer the file path: ``-m`` first
imports the whole voice_mode package.  This module is stdlib only (no numpy, no
voice_mode.server, no voice_mode.conch, which would pull in psutil) because the
machine's audio stutters when idle helpers compete for CPU.  It exits when stdin
closes, so a closed window ends it and a Pause it owns cannot outlive it.

Data sources, all read directly (never by spawning the voicemode CLI):
  control socket   status: speaking / listening   (absent between turns: normal)
  ~/.voicemode/conch, conch.queue.d/*.json         who holds the floor, who waits
  ~/.voicemode/pill/...                            sessions, pause, overrides, ui

The conch file formats are those of voice-mode 8.12.0 (conch.py, conch_queue.py).
Installed as voice_mode/voice_pill.py by patches/apply.sh.
"""
import json
import os
import platform as _platform
import select
import signal
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

try:  # beside this file, installed or not; never via the heavy voice_mode package
    import voice_pill_state as st
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import voice_pill_state as st

try:
    import fcntl
except ImportError:  # Windows: no single-instance guard
    fcntl = None

IDLE_INTERVAL_S = 1.0
TURN_INTERVAL_S = 0.25


@dataclass
class Context:
    base: Path = None            # pill state dir
    home: Path = None            # ~/.voicemode (conch files, voicemode.env)
    sock: Path = None            # control socket
    switch: Path = None          # voicemode-switch, None = next to the venv
    dismissed: frozenset = None  # session keys hidden by Dismiss
    barge_thread: threading.Thread = None

    def __post_init__(self):
        self.base = Path(self.base) if self.base else st.default_base()
        self.home = Path(self.home) if self.home else Path(os.path.expanduser("~")) / ".voicemode"
        self.sock = Path(self.sock) if self.sock else st.control_socket_path()


# -- conch files -------------------------------------------------------------

def pid_alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if sys.platform.startswith("win"):
        return st.process_start(pid) is not None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


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


def build_snapshot(status, sessions, conch, *, paused=False, ptt=False, dismissed=None,
                   last_floor=None, overrides=None, barge=None, ui=None, platform=None) -> dict:
    overrides = overrides or {}
    floor, is_holder = _floor(sessions, conch, last_floor)
    new = session_keys(sessions) - (dismissed or frozenset())
    visible = bool(new) if dismissed is not None else bool(sessions or ptt)
    if paused:
        phase = "paused"
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
        ov = overrides.get(floor["pid"], {})
        own = next((s for s in sessions if s["pid"] == floor["pid"]), {})
        held_voice = (conch.get("holder") or {}).get("voice") if is_holder else None
        voice = ov.get("voice") or own.get("voice") or held_voice
        language = ov.get("language")
    return {"visible": visible, "phase": phase, "floor": floor, "sessions": rows,
            "voice": voice, "language": language,
            "barge": barge or {"enabled": False, "available": False, "count": 0},
            "platform": platform or detect_platform(), "ui": ui or {}}


def _barge_enabled(home) -> bool:
    value = ""
    try:
        for line in (Path(home) / "voicemode.env").read_text().splitlines():
            if line.startswith("VOICEMODE_VOICE_BARGE="):
                value = line.split("=", 1)[1].strip()
    except OSError:
        pass
    return value == "true"


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


def gather(ctx) -> dict:
    sessions = st.live_sessions(ctx.base)
    conch = read_conch(ctx.home)
    barge = {"enabled": _barge_enabled(ctx.home), "available": shutil.which("parec") is not None,
             "count": st.interruptions_since(_midnight(), ctx.base)}
    return build_snapshot(
        st._status(ctx.sock), sessions, conch, paused=st.is_paused(ctx.base),
        ptt=st.ptt_recent(base=ctx.base), dismissed=ctx.dismissed,
        last_floor=_remember_floor(ctx, conch),
        overrides={s["pid"]: st.get_override(s["pid"], ctx.base) for s in sessions},
        barge=barge, ui=st.load_ui(ctx.base))


# -- commands ----------------------------------------------------------------

def _set_override(ctx, floor, **change):
    cur = st.get_override(floor["pid"], ctx.base)
    cur.update(change)
    st.set_override(floor["pid"], cur.get("voice"), cur.get("language"), ctx.base)


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
                target=st.barge_control, args=("on" if msg["on"] else "off", ctx.switch), daemon=True)
            ctx.barge_thread.start()
    elif cmd in ("set_voice", "set_language"):
        key = "voice" if cmd == "set_voice" else "language"
        floor, _ = _floor(st.live_sessions(ctx.base), read_conch(ctx.home),
                          st._read_json(ctx.base / "last_floor.json"))
        if floor and isinstance(msg.get(key), str):
            _set_override(ctx, floor, **{key: msg[key]})
    elif cmd == "save_ui" and isinstance(msg.get("ui"), dict):
        st.save_ui(msg["ui"], ctx.base)


# -- process -----------------------------------------------------------------

def acquire_lock(base):
    """Open file holding the single-instance lock, or None if another backend has it."""
    f = open(st._base(base) / "backend.lock", "a+")
    if fcntl is not None:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return None
    return f


def _release_pause(ctx):
    """If this process owns the pause, lift it, and tell the server (harmless without a socket)."""
    if st._read_json(ctx.base / "paused.json").get("owner") == os.getpid():
        st.set_paused(False, os.getpid(), ctx.base)
        st.send_control(ctx.sock, "resume")


def _emit(ctx, out) -> float:
    snap = gather(ctx)
    out.write(json.dumps(snap) + "\n")
    out.flush()
    return TURN_INTERVAL_S if snap["phase"] in ("speaking", "listening") else IDLE_INTERVAL_S


def run_stdio(ctx, stdin_fd=0, out=None) -> None:
    out = out or sys.stdout
    buf, interval = b"", _emit(ctx, out)
    next_at = time.monotonic() + interval
    while True:
        ready, _, _ = select.select([stdin_fd], [], [], max(0.0, next_at - time.monotonic()))
        sent = False
        if ready:
            chunk = os.read(stdin_fd, 65536)
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


def main(argv=None) -> int:
    ctx = Context()
    lock = acquire_lock(ctx.base)
    if lock is None:
        print("voice_pill: another backend is running", file=sys.stderr)
        return 1
    # The wrapper stops us with SIGTERM: treat it like Ctrl-C so the finally runs.
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        run_stdio(ctx)
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    finally:
        _release_pause(ctx)
        lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
