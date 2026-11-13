#!/usr/bin/env python3
"""voicemode-local on native Windows: the MCP command, its speech services, and their runtime.

Provenance: the runtime, service and lease logic is copied from FELIX,
felix-framework plugins/voicemode/scripts/voice.py (and the active.json lookup of
scripts/launch.js) at commit aea841ffd, branch feat/voice-opt-in: setup with a
hash-locked uv sync, atomic runtime selection, the pinned whisper.cpp zip, model hard
links, connector_options, process_tree and kill_tree, leases and the watchdog,
port_free. Copied rather than imported, and changed for a personal install: its own
folder and ports, the user's home and settings, voicemode-local's patches/apply.py,
optional pill and push-to-talk services, and Job Objects (new here). Since Task 8 of the
shared-supervisor plan the lifecycle (leases, ports, starting, watching, stopping, Job
Objects, the Windows voice host) is vml_supervisor's, beside scripts/ in this checkout, and
the values are this install's profile, vml_profiles/claude.py. This file builds the profile,
decides the settings, words the package's Results, and keeps setup, the switches and the pill.

Commands
  setup   install a runtime into <root>\\runtime\\<digest>-<random>, select it in active.json
  launch  the MCP command: leased speech services, the pill and push-to-talk listener when
          enabled, then voice-mode on this process's stdin and stdout
  serve   the leased speech services only, until stdin closes (diagnosis and tests)
  status  what is installed and running; starts nothing
  stop    stop this install's voice host, services and helpers
  watch   internal (POSIX): stops the services once no connector is left, then exits
  host    internal (Windows): `host <profile file>`, the voice host vml_supervisor starts
          outside every chat; it owns the services and helpers until no connector is left
  barge on|off|status, pill on|off|status, ptt on|off|status
          the native switch: writes VOICEMODE_VOICE_BARGE / VOICEMODE_PILL /
          VOICEMODE_PTT_ENABLED to voicemode.env and, while a connector runs, starts or stops
          the trigger, the pill window or the push-to-talk listener at once
  info    internal: one JSON line for install.ps1 (root, runtime, a stable Python for the
          MCP entry); starts nothing

<root> is VOICEMODE_NATIVE_ROOT, else %LOCALAPPDATA%\\voicemode-local-native (not the
WSL pill's %LOCALAPPDATA%\\voicemode-local), never inside FELIX's folders, and inside the
user's profile or %LOCALAPPDATA% unless --allow-root-outside-profile is given: the
runtime runs code from it, and a folder elsewhere may be writable by other accounts.
uv's Python and cache live under it too. Settings come from the process environment first, then voicemode.env,
through patches/voice_pill_state.setting (the shared resolver).

Speech services listen on 127.0.0.1 only, on the first free port of 28880-28899 (TTS)
and 22022-22041 (STT), or a port set in VOICEMODE_NATIVE_TTS_PORT / _STT_PORT. Never
8880, 8881, 8882, 2022 or 9000 (the WSL and Docker setup), nor FELIX's 18880-18899
and 12022-12041.

Lifetime: a connector holds a lease while it runs. On Windows the services, the trigger and
the listener are children of one voice host per install, started through Windows' process
service so that it belongs to no chat's job; they live in one named Job Object the host holds,
and the host ends them all once no lease is left. It restarts a crashed TTS or STT service on
its own port, at most 3 times in 300 seconds, and every connector starts the host again when it
ended anyway. The trigger and the listener follow their modes: launch writes to run/modes.json
the ones its settings turn on (never off: connectors are a union), a switch writes its own, and the host's next look applies
them. Elsewhere (the tests) the connector starts them, with a watchdog. voice-mode lives in its
connector's own Job Object, so it dies with the connector; the pill window too is started by
voice-mode (or a switch), never by launch. Never os.kill(pid, 0): on Windows that ends the
process instead of checking it.

Reading other processes needs psutil, which only the runtime has: main() runs itself
again with the runtime's Python when the Python it started under lacks it.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import json
import ntpath
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import zipfile
from urllib.error import URLError
from urllib.request import urlopen

WINDOWS = sys.platform == "win32"
SCRIPT = Path(__file__).resolve()
HERE = SCRIPT.parent
REPO = HERE.parents[1]
DEPENDENCIES = HERE / "dependencies.json"
PATCHES = REPO / "patches"
TTS_SERVER = REPO / "kokoro-onnx-server.py"

# The lifecycle lives in vml_supervisor and the values in vml_profiles/claude.py, both in this
# checkout beside scripts/: the checkout's root goes on the import path (Task 8 ruling).
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from vml_profiles import claude as profile_values  # noqa: E402
from vml_supervisor import helpers as sv_helpers  # noqa: E402
from vml_supervisor import host as sv_host  # noqa: E402
from vml_supervisor import procs, winjob  # noqa: E402
from vml_supervisor import services as sv_services  # noqa: E402
from vml_supervisor import state as sv_state  # noqa: E402
from vml_supervisor.profile import BASE_RESERVED_PORTS  # noqa: E402
from vml_supervisor.state import atomic_json, exclusive  # noqa: E402,F401  (setup's lock and records)

LOOPBACK = "127.0.0.1"
LOCAL_HOSTS = "127.0.0.1,localhost,::1"
PROXY_VARIABLES = ("http_proxy", "https_proxy", "all_proxy", "ftp_proxy")
PORT_SETTINGS = {"tts": "VOICEMODE_NATIVE_TTS_PORT", "stt": "VOICEMODE_NATIVE_STT_PORT"}
SPEECH = ("tts", "stt")
OPTIONAL = ("pill", "ptt", "barge")
OPTIONAL_SETTINGS = {"pill": "VOICEMODE_PILL", "ptt": "VOICEMODE_PTT_ENABLED",
                     "barge": "VOICEMODE_VOICE_BARGE"}
SILERO = "models/silero/silero_vad.onnx"
# The native switch: the setting each command writes, and the word for its process.
SWITCHES = {"barge": "VOICEMODE_VOICE_BARGE", "pill": "VOICEMODE_PILL", "ptt": "VOICEMODE_PTT_ENABLED"}
SWITCH_NOUNS = {"barge": "trigger", "pill": "window", "ptt": "listener"}
SWITCH_TITLES = {"barge": "Interrupt by voice", "pill": "The voice pill", "ptt": "Push-to-talk"}
# Which switch settings the launcher's own environment held: voice-mode copies voicemode.env
# into its environment at start, so under a connector only these are real overrides.
ENV_SETTINGS = "VOICEMODE_NATIVE_ENV_SETTINGS"
BARGE_COMMAND_SETTING = "VOICEMODE_BARGE_CONTROL_COMMAND"
STOP_WAIT = 5  # seconds a pill window gets to close on its stop event

LAYOUT = 1
RUNTIMES = "runtime"
REQUIRED_FILES = (
    ".venv/Scripts/python.exe" if WINDOWS else ".venv/bin/python",
    ".venv/Scripts/voice-mode.exe" if WINDOWS else ".venv/bin/voice-mode",
    "services/kokoro-onnx-server.py",
    "dependencies.json",
    *(("services/whisper/whisper-server.exe",) if WINDOWS else ()),
)
ALLOW_OUTSIDE = False  # --allow-root-outside-profile
# Kokoro synthesises one short text into a buffer; nothing is played (FELIX's setup check).
SYNTH_CHECK = (
    "import sys\n"
    "from kokoro_onnx import Kokoro\n"
    "samples, rate = Kokoro(sys.argv[1], sys.argv[2]).create('Voice setup check.', voice=sys.argv[3], "
    "lang='en-us')\n"
    "if len(samples) == 0 or rate <= 0:\n"
    "    sys.exit('Kokoro produced no audio')\n"
    "print(f'Kokoro synthesised {len(samples)} samples at {rate} Hz into memory; no sound was output')\n"
)
STOP_TIMEOUT = 5
MAX_ESPEAK_PATH = 160
# Windows' 260-character path limit; FELIX measured its longest runtime file at 125
# characters below the runtime folder (same lock, 2026-11-04).
MAX_WINDOWS_PATH = 259
LONGEST_RUNTIME_FILE = 125
DEFAULT_VOICE = "af_sky"
PACKAGE_INDEX = "https://pypi.org/simple/"
EXIT_OFF_NETWORK = 69
STOP_SIGNALS = {getattr(signal, name) for name in ("SIGTERM", "SIGINT", "SIGHUP") if hasattr(signal, name)}


class OffNetwork(RuntimeError):
    """A server that setup downloads from could not be reached."""


class VoiceError(RuntimeError):
    """A failure with the plain next step to take."""

    def __init__(self, message, next_step=None):
        super().__init__(message)
        self.next_step = next_step


def setup_step():
    return f'Run: "{sys.executable}" "{SCRIPT}" setup -- then restart the voice connector.'


# -- settings ----------------------------------------------------------------

_resolver = None


def resolver():
    """patches/voice_pill_state.py, the shared resolver (environment, then voicemode.env)."""
    global _resolver
    if _resolver is None:
        spec = importlib.util.spec_from_file_location("vml_voice_pill_state", PATCHES / "voice_pill_state.py")
        _resolver = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_resolver)
    return _resolver


def setting(name, default=None):
    value = resolver().setting(name, default)
    return default if value in (None, "") else value


def truthy(value):
    """A setting is on when it says true, as voice_pill_state.barge_state reads it: spaces and
    quotes around it dropped, any case."""
    return (value or "").strip().strip("\"'").strip().lower() == "true"


def is_auto(value):
    """VOICEMODE_VOICE_BARGE=auto: the trigger runs and listens only on headphones."""
    return (value or "").strip().strip("\"'").strip().lower() == "auto"


def optional_services():
    """The pill, the push-to-talk listener and the voice barge-in trigger, each only when its
    setting is exactly true. The listener runs in native mode (optional_command): its sender
    writes to this install's control pipe, never to the WSL relay."""
    return [kind for kind in OPTIONAL if truthy(setting(OPTIONAL_SETTINGS[kind], ""))
            or (kind == "barge" and is_auto(setting(OPTIONAL_SETTINGS[kind], "")))]


def felix_roots():
    felix = Path(os.environ.get("FELIX_HOME") or Path.home() / ".felix")
    roots = {felix, Path.home() / ".felix"}
    if os.environ.get("FELIX_VOICE_ROOT"):
        roots.add(Path(os.environ["FELIX_VOICE_ROOT"]))
    return {Path(os.path.expanduser(str(r))).resolve() for r in roots}


def profile_folders():
    """The folders only this user should be able to write: the profile, and LOCALAPPDATA."""
    folders = [Path.home()]
    for name in ("USERPROFILE", "LOCALAPPDATA"):
        if os.environ.get(name):
            folders.append(Path(os.environ[name]))
    return [f.resolve() for f in folders]


def _inside(path, folder):
    child, parent = os.path.normcase(str(path)), os.path.normcase(str(folder)).rstrip("\\/")
    return child == parent or child.startswith(parent + os.sep)


def native_root(allow_outside=None):
    allow_outside = ALLOW_OUTSIDE if allow_outside is None else allow_outside
    raw = setting("VOICEMODE_NATIVE_ROOT")
    if raw:
        root = Path(os.path.expanduser(os.path.expandvars(raw)))
    else:
        root = default_root()
    root = root.resolve()
    for felix in felix_roots():
        if root == felix or felix in root.parents:
            raise VoiceError(f"{root} is inside FELIX's folder {felix}; voicemode-local never writes there.",
                             "Set VOICEMODE_NATIVE_ROOT to a folder of its own.")
    for folder in profile_folders():
        if _inside(folder, root):
            raise VoiceError(f"VOICEMODE_NATIVE_ROOT={root} is your profile or LOCALAPPDATA, or holds "
                             f"your {folder}; the install folder must be a folder of its own.",
                             "Choose a folder inside your profile, such as the default.")
    if not allow_outside and not any(_inside(root, folder) for folder in profile_folders()):
        raise VoiceError(f"VOICEMODE_NATIVE_ROOT={root} is outside your profile and LOCALAPPDATA "
                         f"({', '.join(map(str, profile_folders()))}). The runtime runs code from that "
                         "folder, and other accounts may be able to write there.",
                         "Choose a folder inside your profile, or pass --allow-root-outside-profile.")
    return root


# -- small helpers (FELIX's) ---------------------------------------------------

def venv_program(runtime, name):
    return runtime / (f".venv/Scripts/{name}.exe" if WINDOWS else f".venv/bin/{name}")


def site_packages(venv, data):
    return venv / ("Lib/site-packages" if WINDOWS else f"lib/python{data['python']}/site-packages")


def whisper_program(runtime):
    return runtime / "services/whisper/whisper-server.exe"


def on_path(name):
    """A program's full path from PATH's own folders. On Windows, shutil.which and CreateProcess
    also look in the current folder, which may hold anything."""
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        for candidate in ((f"{name}.exe",) if WINDOWS else (name,)):
            full = Path(folder.strip('"')) / candidate
            if folder and full.is_file() and os.access(full, os.X_OK):
                return str(full)
    return None


def url(port):
    return f"http://{LOOPBACK}:{port}/v1"


def runtime_inputs():
    """Every file whose bytes go into a runtime; a change to any of them means a new runtime."""
    patches = json.loads((PATCHES / "manifest.windows.json").read_text(encoding="utf-8"))
    steps = [PATCHES / (step.get("patch") or step.get("copy")) for step in patches["steps"]]
    return [DEPENDENCIES, HERE / "requirements.lock", TTS_SERVER,
            PATCHES / "apply.py", PATCHES / "manifest.windows.json", *steps]


def manifest():
    """The dependencies and a digest of every runtime input."""
    data = json.loads(DEPENDENCIES.read_text(encoding="utf-8"))
    value = hashlib.sha256()
    for path in runtime_inputs():
        value.update(path.relative_to(REPO).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return data, value.hexdigest()


def run(args, **kwargs):
    print("+ " + " ".join(map(str, args)), file=sys.stderr, flush=True)
    if WINDOWS:
        kwargs.setdefault("creationflags", subprocess.CREATE_NO_WINDOW)
    return subprocess.run(list(map(str, args)), check=True, stdout=sys.stderr, **kwargs)


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def download(item, target):
    """Fetch a pinned file, checking size and SHA-256; a file already there that matches is kept."""
    if target.is_file() and target.stat().st_size == item["size"] and digest(target) == item["sha256"]:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".download-", dir=target.parent)
    temporary = Path(name)
    try:
        print(f"Downloading {item['path']} ({item['size']:,} bytes)", file=sys.stderr, flush=True)
        try:
            with os.fdopen(fd, "wb") as output, urlopen(item["url"], timeout=60) as source:
                size = 0
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    size += len(chunk)
                    if size > item["size"]:
                        raise RuntimeError(f"Download exceeds its expected size: {item['path']}")
                    output.write(chunk)
        except (URLError, TimeoutError, ConnectionError) as error:
            raise OffNetwork(f"Could not download {item['url']}: {error}") from error
        if temporary.stat().st_size != item["size"] or digest(temporary) != item["sha256"]:
            raise RuntimeError(f"Download failed verification: {item['path']}; previous file kept")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def package_index_reachable():
    try:
        with urlopen(PACKAGE_INDEX, timeout=15):
            return True
    except (URLError, TimeoutError, ConnectionError):
        return False


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".record-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
        for attempt in range(20):  # a reader's open handle blocks a replace on Windows
            try:
                temporary.replace(path)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.05)
    finally:
        temporary.unlink(missing_ok=True)


def settings_lock(path):
    """voicemode.env.lock beside the file: two switches never lose each other's change."""
    path = Path(path)
    return exclusive(path.with_name(path.name + ".lock"))


def write_setting(path, name, value):
    """Set name=value in a voicemode.env: every assignment of name is rewritten in place
    (``export`` and indentation kept), else one line is appended. Other lines, comments, a
    BOM and the file's line endings stay; a new file gets CRLF on Windows. Atomic replace,
    under settings_lock."""
    with settings_lock(path):
        _write_setting(Path(path), name, value)


def _write_setting(path, name, value):
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raw = b""
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw[3 if bom else 0:].decode("utf-8", errors="surrogateescape")
    newline = "\r\n" if "\r\n" in text else "\n" if "\n" in text else "\r\n" if WINDOWS else "\n"
    out, found = [], False
    for line in re.findall(r"[^\r\n]*(?:\r\n|\n|\r)|[^\r\n]+$", text):
        body = line.rstrip("\r\n")
        stripped = body.strip()
        key = stripped.split("=", 1)[0].strip() if "=" in stripped and not stripped.startswith("#") else ""
        export = key.startswith("export ")
        if export:
            key = key[len("export "):].strip()
        if key == name:
            indent = body[:len(body) - len(body.lstrip())]
            out.append(f"{indent}{'export ' if export else ''}{name}={value}{line[len(body):]}")
            found = True
        else:
            out.append(line)
    if not found:
        if out and not out[-1].endswith(("\n", "\r")):
            out[-1] += newline
        out.append(f"{name}={value}{newline}")
    atomic_write(path, (b"\xef\xbb\xbf" if bom else b"") + "".join(out).encode("utf-8", errors="surrogateescape"))


def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return default


def runtime_environment(runtime, ports=None):
    """The user's environment, minus proxies, pointed at this install's own services, with
    the native switch as the barge control command (the voice_barge tool and the pill's
    toggle run it)."""
    ports = ports or {"tts": profile_values.TTS_PORTS[0], "stt": profile_values.STT_PORTS[0]}
    env = {key: value for key, value in os.environ.items()
           if key.lower() not in PROXY_VARIABLES and key.lower() != "no_proxy"
           and key not in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE")}
    env.update(
        VOICEMODE_TTS_BASE_URLS=url(ports["tts"]),
        VOICEMODE_STT_BASE_URLS=url(ports["stt"]),
        # whisper-server runs without --convert, which needs ffmpeg: it takes WAV.
        VOICEMODE_STT_AUDIO_FORMAT="wav",
        VOICEMODE_VOICES=setting("VOICEMODE_VOICES", DEFAULT_VOICE),
        # Set and empty, so no voicemode.env can forward the connector to a remote server.
        VOICEMODE_MCP_URL="",
        FASTMCP_CHECK_FOR_UPDATES="off",
        PYTHONUTF8="1",
        PYTHONNOUSERSITE="1",
        NO_PROXY=LOCAL_HOSTS,
        no_proxy=LOCAL_HOSTS,
    )
    env["PATH"] = os.pathsep.join([str(venv_program(runtime, "python").parent), env.get("PATH", "")])
    python = str(venv_program(runtime, "python"))  # has psutil: no restart needed
    flag = ["--allow-root-outside-profile"] if ALLOW_OUTSIDE else []
    env[BARGE_COMMAND_SETTING] = json.dumps([python, str(SCRIPT), *flag, "barge"])
    env["VOICEMODE_BARGE_PYTHON"] = python
    env["VOICEMODE_NATIVE_ROOT"] = str(Path(runtime).parent.parent)
    if ENV_SETTINGS not in env:
        env[ENV_SETTINGS] = ",".join(name for name in SWITCHES.values() if name in os.environ)
    return env


def configured_runtime(root, expected):
    """(runtime, current) from active.json. An older complete runtime still starts, but is not current."""
    record = read_json(root / "active.json")
    if not isinstance(record, dict):
        raise VoiceError("voicemode-local is not set up natively on this machine yet.", setup_step())
    holders = (root / RUNTIMES).resolve()
    runtime = (holders / str(record.get("runtime", ""))).resolve()
    if runtime.parent != holders:
        raise VoiceError("Invalid runtime record in active.json; nothing was started.", setup_step())
    complete = read_json(runtime / ".complete.json")
    if not isinstance(complete, dict) or complete.get("manifest") != record.get("manifest"):
        raise VoiceError("The native runtime is incomplete.", setup_step())
    if complete.get("layout") != LAYOUT:
        raise VoiceError("The native runtime is from a layout this launcher cannot start.", setup_step())
    for name in REQUIRED_FILES:
        if not (runtime / name).is_file():
            raise VoiceError(f"The native runtime is missing {name}.", setup_step())
    return runtime, record.get("manifest") == expected


def check_models(runtime):
    for item in json.loads((runtime / "dependencies.json").read_text(encoding="utf-8"))["models"]:
        path = runtime / item["path"]
        if not path.is_file() or path.stat().st_size != item["size"]:
            raise VoiceError(f"Voice model missing or truncated: {path}.", setup_step())


# -- setup -------------------------------------------------------------------

def check_machine():
    if not WINDOWS:
        raise VoiceError("voicemode_native.py sets up native Windows only; elsewhere use install.sh.")
    if platform.machine() != "AMD64":
        raise VoiceError("Native setup needs 64-bit Windows on an Intel or AMD processor.")


def install_whisper(data, root, runtime):
    """whisper-server and the DLLs beside it, from the pinned, hash-checked zip."""
    spec = data["whisper"]
    archive = root / "downloads" / spec["sha256"] / spec["path"]
    download(spec, archive)
    target = whisper_program(runtime).parent
    target.mkdir(parents=True)
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.namelist():
            name = Path(member.replace("\\", "/")).name  # a bare file name, so nothing lands outside
            if name == "whisper-server.exe" or name.lower().endswith(".dll"):
                (target / name).write_bytes(bundle.read(member))
    if not whisper_program(runtime).is_file():
        raise RuntimeError(f"{spec['path']} has no whisper-server.exe")


def link_model(destination, source):
    """A hard link: no special right needed on Windows (a symbolic link needs one), no extra disk."""
    if WINDOWS:
        os.link(source, destination)
    else:
        destination.symlink_to(source)


def model_source(root, item):
    return root / "downloads" / item["sha256"] / Path(item["path"]).name


def relink_models(runtime, data, root):
    """A model downloaded again is a new file; a hard link would still reach the old one."""
    for item in data["models"]:
        destination = runtime / item["path"]
        source = model_source(root, item)
        if not (destination.is_file() and os.path.samefile(destination, source)):
            destination.unlink(missing_ok=True)
            link_model(destination, source)


def setup(root):
    check_machine()
    data, expected = manifest()
    probe = root / RUNTIMES / (expected[:12] + "-xxxxxxxx")
    phonemes = site_packages(probe / ".venv", data) / "espeakng_loader/espeak-ng-data"
    if len(os.fsencode(phonemes)) >= MAX_ESPEAK_PATH:
        raise VoiceError("The install folder's path is too long for the phoneme library (160-byte limit).",
                         "Set VOICEMODE_NATIVE_ROOT to a shorter folder.")
    if len(str(probe)) + LONGEST_RUNTIME_FILE >= MAX_WINDOWS_PATH:
        raise VoiceError("The install folder's path is too long for Windows' 260-character limit.",
                         "Set VOICEMODE_NATIVE_ROOT to a shorter folder.")
    uv = setting("VOICEMODE_UV") or on_path("uv")
    if not uv:
        raise VoiceError("Missing command: uv.", "Install uv (https://docs.astral.sh/uv/), then retry setup.")
    root.mkdir(parents=True, exist_ok=True)
    uv_cache = root / ".uv-cache"  # private, removed afterwards, also after a failure
    # uv's Python lives under the root too, so the runtime depends on nothing outside it.
    uv_env = {**os.environ, "UV_CACHE_DIR": str(uv_cache), "UV_PYTHON_INSTALL_DIR": str(root / "python")}
    try:
        return _setup_locked(root, data, expected, uv, uv_env)
    finally:
        shutil.rmtree(uv_cache, ignore_errors=True)


def _setup_locked(root, data, expected, uv, uv_env):
    with exclusive(root / ".setup.lock"):
        for item in data["models"]:
            download(item, model_source(root, item))
        if (root / "active.json").exists():
            try:
                runtime, current = configured_runtime(root, expected)
            except (OSError, ValueError, KeyError, RuntimeError) as error:
                print(f"Existing runtime needs repair: {error}", file=sys.stderr)
            else:
                if current:
                    relink_models(runtime, data, root)
                    prune_runtimes(root, runtime.name)
                    print(f"Already installed at {runtime}. Nothing was started.")
                    return runtime
        holders = root / RUNTIMES
        holders.mkdir(exist_ok=True)
        runtime = Path(tempfile.mkdtemp(prefix=expected[:12] + "-", dir=holders))
        try:
            venv = runtime / ".venv"
            run([uv, "venv", "--managed-python", "--python", data["python"], venv], env=uv_env)
            # Every package and hash from the lock, wheels only. Copies, not uv-cache hard links,
            # so patching below changes this runtime only. Compiled now: FELIX measured a 19.8 s
            # first start without it on Windows (2026-11-04).
            try:
                run([uv, "pip", "sync", "--python", venv_program(runtime, "python"), "--require-hashes",
                     "--link-mode", "copy", "--only-binary", ":all:", "--compile-bytecode",
                     HERE / data["lock"]], env=uv_env)
            except subprocess.CalledProcessError as error:
                if not package_index_reachable():
                    raise OffNetwork(f"Could not reach {PACKAGE_INDEX}") from error
                raise
            # The native list: ours plus FELIX's no-ffmpeg and early-scipy patches, as Windows has
            # no ffmpeg (patches/manifest.windows.json).
            run([venv_program(runtime, "python"), PATCHES / "apply.py", "--native-windows",
                 "--package-dir", site_packages(venv, data) / "voice_mode"],
                env={**os.environ, "PYTHONUTF8": "1"})
            install_whisper(data, root, runtime)
            (runtime / "services").mkdir(exist_ok=True)
            shutil.copy2(TTS_SERVER, runtime / "services/kokoro-onnx-server.py")
            shutil.copy2(DEPENDENCIES, runtime / "dependencies.json")
            for item in data["models"]:
                destination = runtime / item["path"]
                destination.parent.mkdir(parents=True, exist_ok=True)
                link_model(destination, model_source(root, item))
            # An import check only: it loads the packages and plays nothing.
            run([venv_program(runtime, "python"), "-c",
                 "import voice_mode, kokoro_onnx, soundfile, sounddevice, psutil, "
                 "voice_mode.voice_pill_state, voice_mode.control_transport"],
                env=runtime_environment(runtime))
            run([venv_program(runtime, "python"), "-c", SYNTH_CHECK, runtime / "models/kokoro/kokoro-v1.0.onnx",
                 runtime / "models/kokoro/voices-v1.0.bin", DEFAULT_VOICE], env=runtime_environment(runtime))
            atomic_json(runtime / ".complete.json", {"manifest": expected, "layout": LAYOUT})
            atomic_json(root / "active.json", {"manifest": expected, "runtime": runtime.name})
        except BaseException:
            shutil.rmtree(runtime, ignore_errors=True)
            raise
    removed = prune_runtimes(root, runtime.name)
    if removed:
        print(f"Removed runtimes no longer in use: {', '.join(removed)}.")
    print(f"Installed at {runtime}. Nothing was started and no audio was played or recorded.")
    return runtime


def prune_runtimes(root, active):
    """Remove every runtime that is neither active, nor named by a live lease, nor serving."""
    holders = root / RUNTIMES
    if not holders.is_dir():
        return []
    profile = profile_of(root, holders / active)
    run_dir = Path(profile.run_dir)
    with sv_state.service_lock(run_dir):
        state = read_json(run_dir / "state.json", {}) or {}
        clients = run_dir / "clients"
        if procs.require_psutil() is not None and (state or (clients.is_dir() and any(clients.iterdir()))):
            # Setup runs under the user's Python: without psutil every lease would look dead and be
            # deleted, and a runtime in use could go. Nothing is pruned; the next setup tries again.
            print("voicemode-local: could not check which runtimes are in use (no psutil here); "
                  "older runtimes were kept.", file=sys.stderr)
            return []
        keep = {active}
        for pid in sv_state.live_leases(profile):
            record = read_json(clients / str(pid), {})
            if isinstance(record, dict) and record.get("runtime"):
                keep.add(Path(record["runtime"]).name)
        for entry in state.values():
            if procs.owned(entry) and entry.get("runtime"):
                keep.add(Path(entry["runtime"]).name)
        removed = []
        for folder in sorted(holders.iterdir()):
            if folder.is_dir() and folder.name not in keep:
                try:
                    shutil.rmtree(folder)
                    removed.append(folder.name)
                except OSError as error:  # a file in use: it is not selected, so the next setup retries
                    print(f"voicemode-local: could not remove {folder} yet: {error}", file=sys.stderr)
        return removed


# -- the optional processes' commands (vml_profiles/claude.py holds them) ---------------

def optional_command(kind, runtime, root=None):
    """(command, marker) of the pill window, the push-to-talk listener or the barge trigger;
    see vml_profiles.claude.optional_command. root is the install; by default the runtime's."""
    return profile_values.optional_command(kind, runtime, root, windows=WINDOWS)


powershell = profile_values.powershell
pill_data_dir = profile_values.pill_data_dir
ptt_log = profile_values.ptt_log


def _long_path(full):
    """full with its 8.3 short names expanded: the longest prefix that exists goes through
    GetLongPathNameW, the rest is kept as written. pill-windows.ps1's Get-LongPath does the same
    after GetFullPath, which on its own expands only some short names (measured 2026-11-06)."""
    import ctypes
    from ctypes import wintypes

    get = ctypes.windll.kernel32.GetLongPathNameW
    get.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    get.restype = wintypes.DWORD
    head, rest = full, []
    while True:
        buf = ctypes.create_unicode_buffer(32768)
        if get(head, buf, len(buf)):
            return ntpath.join(buf.value, *reversed(rest)) if rest else buf.value
        parent, name = ntpath.split(head)
        if not name or parent == head:
            return full
        rest.append(name)
        head = parent


def windows_full_path(path):
    """The full path pill-windows.ps1 names its pill after: absolute against the current
    folder, / as \\, . and .. resolved, trailing dots and spaces of the last part dropped (as
    GetFullPath), and on Windows 8.3 short names expanded (_long_path). Off Windows (the
    Linux tests) a relative path cannot be resolved the same way and is only normalised."""
    text = str(path)
    if os.name == "nt":
        full = ntpath.abspath(text)
        return _long_path(full) if "~" in full else full
    full = ntpath.normpath(text)
    head, name = ntpath.split(full)
    if name and name not in (".", ".."):
        stripped = name.rstrip(". ")
        if stripped:
            full = ntpath.join(head, stripped)
    return full


def pill_names(data_dir):
    """(mutex, stop event) of the native pill window that uses data_dir: one pill per install.
    pill-windows.ps1 derives the same names (tests compare the two): SHA-256 of the full path
    (windows_full_path, i.e. GetFullPath), trailing slashes dropped, lowercased, as UTF-8; the
    first 12 hex digits. So any spelling of one folder gives one name. The WSL pill keeps the
    fixed names without a suffix."""
    key = windows_full_path(data_dir).rstrip("\\/").lower()
    tag = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    return f"Local\\VoiceModePillWindow-{tag}", f"Local\\VoiceModePillStop-{tag}"


def pill_stop_event(root):
    """The event that closes this install's pill window: set it, and the window exits."""
    return pill_names(pill_data_dir(root))[1]


# -- watch and stop --------------------------------------------------------------

def watch(root):
    """POSIX: vml_supervisor's watchdog for this install (Windows has the voice host instead)."""
    return sv_host.watch_main(profile_of(root))


def stop(root):
    """Stop this install's voice host, services and helpers (vml_supervisor's stop), whatever leases remain."""
    results = sv_services.stop(profile_of(root))
    done = next((r for r in results if r.code == "services_stopped"), None)
    say([r for r in results if r is not done])
    data = done.data if done is not None else {}
    print(f"Stopped voicemode-local's native services: {', '.join(data.get('services', [])) or 'none were running'}.")
    if data.get("connectors"):
        print(f"{data['connectors']} voice connector(s) still running will have no speech until they restart.")


# -- the profile over vml_supervisor (Task 8) ---------------------------------------
# The package starts, leases, watches and stops; this launcher builds the profile, decides the
# settings it reads, and words the Results it returns (the package writes no text for people).

# Results that are the expected outcome, not news.
QUIET = frozenset({"no_job", "breakaway", "host_started", "host_started_late", "helper_started",
                   "helper_running", "helper_restarted", "helper_stopped", "helper_not_running",
                   "services_stopped"})


def active_runtime(root):
    """The runtime folder active.json names, else the runtime holder (a profile needs one)."""
    record = read_json(root / "active.json")
    name = Path(str(record.get("runtime", ""))).name if isinstance(record, dict) else ""
    return root / RUNTIMES / name if name else root / RUNTIMES


def connector_environment(runtime):
    """What the services and helpers get: the launcher's own environment for voice-mode."""
    env = runtime_environment(runtime)
    env.update(tool_selection())
    env.update(voice_mode_settings())
    return env


def profile_of(root, runtime=None):
    """This install's HostProfile (vml_profiles/claude.py), for the runtime given or the active one."""
    runtime = Path(runtime) if runtime is not None else active_runtime(root)
    return profile_values.personal_profile(root, runtime, env=connector_environment(runtime),
                                           allow_outside=ALLOW_OUTSIDE, windows=WINDOWS)


def say(results, quiet=QUIET):
    """Every Result that is news, one line each on stderr: nothing the package met goes unsaid."""
    for result in results:
        if result.code not in quiet:
            print(f"voicemode-local: {result.code}: {result.detail}", file=sys.stderr, flush=True)


def port_setting(kind):
    """The port VOICEMODE_NATIVE_TTS_PORT / _STT_PORT sets (environment, then voicemode.env), or None."""
    raw = setting(PORT_SETTINGS[kind])
    if raw is None:
        return None
    try:
        port = int(str(raw).strip())
    except ValueError:
        port = None
    reserved = set(BASE_RESERVED_PORTS) | set(profile_values.FELIX_PORTS)
    if port is None or not 1024 <= port <= 65535 or port in reserved:
        raise VoiceError(f"{PORT_SETTINGS[kind]}={raw!r} is not usable: give a port from 1024 to 65535 "
                         "outside 8880-8882, 2022, 9000 and FELIX's 18880-18899 and 12022-12041.",
                         f"Fix or remove {PORT_SETTINGS[kind]}, then restart the voice connector.")
    return port


def write_port_settings(root):
    """run/ports.json from the port settings, for whoever chooses the ports: on Windows the host,
    which inherits no environment and must not miss a port set in it."""
    ports = {kind: port for kind in SPEECH if (port := port_setting(kind)) is not None}
    sv_state.atomic_json(Path(root) / "run" / profile_values.PORTS_FILE, ports)


def launch_modes():
    """The trigger's and the listener's modes this launch's settings turn ON (the environment
    first, as optional_services reads them). Never "off": connectors are a union, so one whose
    settings do not want a helper must not stop another's. Only a switch writes off."""
    modes = {kind: profile_values.mode_of(kind, setting(name, ""))
             for kind, name in profile_values.MODE_SETTINGS.items()}
    return {kind: mode for kind, mode in modes.items() if mode != "off"}


def failure(results, runtime):
    """The VoiceError for speech services that did not start, from the first Result that says why."""
    windows_step = "Start Claude Code from a normal desktop session, then restart the voice connector."
    for result in results:
        data = result.data if isinstance(result.data, dict) else {}
        kind = str(data.get("service", "")).lower()
        noun = kind.upper()
        if result.code == "runtime_incomplete":
            return VoiceError(f"The runtime {Path(runtime).name} was replaced while starting.",
                              "Restart the voice connector.")
        if result.code == "no_free_port":
            return VoiceError(f"No free local port for the {noun} service (tried {data.get('first')} to "
                              f"{data.get('last')}).",
                              f"Close what uses them, or set {PORT_SETTINGS.get(kind, 'its port setting')}, "
                              "then restart.")
        if result.code in ("owner_other", "owner_unknown"):
            return VoiceError(f"Something other than our {noun} service answered on {LOOPBACK}:{data.get('port')} "
                              f"(ours is process {data.get('pid')}; listening: "
                              f"{data.get('listeners') or 'none found'}); voice was not connected to it.",
                              f"Restart the voice connector, or set {PORT_SETTINGS.get(kind, 'its port setting')}.")
        if result.code == "service_exited":
            return VoiceError(f"The local {noun} service stopped while starting.", f"See {data.get('log')}.")
        if result.code == "service_not_ready":
            return VoiceError(f"Local speech services did not become ready: {result.detail}",
                              f"See {data.get('log')}.")
        if result.code in ("job_assign_failed", "resume_failed"):
            return VoiceError(f"Could not put a speech service in its Job Object ({result.detail}); it was not "
                              "started.", windows_step)
        if result.code.startswith("host_") and result.code not in ("host_started", "host_started_late"):
            return VoiceError(f"The voice host could not be started ({result.code}: {result.detail}).", windows_step)
    told = "; ".join(f"{r.code}: {r.detail}" for r in results if r.code not in QUIET) or "no reason was given"
    return VoiceError(f"Local speech services did not start: {told}.",
                      "If this repeats, run setup again, then restart the voice connector.")


def ensure_speech(profile, helpers=True):
    """Lease this connector and make sure the speech services answer (vml_supervisor); with
    helpers, the trigger and the listener follow their modes too. Returns the services' ports."""
    write_port_settings(profile.root)
    if helpers:
        profile_values.write_modes(profile.run_dir, launch_modes())
        ports, results = sv_host.ensure(profile, os.getpid())
    else:
        ports, results = sv_services.ensure_services(profile, os.getpid())
    say(results)   # all of them, also when one below becomes the error: none goes unsaid
    if any(kind not in ports for kind in SPEECH):
        raise failure(results, profile.runtime_dir)
    return {kind: ports[kind] for kind in SPEECH}


def leave(profile):
    """Drop this connector's lease; the services stop when no connector is left."""
    say(sv_services.release(profile, os.getpid()))


def start_in_job(command, job, **kwargs):
    """winjob.spawn, said: a child that could not be put in its job is not started (VoiceError),
    and one whose parent's job forbids breakaway (it also ends with that job) is said on stderr."""
    try:
        process, spawned = winjob.spawn(command, job, **kwargs)
    except winjob.SpawnError as error:
        raise VoiceError(f"Could not put {Path(command[0]).name} in its Job Object ({error.result.detail}); "
                         "it was not started.",
                         "Start Claude Code from a normal desktop session, then restart the voice "
                         "connector.") from error
    if spawned.code not in ("no_job", "breakaway"):
        print(f"voicemode-local: {Path(command[0]).name}: {spawned.code}: {spawned.detail}",
              file=sys.stderr, flush=True)
    return process


_connector_jobs = []


def connector_job():
    """A private Job Object for this connector's own voice-mode: it dies when this process does.
    With breakaway: what voice-mode starts through the native switch belongs in the services' job."""
    if not WINDOWS:
        return None
    if not _connector_jobs:
        _connector_jobs.append(winjob.Job(breakaway=True))
    return _connector_jobs[0]


def start_pill(profile, runtime, env, state):
    """Start the pill window into the services' job and record it in run\\state.json. The caller
    holds the service lock and has checked that none is running. The pill is not a vml_supervisor
    helper: launch never starts it (voice-mode does, pill_autostart) and it stops through its event."""
    run_dir = Path(profile.run_dir)
    command, marker = optional_command("pill", runtime, profile.root)
    job = None
    try:
        job = winjob.services_job(profile)
    except OSError as error:
        print(f"voicemode-local: job_unavailable: {profile.job_name}: {error}; the window starts without it",
              file=sys.stderr, flush=True)
    Path(profile.working_dir).mkdir(parents=True, exist_ok=True)
    log = run_dir / "pill.log"
    with log.open("a") as output:
        process = start_in_job(command, job, env=env, cwd=str(profile.working_dir), stdin=subprocess.DEVNULL,
                               stdout=output, stderr=output, **procs.background_options())
    state["pill"] = {"pid": process.pid, "port": None, "marker": marker, "log": str(log),
                     "command": [str(c) for c in command], "runtime": str(runtime)}
    try:
        sv_state.atomic_json(run_dir / "state.json", state)
    except BaseException:
        procs.kill_tree(process.pid, STOP_TIMEOUT)
        raise
    return process


# -- the native switch ---------------------------------------------------------

def silero_present(runtime):
    """The barge trigger's model, at its pinned size, in the runtime (setup links it)."""
    path = runtime / SILERO
    try:
        data = json.loads((runtime / "dependencies.json").read_text(encoding="utf-8"))
        size = next((m["size"] for m in data["models"] if m["path"] == SILERO), None)
    except (OSError, ValueError, KeyError):
        size = None
    return path.is_file() and (size is None or path.stat().st_size == size)


def set_pill_stop_event(root):
    """Set the install's pill stop event; False when no window has it open."""
    import ctypes
    from ctypes import wintypes

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenEventW.restype = wintypes.HANDLE
    k.OpenEventW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    k.SetEvent.argtypes = (wintypes.HANDLE,)
    k.CloseHandle.argtypes = (wintypes.HANDLE,)
    event = k.OpenEventW(0x2, False, pill_stop_event(root))  # EVENT_MODIFY_STATE
    if not event:
        return False
    try:
        return bool(k.SetEvent(event))
    finally:
        k.CloseHandle(event)


def can_inspect():
    """Whether this Python can read other processes (on Windows that needs psutil, which only
    the runtime has; main() has already restarted under it when there is one)."""
    return not WINDOWS or importlib.util.find_spec("psutil") is not None


def expose_barge_tool(path):
    """As voicemode-switch barge on: a VOICEMODE_TOOLS_ENABLED whitelist would hide the
    voice_barge tool, so add it to one that exists. Never create a whitelist: without one,
    every tool loads."""
    with settings_lock(path):
        try:
            text = Path(path).read_bytes().decode("utf-8-sig", errors="replace")
        except FileNotFoundError:
            return
        tools = resolver()._parse_env(text).get("VOICEMODE_TOOLS_ENABLED")
        if tools is not None and "voice_barge_tool" not in [t.strip() for t in tools.split(",")]:
            _write_setting(Path(path), "VOICEMODE_TOOLS_ENABLED",
                           ",".join([t for t in tools.split(",") if t.strip()] + ["voice_barge_tool"]))


def env_override(name, value):
    """The environment's value of name when it differs from value and the environment wins.
    Under a connector, only settings the launcher's own environment held count."""
    listed = os.environ.get(ENV_SETTINGS)
    if listed is not None and name not in listed.split(","):
        return None
    current = os.environ.get(name)
    if current is None or current.strip() == value:
        return None
    return current


def stop_optional(root, kind, entry):
    """The pill through its stop event first (the window closes and ends its backend), then
    kill_tree of what is still there."""
    if kind == "pill" and WINDOWS and set_pill_stop_event(root):
        deadline = time.monotonic() + STOP_WAIT
        while procs.owned(entry) and time.monotonic() < deadline:
            time.sleep(0.1)
    if procs.owned(entry):
        failed = procs.kill_tree(entry["pid"], STOP_TIMEOUT)
        if failed is not None:
            raise VoiceError(f"The {SWITCH_NOUNS[kind]} would not stop ({failed.detail}).")


def _serving_runtime(root, state, run_dir, leases):
    """The runtime the running connectors use: the speech services', else a lease's."""
    for kind in SPEECH:
        entry = state.get(kind)
        if procs.owned(entry) and entry.get("runtime"):
            return Path(entry["runtime"])
    for pid in leases:
        record = read_json(run_dir / "clients" / str(pid), {})
        if isinstance(record, dict) and record.get("runtime"):
            return Path(record["runtime"])
    return None


def _say_no_channel(run_dir, leases):
    records = [read_json(run_dir / "clients" / str(pid), {}) for pid in leases]
    if any(isinstance(r, dict) and r.get("control_channel") is False for r in records):
        print("A voice connector started before this has no control channel, so it cannot show who "
              "is speaking or be interrupted by voice: reconnect it (/mcp in Claude Code). New "
              "connectors get the channel.")


def switch(root, kind, action):
    """barge|pill|ptt on|off|status. See the module docstring."""
    if action == "status":
        return switch_status(root, kind)
    # auto (barge only): the trigger runs and listens only while the route is headphones.
    name, on = SWITCHES[kind], action in ("on", "auto")
    value = "auto" if action == "auto" else ("true" if on else "false")
    if on and kind == "barge":
        runtime, _ = configured_runtime(root, manifest()[1])
        if not silero_present(runtime):
            raise VoiceError(f"The Silero model that interrupt by voice needs is missing from {runtime}.",
                             setup_step())
    path = resolver().env_file()
    write_setting(path, name, value)
    if on and kind == "barge":
        expose_barge_tool(path)
    print(f"{SWITCH_TITLES[kind]} is {action}: {name}={value} in {path}.")
    current = env_override(name, value)
    if current is not None:
        print(f"Note: {name}={current} is set in the environment, which wins over voicemode.env: "
              f"a voice connector started with it still treats this as {current}. Remove it there "
              "to make this stick.")
    if not can_inspect():
        print("Nothing is running here to change: voicemode-local is not set up natively.")
        return 0
    if kind in profile_values.MODE_SETTINGS:
        return _switch_helper(root, kind, profile_values.mode_of(kind, value))
    return _switch_pill(root, kind, on, name, value)


def _switch_helper(root, kind, mode):
    """The trigger or the listener: a vml_supervisor helper. The decision goes to run/modes.json
    first, so the host's next look keeps it (read_mode in vml_profiles/claude.py)."""
    run_dir = root / "run"
    noun = SWITCH_NOUNS[kind]
    profile_values.write_modes(run_dir, {kind: mode})
    profile = profile_of(root)
    if mode == "off":
        result = sv_helpers.stop_helper(profile, kind)
        say(result.data.get("results", []))
        if result.code == "helper_stopped":
            print(f"Stopped the {noun} (process {result.data.get('pid')}).")
        elif result.code != "helper_not_running":
            raise VoiceError(f"The {noun} could not be stopped ({result.code}: {result.detail}).")
        return 0
    state = read_json(run_dir / "state.json", {}) or {}
    entry = state.get(kind)
    if procs.owned(entry) and entry.get("mode") == mode:
        print(f"The {noun} is already running (process {entry['pid']}).")
        return 0
    leases = sv_state.live_leases(profile)
    runtime = _serving_runtime(root, state, run_dir, leases) if leases else None
    if runtime is None:
        print(f"No voice connector is running: the {noun} starts with the next voice connector.")
        return 0
    if kind == "barge" and not silero_present(runtime):
        raise VoiceError(f"The Silero model that interrupt by voice needs is missing from {runtime}.",
                         setup_step())
    result = sv_helpers.start_helper(profile_of(root, runtime), kind, mode)
    say(result.data.get("results", []))
    pid = result.data.get("pid")
    if result.code == "helper_running":
        print(f"The {noun} is already running (process {pid}).")
    elif result.code in ("helper_started", "helper_restarted"):
        print(f"The {noun} {result.code.split('_')[1]} (process {pid}).")
    else:
        raise VoiceError(f"The {noun} did not start ({result.code}: {result.detail}).",
                         f"See {result.data.get('log')}." if result.data.get("log") else None)
    _say_no_channel(run_dir, leases)
    return 0


def _switch_pill(root, kind, on, name, value):
    """The pill window: its own path, not a vml_supervisor helper (launch never starts it, and it
    closes through its stop event)."""
    run_dir = root / "run"
    noun = SWITCH_NOUNS[kind]
    with sv_state.service_lock(run_dir):
        state = read_json(run_dir / "state.json", {}) or {}
        entry = state.get(kind)
        if not on:
            if procs.owned(entry):
                stop_optional(root, kind, entry)
                print(f"Stopped the {noun} (process {entry['pid']}).")
            if kind in state:
                state.pop(kind)
                sv_state.atomic_json(run_dir / "state.json", state)
            return 0
        if procs.owned(entry):
            print(f"The {noun} is already running (process {entry['pid']}).")
            return 0
        leases = sv_state.live_leases(profile_of(root))
        runtime = _serving_runtime(root, state, run_dir, leases) if leases else None
        if runtime is None:
            print(f"No voice connector is running: the {noun} starts with the next voice connector.")
            return 0
        state.pop(kind, None)
        ports = {k: state[k]["port"] for k in SPEECH if procs.owned(state.get(k))}
        env = runtime_environment(runtime, ports if len(ports) == len(SPEECH) else None)
        env[name] = value  # the window checks it, and a stale false from voice-mode must not stop it
        process = start_pill(profile_of(root, runtime), runtime, env, state)
        print(f"The {noun} started (process {process.pid}).")
        _say_no_channel(run_dir, leases)
    return 0


def switch_status(root, kind):
    """Line 1: the same contract as `python -m voice_mode.voice_barge status` (on, off,
    cannot run: <reason>) for barge, on or off for the pill; voicemode.env first, as the
    switch writes it; on or off for push-to-talk too. Line 2: whether the trigger, window or
    listener is running."""
    if kind == "barge":
        mode = resolver().barge_mode()
        line = barge_cannot_run() if mode is None else mode
    else:
        line = "on" if truthy(resolver().file_first_setting(SWITCHES[kind], "")) else "off"
    print(line)
    entry = (read_json(root / "run" / "state.json", {}) or {}).get(kind)
    noun = SWITCH_NOUNS[kind]
    running = can_inspect() and procs.owned(entry)
    print(f"{noun}: running (process {entry['pid']})" if running else f"{noun}: not running")
    return 0


def barge_cannot_run():
    """voice_barge.CANNOT_RUN, read from its source: importing it would load numpy."""
    import ast

    tree = ast.parse((PATCHES / "voice_barge.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "CANNOT_RUN":
            return ast.literal_eval(node.value)
    raise RuntimeError("patches/voice_barge.py has no CANNOT_RUN")


class Stopped(Exception):
    pass


@contextlib.contextmanager
def forwarded_signals(get_child, stopping):
    def forward(signum, frame):
        child = get_child()
        if child is None:
            raise Stopped(signum)
        child.send_signal(signum)
        stopping.append(time.monotonic())

    previous = {}
    for signum in STOP_SIGNALS:
        with contextlib.suppress(ValueError, OSError):  # not the main thread
            previous[signum] = signal.signal(signum, forward)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


TOOL_SETTINGS = ("VOICEMODE_TOOLS_ENABLED", "VOICEMODE_TOOLS_DISABLED", "VOICEMODE_TOOLS")
# Upstream 8.12's default when none of TOOL_SETTINGS is set (tools/__init__.py), plus ours.
DEFAULT_TOOLS = "converse,service,voice_barge_tool"


def tool_selection():
    """Upstream loads only converse and service unless a tool setting says otherwise, which
    would hide the voice_barge tool (and voice_stop). With no setting in the environment or
    voicemode.env, voice-mode gets that default plus voice_barge_tool; any setting the user
    made is left alone."""
    if any(setting(name) is not None for name in TOOL_SETTINGS):
        return {}
    return {"VOICEMODE_TOOLS_ENABLED": DEFAULT_TOOLS}


CONTROL_CHANNEL = "VOICEMODE_CONTROL_CHANNEL_ENABLED"
STREAMING = "VOICEMODE_STREAMING_ENABLED"


def _on(name):
    return truthy(setting(name, ""))


def voice_mode_settings():
    """What voice-mode needs for the optional services, unless the user set it (environment
    or voicemode.env): the control channel whenever the pill, barge or push-to-talk is on
    (without it the trigger never sees `speaking`: 0 cuts, then 2 of 2 with it, controller's
    spoken test 2026-11-06), and buffered playback with barge (the trigger is inert while
    playback streams, docs/voice-barge-in/README.md). Natively the channel is the named pipe
    of patch_control_transport.py, which manifest.windows.json applies."""
    extra = {}
    if any(_on(OPTIONAL_SETTINGS[kind]) for kind in OPTIONAL) and setting(CONTROL_CHANNEL) is None:
        extra[CONTROL_CHANNEL] = "true"
    if _on(OPTIONAL_SETTINGS["barge"]) and setting(STREAMING) is None:
        extra[STREAMING] = "false"
    return extra


def launch(root):
    """The MCP command: the leased speech services (and the trigger and the listener when they are
    on), then voice-mode on stdio. On Windows the services belong to the voice host outside every
    chat; every HOST_CHECK seconds this connector starts it again if it ended anyway."""
    data, expected = manifest()
    runtime, current = configured_runtime(root, expected)
    if not current:
        print("voicemode-local: the native runtime is out of date; the previous one is used. "
              f'To update, run: "{sys.executable}" "{SCRIPT}" setup', file=sys.stderr, flush=True)
    check_models(runtime)
    profile = profile_of(root, runtime)
    child = None
    stopping = []
    try:
        with forwarded_signals(lambda: child, stopping):
            ports = ensure_speech(profile)
            env = runtime_environment(runtime, ports)
            # Upstream loads every .voicemode/voicemode.env walking up from its working folder;
            # from a WSL checkout that was the WSL setup's file. The label still gets the folder.
            # A relaunch under the runtime's Python runs in the install folder and passes the
            # client's folder on in VOICEMODE_CLIENT_FOLDER.
            env["VOICEMODE_CLIENT_FOLDER"] = os.environ.get("VOICEMODE_CLIENT_FOLDER") or os.getcwd()
            env.update(tool_selection())
            env.update(voice_mode_settings())
            child = start_in_job([str(venv_program(runtime, "voice-mode"))], connector_job(), env=env,
                                 cwd=str(runtime), **procs.connector_options())
            # If this launcher dies, its voice-mode keeps the lease alive until it ends too.
            sv_state.atomic_json(root / "run" / "clients" / str(os.getpid()),
                                 {"child": child.pid, "runtime": str(runtime),
                                  "control_channel": (env.get(CONTROL_CHANNEL) or "").strip().lower() == "true"})
            checked = time.monotonic()
            while True:
                try:
                    return child.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    if stopping and time.monotonic() - stopping[0] > STOP_TIMEOUT:
                        child.kill()
                    if procs.WINDOWS and time.monotonic() - checked >= sv_host.HOST_CHECK:
                        checked = time.monotonic()
                        say(sv_host.check_host(profile), quiet=())
    except Stopped as stopped:
        return 128 + stopped.args[0]
    finally:
        if child is not None and child.poll() is None:
            if WINDOWS:
                say([r for r in [procs.kill_tree(child.pid, STOP_TIMEOUT)] if r is not None])
            else:
                child.terminate()
                try:
                    child.wait(STOP_TIMEOUT)
                except subprocess.TimeoutExpired:  # it ignored the stop request: never left running
                    child.kill()
                    child.wait()
        leave(profile)


def serve(root):
    """The leased speech services alone, until stdin closes. Prints their ports as one JSON line."""
    data, expected = manifest()
    runtime, _ = configured_runtime(root, expected)
    check_models(runtime)
    profile = profile_of(root, runtime)
    try:
        ports = ensure_speech(profile, helpers=False)
        print(json.dumps(ports), flush=True)
        sys.stdin.read()
        return 0
    finally:
        leave(profile)


def status(root):
    data, expected = manifest()
    runtime, current = configured_runtime(root, expected)
    check_models(runtime)
    state = read_json(root / "run/state.json", {}) or {}
    running = {kind: entry for kind, entry in state.items() if procs.owned(entry)}
    profile = profile_of(root, runtime)
    for kind in SPEECH:
        if kind in running:
            spec = profile.services[kind]
            if not sv_services.check_service(spec.ready_kind, running[kind]["port"], spec.ready_path):
                raise RuntimeError(f"Local {kind.upper()} service unavailable at "
                                   f"http://{LOOPBACK}:{running[kind]['port']}{spec.ready_path}.")
    if running:
        print("Running: " + ", ".join(
            f"{kind} {LOOPBACK}:{e['port']}" if e.get("port") else kind for kind, e in running.items()) + ".")
    else:
        print("Not running; the services start with the voice connector.")
    print(f"Installed at {runtime}.")
    use = disk_use(root)
    print(f"Disk: {use['root']:,} bytes in {root} (runtime {use['runtime']:,}, downloads "
          f"{use['downloads']:,}, Python {use['python']:,}; a hard-linked file counted once).")
    if not current:
        print("The runtime is out of date: run setup.")
        return 1
    return 0


def protected_folders():
    """(folder, also_inside): never deleted, nor any folder that holds one; with also_inside,
    nothing inside it either. The WSL pill's folder and FELIX's are never ours; the profile and
    LOCALAPPDATA hold everything else."""
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    found = [(local / "voicemode-local", True), *((f, True) for f in felix_roots())]
    found += [(f, False) for f in profile_folders()]
    return [(Path(os.path.abspath(f)), inside) for f, inside in found]


def remove_data(root):
    """Delete the install folder (install.ps1 -Uninstall -RemoveData), after stop. Refuses a
    protected folder, one inside a never-ours folder, one that holds a protected folder, and
    one that does not look like an install (no active.json or runtime folder)."""
    target = Path(os.path.abspath(root))
    for folder, also_inside in protected_folders():
        if _inside(folder, target) or (also_inside and _inside(target, folder)):
            raise VoiceError(f"Refusing to delete {target}: it is, holds or is inside {folder}.")
    if not target.exists():
        print(f"Nothing to delete at {target}.")
        return 0
    if not ((target / "active.json").is_file() or (target / RUNTIMES).is_dir()):
        raise VoiceError(f"Refusing to delete {target}: it is not an install folder "
                         "(no active.json or runtime folder).")
    shutil.rmtree(target, ignore_errors=True)
    if target.exists():
        raise VoiceError(f"Could not delete all of {target}; a voice connector may still be using it.",
                         "Close Claude Code, then run the uninstall again.")
    print(f"Deleted {target}.")
    return 0


def default_root():
    if WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "voicemode-local-native"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "voicemode-local-native"


def disk_use(root):
    """Bytes under the root and its runtime, downloads and python folders, each hard-linked
    file counted once per total."""
    totals = {}
    for key, folder in (("root", root), ("runtime", root / RUNTIMES), ("downloads", root / "downloads"),
                        ("python", root / "python")):
        seen, total = set(), 0
        for base, _, files in os.walk(folder):
            for name in files:
                try:
                    info = os.stat(os.path.join(base, name))
                except OSError:
                    continue
                if (info.st_dev, info.st_ino) not in seen:
                    seen.add((info.st_dev, info.st_ino))
                    total += info.st_size
        totals[key] = total
    return totals


def stable_python(root):
    """uv's minor-version link to its managed Python under the root
    (python\\cpython-3.12-windows-x86_64-none): uv moves it to each new patch release, so its
    path outlives runtime rebuilds, which a runtime's venv Python does not. None if absent."""
    data = json.loads(DEPENDENCIES.read_text(encoding="utf-8"))
    name = f"cpython-{data['python']}-windows-x86_64-none" if WINDOWS else f"cpython-{data['python']}"
    path = root / "python" / name / ("python.exe" if WINDOWS else "bin/python3")
    return path if path.is_file() else None


def info(root):
    """One JSON line for install.ps1."""
    record = read_json(root / "active.json")
    name = Path(str(record.get("runtime", ""))).name if isinstance(record, dict) else ""
    python = stable_python(root)
    print(json.dumps({"root": str(root), "runtime": str(root / RUNTIMES / name) if name else None,
                      "python": str(python) if python else None, "script": str(SCRIPT),
                      "default_root": str(default_root())}))
    return 0


def restart_with_runtime_python(root, args):
    """Reading other processes needs psutil, which the runtime has. None means carry on here.
    args: the command and its action, as given."""
    command = args[0]
    try:
        import psutil  # noqa: F401
        return None
    except ImportError:
        pass
    record = read_json(root / "active.json")
    name = Path(str(record.get("runtime", ""))).name if isinstance(record, dict) else ""
    python = venv_program(root / RUNTIMES / name, "python") if name else None
    if python is None or not python.is_file():
        if command == "stop":
            print("voicemode-local's native services are not running: nothing is set up here.")
            return 0
        return None  # the command itself reports what is missing
    # Only the inner launcher is in this job: its services and voice-mode go in jobs of their own.
    job = winjob.Job(silent_breakaway=True)
    # The flag after the command: a lease is alive while its launcher's command line holds
    # `voicemode_native.py launch`, so nothing may come between the two.
    flag = ["--allow-root-outside-profile"] if ALLOW_OUTSIDE else []
    # The install folder as its working folder, never the client's: Windows searches the
    # current folder first, and every child of the launcher would inherit it.
    env = dict(os.environ, VOICEMODE_NATIVE_ROOT=str(root))
    env.setdefault("VOICEMODE_CLIENT_FOLDER", os.getcwd())   # the session label: launch reads it
    child = start_in_job([str(python), str(SCRIPT), *args, *flag], job, cwd=str(root), env=env,
                         **procs.connector_options())
    return child.wait()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["host"]:
        # The Windows voice host, started by vml_supervisor outside every chat with the profile
        # file as its one argument: it inherits nothing, so it resolves no root or settings here.
        if len(argv) != 2:
            print("usage: voicemode_native.py host <profile file>", file=sys.stderr)
            return 2
        return sv_host.host_main(Path(argv[1]))
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["setup", "launch", "serve", "status", "stop", "watch",
                                            "barge", "pill", "ptt", "info", "remove-data"])
    parser.add_argument("action", nargs="?", choices=["on", "off", "auto", "status"],
                        help="for barge, pill and ptt: on, off or status; barge also auto")
    parser.add_argument("--allow-root-outside-profile", action="store_true",
                        help="accept a VOICEMODE_NATIVE_ROOT outside the profile and LOCALAPPDATA")
    args = parser.parse_args(argv)
    if (args.command in SWITCHES) != (args.action is not None):
        parser.error("on, off or status goes with barge, pill and ptt only, and they need one")
    if args.action == "auto" and args.command != "barge":
        parser.error("auto is for barge only")
    global ALLOW_OUTSIDE
    ALLOW_OUTSIDE = args.allow_root_outside_profile
    try:
        root = native_root()
        if args.command == "info":
            return info(root)
        if args.command == "remove-data":  # never under the runtime it deletes
            return remove_data(root)
        if WINDOWS and args.command != "setup":
            restarted = restart_with_runtime_python(root, [args.command, *([args.action] if args.action else [])])
            if restarted is not None:
                return restarted
        if args.command == "setup":
            setup(root)
            return 0
        if args.command == "stop":
            stop(root)
            return 0
        if args.command == "watch":
            return watch(root)
        if args.command in SWITCHES:
            return switch(root, args.command, args.action)
        return {"launch": launch, "serve": serve, "status": status}[args.command](root)
    except OffNetwork as error:
        print(f"voicemode-local: {error}", file=sys.stderr)
        print("Next step: check the network or VPN and run setup again. Finished downloads are kept.",
              file=sys.stderr)
        return EXIT_OFF_NETWORK
    except VoiceError as error:
        print(f"voicemode-local: {error}", file=sys.stderr)
        if error.next_step:
            print(f"Next step: {error.next_step}", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"voicemode-local: {error}", file=sys.stderr)
        print(f"Next step: if this repeats, run setup again. {setup_step()}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
