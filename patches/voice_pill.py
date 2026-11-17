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

Voice preview (2026-11-17): {"cmd": "preview_voice", "voice": id} asks the first local TTS
server that lists the voice for one short sentence and plays it in a child process
(``voice_pill.py --play-wav``, numpy + sounddevice like voice-mode's own player), so this
process stays stdlib only. Never while a voice turn is on; answers are window lines.

The conch file formats are those of voice-mode 8.12.0 (conch.py, conch_queue.py).
Installed as voice_mode/voice_pill.py by patches/apply.sh.
"""
import errno
import importlib.util
import json
import os
import platform as _platform
import queue
import select
import signal
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
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
# (Robert, 2026-11-10) The pill closes with the last voice session: once no session (and
# no recent push-to-talk) has been seen for QUIT_GRACE_S, the backend prints {"quit": true}
# and exits EXIT_DONE; the window closes on that and does not restart it. Never in the
# first START_GRACE_S, so a window started just before its session registers survives.
START_GRACE_S = 30.0
QUIT_GRACE_S = 20.0
EXIT_DONE = 3
# Windows byte-range locks are mandatory: lock a byte far past any content.
# Kept under 2**31 in case the CRT treats the offset as 32-bit (not checked).
LOCK_OFFSET = 1 << 30


class Context:
    """Paths left unset are resolved on every access, so they follow the base dir."""

    def __init__(self, base=None, home=None, sock=None, switch=None, barge_thread=None):
        self.base, self.home, self.sock = base, home, sock
        self.switch = switch              # barge command: None = the setting, else voicemode-switch
        self.alive = False                # the last gather saw a session or a recent push-to-talk
        self.barge_thread = barge_thread
        self.barge_wanted = None          # the last barge state asked for: "on" or "off"
        self.barge_running = False        # a worker is applying it (set and cleared under the lock)
        self.barge_lock = threading.Lock()
        self.wake = None                  # the last Wake: label, ok, detail, at, claude_pid
        self.replies = queue.Queue()      # window lines for the page from worker threads
        self.preview_lock = threading.Lock()
        self.preview_running = False      # one preview at a time (set and cleared under the lock)
        self.preview_thread = None
        self.preview_proc = None          # the child playing the sample, while it plays
        self.preview_cut = False          # the guard stopped it: a turn began

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
    """The language a voice speaks, from its id (language_from_id); English when unknown.
    It knew only English and German until every local language reached the pill (2026-11-16)."""
    return language_from_id(voice) or "English"


LANGUAGES_SETTING = "VOICEMODE_PILL_LANGUAGES"   # e.g. "English" or "English,German"
PIPER_PORT = ":8881"                             # piper-proxy, which serves p_de_* (voicemode-switch)
KOKORO_MODEL = Path("models/kokoro/kokoro-v1.0.onnx")
GERMAN_MODEL = (Path("models/kokoro-de-martin/kokoro-martin.onnx"),   # kokoro-onnx-server.py:
                Path("models/kokoro-de-martin/voices-martin.npz"))    # <model-dir>-de-martin


def _install_dirs():
    """Folders that may hold models/kokoro: the install this backend runs from (a repo
    checkout, or a native runtime whose .venv runs it), and the repo beside voicemode-switch."""
    dirs = [Path(sys.prefix).parent, Path(__file__).resolve().parent.parent,
            st.default_switch().parent]
    return list(dict.fromkeys(dirs))


def listed_languages(home=None):
    """The languages VOICEMODE_PILL_LANGUAGES names, lower-cased (a comma list of the names
    the pill shows: English, German, Spanish ...), or None when it is unset or blank."""
    raw = (st.setting(LANGUAGES_SETTING, "", home) or "").strip()
    if not raw:
        return None
    return [x.strip().lower() for x in raw.split(",") if x.strip()]


def german_available(home=None) -> bool:
    """Can a German voice speak here? VOICEMODE_PILL_LANGUAGES decides when set. Otherwise yes
    if a German voice is configured, Piper is in the TTS chain, or the German Kokoro model is
    installed; no if the English Kokoro model is found without it (FELIX); and yes when
    nothing can be found, as before, because a wrong no hides a working voice."""
    listed = listed_languages(home)
    if listed is not None:
        return "german" in listed
    for name in ("VOICEMODE_VOICES", "VOICEMODE_TTS_VOICE"):
        voices = (st.setting(name, "", home) or "").split(",")
        if any(voice_language(v.strip()) == "German" for v in voices):
            return True
    if PIPER_PORT in (st.setting("VOICEMODE_TTS_BASE_URLS", "", home) or ""):
        return True
    found_english = False
    for d in _install_dirs():
        try:
            if all((d / f).is_file() for f in GERMAN_MODEL):
                return True
            found_english = found_english or (d / KOKORO_MODEL).is_file()
        except OSError:
            continue
    return not found_english


GERMAN_VOICES = ("dm_martin", "p_de_thorsten", "p_de_eva")


def german_voices(home=None) -> list:
    """The German voices that can speak here, for the pill's voice list (2026-11-16: dm_martin
    was offered with Piper in the chain but no German Kokoro model, and failed on every server).
    dm_martin needs the German Kokoro model; p_de_* need Piper in the TTS chain or a configured
    p_de_* voice. With no evidence either way, all are offered, as before: a wrong no hides a
    working voice."""
    kokoro_german = None   # True: model found; False: English Kokoro found without it
    for d in _install_dirs():
        try:
            if all((d / f).is_file() for f in GERMAN_MODEL):
                kokoro_german = True
                break
            if (d / KOKORO_MODEL).is_file():
                kokoro_german = False
        except OSError:
            continue
    configured = []
    for name in ("VOICEMODE_VOICES", "VOICEMODE_TTS_VOICE"):
        configured += [v.strip() for v in (st.setting(name, "", home) or "").split(",")]
    piper = (PIPER_PORT in (st.setting("VOICEMODE_TTS_BASE_URLS", "", home) or "")
             or any(v.startswith("p_de_") for v in configured))
    if kokoro_german is None and not piper:
        return list(GERMAN_VOICES)
    out = []
    if kokoro_german is not False or "dm_martin" in configured:
        out.append("dm_martin")
    if piper:
        out += ["p_de_thorsten", "p_de_eva"]
    return out


# -- every local voice, by language (2026-11-16) -----------------------------------------
# The pill listed only English and German from lists in model.js. Each local TTS server says
# which voices it has: the Piper proxy and kokoro-onnx-server answer {"data": [{id,
# language_name}]}, Kokoro-FastAPI {"voices": [ids]}. Choosing any language already works:
# voice_pill_state.language_note tells the model to write in it.

KOKORO_LANGUAGES = {"a": "English", "b": "English", "e": "Spanish", "f": "French", "h": "Hindi",
                    "i": "Italian", "j": "Japanese", "p": "Portuguese", "z": "Chinese", "d": "German"}
EXPERIMENTAL_PREFIXES = ("j", "z")   # Kokoro Japanese / Chinese: espeak-only phonemising, 2026-11-17
PIPER_LANGUAGES = {"de": "German", "nl": "Dutch", "pl": "Polish", "ru": "Russian", "ko": "Korean"}
LOCAL_HOSTS = ("127.0.0.1", "localhost")
VOICES_TIMEOUT_S = 1.0
VOICES_CACHE_S = 60.0          # the snapshot loop runs every 0.25-1 s: do not ask every tick
_VOICES_CACHE = {}             # URL setting -> (time, ({language: [voice ids]}, {experimental ids}))
_clock = time.monotonic


def language_from_id(voice):
    """A voice's language from its id alone: Piper's p_<code>_..., else Kokoro's first
    letter. None when neither is known."""
    if not isinstance(voice, str) or not voice:
        return None
    if voice.startswith("p_") and voice.count("_") >= 2:
        return PIPER_LANGUAGES.get(voice.split("_")[1])
    return KOKORO_LANGUAGES.get(voice[0])


def _fetch_voices(url):
    """The parsed JSON a server answers at url, without any proxy (they are local)."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=VOICES_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _local_base_urls(raw):
    """The local entries of a VOICEMODE_TTS_BASE_URLS value, in order, without a trailing /."""
    urls = []
    for item in (raw or "").split(","):
        item = item.strip()
        try:
            host = urllib.parse.urlsplit(item).hostname
        except ValueError:
            continue
        if host in LOCAL_HOSTS:
            urls.append(item.rstrip("/"))
    return urls


def _local_voice_urls(raw):
    return [u + "/audio/voices" for u in _local_base_urls(raw)]


def _id_quality(voice):
    """A voice's quality from its id alone, for servers that say nothing: Kokoro's j (Japanese)
    and z (Chinese) voices are experimental (2026-11-17, round trips n=1); Piper's p_<code>_
    voices and every other Kokoro voice are good."""
    if not isinstance(voice, str) or not voice or voice.startswith("p_"):
        return "good"
    return "experimental" if voice[0] in EXPERIMENTAL_PREFIXES else "good"


def _voice_entries(body):
    """[(voice id, language, quality)] from either answer shape; anything else gives nothing.
    quality is the server's own "quality" when it is "experimental", else classed by the id
    when the server gave none; an unknown value counts as good."""
    out = []
    if isinstance(body, dict) and isinstance(body.get("data"), list):
        for item in body["data"]:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            name = item.get("language_name")
            lang = name if isinstance(name, str) and name else language_from_id(item["id"])
            said = item.get("quality")
            quality = ("experimental" if said == "experimental" else "good") if isinstance(said, str) \
                else _id_quality(item["id"])
            out.append((item["id"], lang, quality))
    elif isinstance(body, dict) and isinstance(body.get("voices"), list):
        out = [(v, language_from_id(v), _id_quality(v)) for v in body["voices"] if isinstance(v, str)]
    return [(v, lang, q) for v, lang, q in out if lang]


def _voice_pairs(body):
    """[(voice id, language)] from either answer shape; anything else gives nothing."""
    return [(v, lang) for v, lang, _ in _voice_entries(body)]


def _ask_servers(raw):
    """({language: [voice ids]}, {experimental voice ids}); the first server naming a voice
    decides its quality."""
    merged, experimental, seen = {}, set(), set()
    for url in _local_voice_urls(raw):
        try:
            entries = _voice_entries(_fetch_voices(url))
        except Exception:  # noqa: BLE001 - a server that is down or odd is skipped
            continue
        for voice, lang, quality in entries:
            ids = merged.setdefault(lang, [])
            if voice not in ids:
                ids.append(voice)
            if voice not in seen:
                seen.add(voice)
                if quality == "experimental":
                    experimental.add(voice)
    return merged, experimental


def voices_by_language(home=None) -> dict:
    """{language: [voice ids]} from the local TTS servers in VOICEMODE_TTS_BASE_URLS, in
    server order (first server first). Cached for VOICES_CACHE_S per URL setting. Never
    raises; {} when nothing answered. VOICEMODE_PILL_LANGUAGES, when set, keeps only the
    languages it lists. Voices a server marks "experimental" (Japanese, Chinese: round trips came
    back as nonsense, 2026-11-17) are hidden unless their language is named in that setting;
    a server that gives no quality is classed by the voice id (j, z). A German voice german_voices() knows but does not
    offer here (dm_martin without its model) is dropped; one it does not know is kept,
    because its server says it speaks it (Piper's p_de_kerstin)."""
    try:
        raw = st.setting("VOICEMODE_TTS_BASE_URLS", "", home) or ""
        now = _clock()
        hit = _VOICES_CACHE.get(raw)
        if hit is None or now - hit[0] >= VOICES_CACHE_S:
            hit = (now, _ask_servers(raw))
            _VOICES_CACHE[raw] = hit
        allowed = set(german_voices(home))
        listed = listed_languages(home)   # VOICEMODE_PILL_LANGUAGES: only these, when set
        out = {}
        by_lang, experimental = hit[1]
        for lang, ids in by_lang.items():
            if listed is not None and lang.lower() not in listed:
                continue
            opted_in = listed is not None and lang.lower() in listed   # naming a language opts in
            keep = [v for v in ids if (v not in GERMAN_VOICES or v in allowed)
                    and (opted_in or v not in experimental)]
            if keep:
                out[lang] = keep
        return out
    except Exception:  # noqa: BLE001 - a broken list hides the menus, never the pill
        return {}


PTT_SETTING = "VOICEMODE_PTT_ENABLED"


def ptt_listening(platform, home=None) -> bool:
    """Is a push-to-talk listener running, so its keys may be shown? The setting when it
    says yes or no. Unset: on a Mac, exactly while voicemode-mcp's listener lives
    (<upstream home>/run/ptt-listener.pid names a live process; FELIX starts none).
    Elsewhere on, as before."""
    raw = (st.setting(PTT_SETTING, "", home) or "").strip().strip('"').lower()
    if raw in ("false", "0", "no", "off"):
        return False
    if raw in ("true", "1", "yes", "on"):
        return True      # said so: e.g. a listener started by hand, without voicemode-mcp
    if platform == "mac":
        try:
            pid = int((st.upstream_home() / "run" / "ptt-listener.pid").read_text().strip())
            os.kill(pid, 0)
            return True
        except (OSError, ValueError):
            return False
    return True


def _floor_voice(floor, is_holder, sessions, conch, overrides):
    ov = overrides.get(floor["pid"], {})
    own = next((s for s in sessions if s["pid"] == floor["pid"]), {})
    held = (conch.get("holder") or {}).get("voice") if is_holder else None
    return ov.get("voice") or own.get("voice") or held


def build_snapshot(status, sessions, conch, *, paused=False, ptt=False, closed=False,
                   last_floor=None, overrides=None, barge=None, ui=None, platform=None, beeps=False,
                   default_voice=st.FALLBACK_VOICE, play=None, wake=None,
                   german=True, ptt_listener=None, german_list=None, voices=None) -> dict:
    overrides = overrides or {}
    floor, is_holder = _floor(sessions, conch, last_floor)
    visible = bool(sessions or ptt) and not closed
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
    platform = platform or detect_platform()
    return {"visible": visible, "phase": phase, "floor": floor, "sessions": rows,
            "voice": voice, "language": language,
            "barge": barge or {"enabled": False, "available": False, "count": 0},
            "beeps": beeps,
            "platform": platform, "ui": ui or {},
            "play": play, "wake": wake, "german_available": bool(german),
            "german_voices": list(german_list) if german_list is not None else list(GERMAN_VOICES),
            "ptt": (platform != "mac") if ptt_listener is None else bool(ptt_listener),
            "voices_by_language": dict(voices or {})}


def _barge_enabled(home) -> bool:
    # The toggle (voicemode-switch barge) writes voicemode.env: read it from there first.
    return (st.file_first_setting("VOICEMODE_VOICE_BARGE", "", home) or "").strip() in ("true", "auto")


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
    ptt = st.ptt_recent(base=ctx.base)
    ctx.alive = bool(sessions or ptt)
    play = _play_target(ctx, sessions)
    conch = read_conch(ctx.home)
    barge = {"enabled": _barge_enabled(ctx.home), "available": st.barge_available(),
             "count": st.interruptions_since(_midnight(), ctx.base)}
    platform = detect_platform()
    return build_snapshot(
        st._status(ctx.sock), sessions, conch, paused=st.is_paused(ctx.base),
        ptt=ptt, closed=st.pill_closed(ctx.base),
        last_floor=_remember_floor(ctx, conch),
        overrides={s["pid"]: st.get_override(s["pid"], ctx.base) for s in sessions},
        barge=barge, ui=st.load_ui(ctx.base), beeps=_beeps_on(ctx),
        default_voice=st.default_voice(ctx.home),
        play={"label": play.label} if play else None, wake=wake_view(ctx), platform=platform,
        german=german_available(ctx.home), ptt_listener=ptt_listening(platform, ctx.home),
        german_list=german_voices(ctx.home), voices=voices_by_language(ctx.home))


# -- voice preview (2026-11-17) ----------------------------------------------------------------
# A tester asked to hear a voice before choosing it. One fixed sentence per language, made by
# the first local TTS server that lists the voice (same discovery as voices_by_language), played
# by a child process. Never while a turn is on, never over a session's audio: refused before,
# checked again once the sample is made, and cut by guard_preview when a turn starts.

PREVIEW_SAMPLES = {
    "English": "Hello, this is how I sound.",
    "German": "Hallo, so klingt meine Stimme.",
    "Spanish": "Hola, así suena mi voz.",
    "French": "Bonjour, voici ma voix.",
    "Italian": "Ciao, questa è la mia voce.",
    "Portuguese": "Olá, esta é a minha voz.",
    "Dutch": "Hallo, zo klinkt mijn stem.",
    "Polish": "Cześć, tak brzmi mój głos.",
    "Russian": "Привет, так звучит мой голос.",
    "Japanese": "こんにちは、これが私の声です。",
    "Chinese": "你好，这是我的声音。",
    "Hindi": "नमस्ते, यह मेरी आवाज़ है।",
    "Korean": "안녕하세요, 이것이 제 목소리입니다.",
}
PREVIEW_TTS_TIMEOUT_S = 30.0     # a loaded local Kokoro can be slower than real time
PREVIEW_PLAY_TIMEOUT_S = 30.0    # one sentence: a player still running by then is stuck
PREVIEW_MAX_BYTES = 8 << 20
TURN_PHASES = ("speaking", "listening", "holding", "paused")


def preview_text(language) -> str:
    """The sample sentence for a language; English when there is none for it."""
    return PREVIEW_SAMPLES.get(language) or PREVIEW_SAMPLES["English"]


def preview_owner(voice, home=None):
    """(base URL, language) of the first local TTS server that lists the voice, else (None, None)."""
    for base in _local_base_urls(st.setting("VOICEMODE_TTS_BASE_URLS", "", home) or ""):
        try:
            pairs = _voice_pairs(_fetch_voices(base + "/audio/voices"))
        except Exception:  # noqa: BLE001 - a server that is down or odd is skipped
            continue
        for v, lang in pairs:
            if v == voice:
                return base, lang
    return None, None


def build_tts_request(base, voice, text):
    body = json.dumps({"model": "tts-1", "input": text, "voice": voice,
                       "response_format": "wav"}).encode("utf-8")
    return urllib.request.Request(base.rstrip("/") + "/audio/speech", data=body, method="POST",
                                  headers={"Content-Type": "application/json"})


def _synthesize(req) -> bytes:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # local: no proxy
    with opener.open(req, timeout=PREVIEW_TTS_TIMEOUT_S) as resp:
        return resp.read(PREVIEW_MAX_BYTES)


def _player_unavailable():
    """Why no sample can be played from here, or None. Looked up, never imported."""
    if getattr(sys, "frozen", False):
        return "this backend has no Python to play audio with"
    for mod in ("numpy", "sounddevice"):
        if importlib.util.find_spec(mod) is None:
            return f"no audio output module ({mod})"
    return None


def _start_player():
    """A child that plays the WAV it reads on stdin (main --play-wav)."""
    return subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--play-wav"],
                            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def play_wav_bytes(data) -> int:
    """Play a 16-bit WAV with sounddevice (voice-mode's own output path). Runs in the child."""
    import io
    import wave
    try:  # parse first: bad input never reaches the audio stack
        with wave.open(io.BytesIO(data), "rb") as w:
            if w.getsampwidth() != 2:
                print("voice_pill: preview is not 16-bit audio", file=sys.stderr)
                return 2
            rate, channels = w.getframerate(), w.getnchannels()
            frames = w.readframes(w.getnframes())
    except (wave.Error, EOFError) as e:
        print(f"voice_pill: preview is not a WAV: {e}", file=sys.stderr)
        return 2
    import numpy as np
    import sounddevice as sd
    sd.play(np.frombuffer(frames, dtype="<i2").reshape(-1, channels), rate)
    sd.wait()
    return 0


def _turn_active(ctx) -> bool:
    """A voice turn is on: the server says so, the pill is paused, or a session holds the floor."""
    status = st._status(ctx.sock)
    if any(status.get(k) for k in ("speaking", "listening", "holding")):
        return True
    if st.is_paused(ctx.base):
        return True
    return read_conch(ctx.home).get("holder") is not None


def _preview_reply(state, **kw):
    return {"window": {"preview": state, **kw}}


BUSY_TURN = {"preview": "busy", "reason": "turn"}


def _preview_worker(ctx, voice):
    try:
        base, lang = preview_owner(voice, ctx.home)
        if base is None:
            ctx.replies.put(_preview_reply("error", voice=voice,
                                           detail=f"unknown voice: no local TTS server lists {voice}"))
            return
        wav = _synthesize(build_tts_request(base, voice, preview_text(lang or language_from_id(voice))))
        with ctx.preview_lock:
            if _turn_active(ctx):   # a turn began while the sample was made
                ctx.replies.put({"window": dict(BUSY_TURN)})
                return
            proc = ctx.preview_proc = _start_player()
            ctx.preview_cut = False
        ctx.replies.put(_preview_reply("playing", voice=voice))
        try:
            proc.communicate(wav, timeout=PREVIEW_PLAY_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            ctx.replies.put(_preview_reply("error", voice=voice, detail="the player did not finish"))
            return
        if ctx.preview_cut:
            ctx.replies.put(_preview_reply("stopped", voice=voice, detail="a voice turn started"))
        elif proc.returncode == 0:
            ctx.replies.put(_preview_reply("done", voice=voice))
        else:
            ctx.replies.put(_preview_reply("error", voice=voice,
                                           detail=f"the player exited with {proc.returncode}"))
    except Exception as e:  # noqa: BLE001 - every preview ends with an answer
        print(f"voice_pill: preview {voice} failed: {e}", file=sys.stderr, flush=True)
        ctx.replies.put(_preview_reply("error", voice=voice, detail=str(e)[:200]))
    finally:
        with ctx.preview_lock:
            ctx.preview_proc = None
            ctx.preview_running = False


def _preview(ctx, voice):
    if not isinstance(voice, str) or not voice.strip():
        return _preview_reply("error", detail="no voice given")
    voice = voice.strip()
    why = _player_unavailable()
    if why:
        return _preview_reply("unavailable", detail=why)
    if _turn_active(ctx):
        return {"window": dict(BUSY_TURN)}
    with ctx.preview_lock:
        if ctx.preview_running:
            return _preview_reply("busy", reason="preview")
        ctx.preview_running = True
        # "loading" goes into the queue BEFORE the worker starts, so the worker's answers (an
        # instant error included) always come after it; returning it instead let the caller
        # queue it after a terminal answer, and the page waited 45 s on a stale "loading".
        ctx.replies.put(_preview_reply("loading", voice=voice))
        ctx.preview_thread = threading.Thread(target=_preview_worker, args=(ctx, voice), daemon=True,
                                              name="voice-pill-preview")
        ctx.preview_thread.start()
    return None


def stop_preview(ctx) -> None:
    """Stop a playing sample now (a turn began, or the backend is exiting)."""
    with ctx.preview_lock:
        proc = ctx.preview_proc
        if proc is not None and proc.poll() is None:
            ctx.preview_cut = True
            try:
                proc.kill()
            except OSError:
                pass


def guard_preview(ctx, snap) -> None:
    """Never over a session's audio: a snapshot showing a turn, or a session talking, cuts it."""
    if ctx.preview_proc is None:
        return
    if snap.get("phase") in TURN_PHASES or any(r.get("state") == "talking" for r in snap.get("sessions") or []):
        stop_preview(ctx)


# -- commands ----------------------------------------------------------------

def _set_override(ctx, floor, **change):
    cur = st.get_override(floor["pid"], ctx.base)
    cur.update(change)
    st.set_override(floor["pid"], cur.get("voice"), cur.get("language"), ctx.base)


def _toggle_barge(action, switch):
    print(f"voice barge {action}: {st.barge_control(action, switch)}", file=sys.stderr, flush=True)


def _barge_worker(ctx, applied=None):
    """Apply what is wanted until what was last applied is still what is wanted: a switch
    sent while an earlier one runs is applied after it, never dropped."""
    while True:
        with ctx.barge_lock:
            wanted = ctx.barge_wanted
            if wanted == applied:
                ctx.barge_running = False  # under the lock: a later wish starts a new worker
                return
        try:
            _toggle_barge(wanted, ctx.switch)
        except Exception as e:  # noqa: BLE001 - the worker must end cleanly, or no switch runs again
            print(f"voice barge {wanted} failed: {e}", file=sys.stderr, flush=True)
        applied = wanted


def _set_barge(ctx, on: bool) -> None:
    with ctx.barge_lock:
        ctx.barge_wanted = "on" if on else "off"
        if not ctx.barge_running:
            # `on` can download a model: never block the snapshot loop on it.
            ctx.barge_running = True
            ctx.barge_thread = threading.Thread(target=_barge_worker, args=(ctx,), daemon=True)
            ctx.barge_thread.start()


def handle_command(msg, ctx=None):
    """Apply one command. Anything malformed or unknown is ignored. Returns a line for the
    page ({"window": ...}) when the command has an answer, else None."""
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
        st.set_pill_closed(True, ctx.base)  # sticks until voice restarts from scratch
    elif cmd == "set_barge" and isinstance(msg.get("on"), bool):
        _set_barge(ctx, msg["on"])
    elif cmd == "set_beeps" and isinstance(msg.get("on"), bool):
        st.set_beeps(msg["on"], ctx.base)
    elif cmd in ("set_voice", "set_language"):
        key = "voice" if cmd == "set_voice" else "language"
        sessions, conch = st.live_sessions(ctx.base), read_conch(ctx.home)
        floor, is_holder = _floor(sessions, conch, st._read_json(ctx.base / "last_floor.json"))
        if key == "language" and msg.get(key) == "German" and not german_available(ctx.home):
            return  # no German voice can speak here: the session would lose its TTS
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
    elif cmd == "preview_voice":
        return _preview(ctx, msg.get("voice"))
    return None


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


def _write_replies(ctx, out) -> None:
    while True:
        try:
            line = ctx.replies.get_nowait()
        except queue.Empty:
            return
        out.write(json.dumps(line) + "\n")
        out.flush()


def _emit(ctx, out) -> float:
    snap = gather(ctx)
    if ctx is not None:
        guard_preview(ctx, snap)
        _write_replies(ctx, out)
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


def _quit(ctx, out) -> int:
    """No voice session left: close the window for good and lift a user's close with it."""
    st.set_pill_closed(False, ctx.base)
    out.write(json.dumps({"quit": True}) + "\n")
    out.flush()
    return EXIT_DONE


def run_stdio(ctx, stdin_fd=0, out=None):
    """Returns None when stdin closes (the window is gone), EXIT_DONE when the last
    voice session has left."""
    out = out or sys.stdout
    source = _input_source(stdin_fd)
    started = time.monotonic()
    buf, interval = b"", _emit(ctx, out)
    last_alive = started
    next_at = time.monotonic() + interval
    while True:
        now = time.monotonic()
        if ctx.alive:
            last_alive = now
        elif now - started >= START_GRACE_S and now - last_alive >= QUIT_GRACE_S:
            return _quit(ctx, out)
        chunk = source.wait(max(0.0, next_at - time.monotonic()))
        sent = False
        if chunk is not None:
            if not chunk:
                return None
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    reply = handle_command(json.loads(line), ctx)
                    if reply:
                        ctx.replies.put(reply)
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
    if argv and argv[0] == "--play-wav":   # the preview's child: play stdin, then exit
        return play_wav_bytes(sys.stdin.buffer.read())
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
    alive_file = ctx.base / "backend.json"  # pill_autostart: a window is already up
    st._write_json(alive_file, {"pid": os.getpid(), **st._identity(os.getpid())})  # start_unknown if ps failed
    code = 0
    try:
        code = run_stdio(ctx) or 0
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    except OSError as e:
        # Windows reports a write to a closed pipe as EINVAL, not EPIPE.
        if not (st._on_windows() and e.errno == errno.EINVAL):
            raise
    finally:
        stop_preview(ctx)
        _release_pause(ctx)
        st._unlink(alive_file)
        release_lock(lock)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
