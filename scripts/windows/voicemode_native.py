#!/usr/bin/env python3
"""voicemode-local on native Windows: the MCP command, its speech services, and their runtime.

Provenance: the runtime, service and lease logic is copied from FELIX,
felix-framework plugins/voicemode/scripts/voice.py (and the active.json lookup of
scripts/launch.js) at commit aea841ffd, branch feat/voice-opt-in: setup with a
hash-locked uv sync, atomic runtime selection, the pinned whisper.cpp zip, model hard
links, connector_options, process_tree and kill_tree, leases and the watchdog,
port_free. Copied rather than imported, and changed for a personal install: its own
folder and ports, the user's home and settings, voicemode-local's patches/apply.py,
optional pill and push-to-talk services, and Job Objects (new here).

Commands
  setup   install a runtime into <root>\\runtime\\<digest>-<random>, select it in active.json
  launch  the MCP command: leased speech services, the pill and push-to-talk listener when
          enabled, then voice-mode on this process's stdin and stdout
  serve   the leased speech services only, until stdin closes (diagnosis and tests)
  status  what is installed and running; starts nothing
  stop    stop this install's services
  watch   internal: stops the services once no connector is left, then exits

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

Lifetime: a connector holds a lease while it runs. The services, the pill and the
listener live in one named Job Object per install that kills them when its last handle
closes; every connector and the watchdog hold a handle. The watchdog restarts a crashed
TTS or STT service on its own port, at most MAX_RESTARTS times in RESTART_WINDOW
seconds; past that, the next connector starts a new one. voice-mode lives in its
connector's own Job Object, so it dies with the connector. Never os.kill(pid, 0): on
Windows that ends the process instead of checking it.

Reading other processes needs psutil, which only the runtime has: main() runs itself
again with the runtime's Python when the Python it started under lacks it.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener, urlopen

WINDOWS = sys.platform == "win32"
SCRIPT = Path(__file__).resolve()
HERE = SCRIPT.parent
REPO = HERE.parents[1]
DEPENDENCIES = HERE / "dependencies.json"
PATCHES = REPO / "patches"
TTS_SERVER = REPO / "kokoro-onnx-server.py"

LOOPBACK = "127.0.0.1"
LOCAL_HOSTS = "127.0.0.1,localhost,::1"
PROXY_VARIABLES = ("http_proxy", "https_proxy", "all_proxy", "ftp_proxy")
# Ours, tried in order. FELIX's are 18880+ and 12022+; the live WSL and Docker setup
# owns 8880, 8881, 8882, 2022 and 9000.
PORTS = {"tts": list(range(28880, 28900)), "stt": list(range(22022, 22042))}
RESERVED = frozenset({8880, 8881, 8882, 2022, 9000} | set(range(18880, 18900)) | set(range(12022, 12042)))
PORT_SETTINGS = {"tts": "VOICEMODE_NATIVE_TTS_PORT", "stt": "VOICEMODE_NATIVE_STT_PORT"}
SPEECH = ("tts", "stt")
OPTIONAL = ("pill", "ptt")
OPTIONAL_SETTINGS = {"pill": "VOICEMODE_PILL", "ptt": "VOICEMODE_PTT_ENABLED"}
LAUNCH_MARKERS = ("voicemode_native.py launch", "voicemode_native.py serve")

LAYOUT = 1
RUNTIMES = "runtime"
REQUIRED_FILES = (
    ".venv/Scripts/python.exe" if WINDOWS else ".venv/bin/python",
    ".venv/Scripts/voice-mode.exe" if WINDOWS else ".venv/bin/voice-mode",
    "services/kokoro-onnx-server.py",
    "dependencies.json",
    *(("services/whisper/whisper-server.exe",) if WINDOWS else ()),
)
WATCH_INTERVAL = 3
MAX_RESTARTS = 3
RESTART_WINDOW = 300
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
START_TIMEOUT = 60
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

# Speech-service checks never go through a proxy, whatever the environment says.
_local_opener = build_opener(ProxyHandler({}))


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


def optional_services():
    """The pill and the push-to-talk listener, each only when its setting is exactly true.
    Push-to-talk is refused natively until task 7: ptt-listener-windows.ps1 still sends to the WSL
    relay on 127.0.0.1:8765, so it would drive the WSL install, not this one."""
    enabled = [kind for kind in OPTIONAL if (setting(OPTIONAL_SETTINGS[kind], "") or "").strip() == "true"]
    if WINDOWS and "ptt" in enabled:
        enabled.remove("ptt")
        print("voicemode-local: push-to-talk not started: its Windows listener still sends to the WSL "
              "relay on 127.0.0.1:8765 and would drive the WSL install (native mode is task 7).",
              file=sys.stderr, flush=True)
    return enabled


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
    elif WINDOWS:
        root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "voicemode-local-native"
    else:
        root = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "voicemode-local-native"
    root = root.resolve()
    for felix in felix_roots():
        if root == felix or felix in root.parents:
            raise VoiceError(f"{root} is inside FELIX's folder {felix}; voicemode-local never writes there.",
                             "Set VOICEMODE_NATIVE_ROOT to a folder of its own.")
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


@contextlib.contextmanager
def exclusive(path):
    """Hold a lock file for the block; released at process end on both systems."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as lock:
        if sys.platform == "win32":  # the real OS, whatever WINDOWS is set to
            import msvcrt

            while True:
                try:
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.2)
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock, fcntl.LOCK_EX)
            yield


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


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".record-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output)
            output.write("\n")
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


def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return default


def runtime_environment(runtime, ports=None):
    """The user's environment, minus proxies, pointed at this install's own services."""
    ports = ports or {kind: PORTS[kind][0] for kind in SPEECH}
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
    run_dir = root / "run"
    holders = root / RUNTIMES
    if not holders.is_dir():
        return []
    with service_lock(run_dir):
        keep = {active}
        for pid in live_leases(run_dir):
            record = read_json(run_dir / "clients" / str(pid), {})
            if isinstance(record, dict) and record.get("runtime"):
                keep.add(Path(record["runtime"]).name)
        for entry in (read_json(run_dir / "state.json", {}) or {}).values():
            if owned(entry) and entry.get("runtime"):
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


# -- Windows Job Objects -------------------------------------------------------

class Job:
    """A Windows Job Object that ends every process in it when its last handle closes.

    Named, it is shared: every process that opens the name holds a handle, and the
    processes in it outlive all but the last of them. With silent_breakaway only the
    processes assigned to it are in it, not what they start: for a parent whose child
    starts processes that must live in jobs of their own (a process already in a job can
    only join one nested inside it).
    """

    def __init__(self, name=None, silent_breakaway=False):
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        k = self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateJobObjectW.restype = wintypes.HANDLE
        k.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
        k.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
        k.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        k.CloseHandle.argtypes = (wintypes.HANDLE,)

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", ctypes.c_uint64 * 6),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        self.handle = k.CreateJobObjectW(None, name)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = Extended()
        # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, plus BREAKAWAY_OK | SILENT_BREAKAWAY_OK when asked.
        info.BasicLimitInformation.LimitFlags = 0x2000 | (0x1800 if silent_breakaway else 0)
        if not k.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            error = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(error)

    def assign(self, process_handle):
        if not self._kernel.AssignProcessToJobObject(self.handle, int(process_handle)):
            raise self._ctypes.WinError(self._ctypes.get_last_error())

    def close(self):
        if self.handle:
            self._kernel.CloseHandle(self.handle)
            self.handle = None


def _resume_threads(pid):
    """Resume a process created suspended: every thread it owns (it has one)."""
    import ctypes
    from ctypes import wintypes

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k.OpenThread.restype = wintypes.HANDLE
    k.ResumeThread.argtypes = (wintypes.HANDLE,)
    k.CloseHandle.argtypes = (wintypes.HANDLE,)

    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
                    ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG), ("dwFlags", wintypes.DWORD)]

    snapshot = k.CreateToolhelp32Snapshot(0x4, 0)  # TH32CS_SNAPTHREAD
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    resumed = 0
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(Entry)
        more = k.Thread32First(snapshot, ctypes.byref(entry))
        while more:
            if entry.th32OwnerProcessID == pid:
                thread = k.OpenThread(0x2, False, entry.th32ThreadID)  # THREAD_SUSPEND_RESUME
                if thread:
                    if k.ResumeThread(thread) != 0xFFFFFFFF:
                        resumed += 1
                    k.CloseHandle(thread)
            more = k.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        k.CloseHandle(snapshot)
    if not resumed:
        raise RuntimeError(f"could not resume process {pid}")


def spawn(command, job=None, **kwargs):
    """Popen; on Windows with a job, created suspended and put in the job before its first
    instruction, so nothing it starts can escape the job."""
    if not (WINDOWS and job is not None):
        return subprocess.Popen(command, **kwargs)
    kwargs["creationflags"] = kwargs.get("creationflags", 0) | 0x4  # CREATE_SUSPENDED
    process = subprocess.Popen(command, **kwargs)
    try:
        try:
            job.assign(process._handle)
        except OSError as error:
            # Voice still works; only the guarantee that it ends with its owner is lost.
            print(f"voicemode-local: warning: could not put {Path(command[0]).name} in a Job Object "
                  f"({error}); it may outlive this launcher.", file=sys.stderr, flush=True)
        _resume_threads(process.pid)
    except BaseException:
        process.kill()
        raise
    return process


_jobs = {}


def services_job(root):
    """This install's shared Job Object for its services, held open for this process's lifetime."""
    if not WINDOWS:
        return None
    if root not in _jobs:
        name = "Local\\voicemode-local-services-" + hashlib.sha256(str(root).lower().encode()).hexdigest()[:16]
        _jobs[root] = Job(name)
    return _jobs[root]


def connector_job():
    """A private Job Object for this connector's own voice-mode: it dies when this process does."""
    if not WINDOWS:
        return None
    if None not in _jobs:
        _jobs[None] = Job()
    return _jobs[None]


# -- processes (FELIX's) ------------------------------------------------------

def connector_options():
    """voice-mode talks to the MCP host over this process's own stdin and stdout. On Windows Python
    passes those on only when named (FELIX, 2026-11-04: unnamed, `initialize` got no answer in 120 s)."""
    if not WINDOWS:
        return {}
    return {"stdin": sys.stdin, "stdout": sys.stdout, "stderr": sys.stderr,
            "creationflags": subprocess.CREATE_NO_WINDOW}


def background_options():
    """A process that outlives this one, with no window."""
    if WINDOWS:
        return {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _psutil_process(pid):
    import psutil

    try:
        return psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def process_command(pid):
    """A process's command line, or None when it is gone."""
    if WINDOWS:
        import psutil

        process = _psutil_process(pid)
        try:
            return (" ".join(process.cmdline()) or None) if process else None
        except psutil.Error:
            return None
    result = subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(pid)],
                            capture_output=True, text=True, check=False)
    return result.stdout.strip() or None


def process_tree(pid):
    """A process and everything it started. A venv's python.exe starts the real interpreter as a
    child on Windows, and the child is the one that listens (FELIX, 2026-11-04)."""
    if not WINDOWS:
        return {pid}
    import psutil

    process = _psutil_process(pid)
    if process is None:
        return set()
    try:
        return {pid, *(child.pid for child in process.children(recursive=True))}
    except psutil.Error:
        return {pid}


def kill_tree(pid):
    import psutil

    process = _psutil_process(pid)
    if process is None:
        return
    try:
        members = [*process.children(recursive=True), process]
    except psutil.Error:
        members = [process]
    for member in members:
        with contextlib.suppress(psutil.Error):
            member.kill()
    psutil.wait_procs(members, timeout=STOP_TIMEOUT)


def listener_pids(port):
    """Process ids listening on a TCP port."""
    if WINDOWS:
        import psutil

        return {item.pid for item in psutil.net_connections("tcp")
                if item.status == psutil.CONN_LISTEN and item.laddr and item.laddr.port == port and item.pid}
    known = [p for p in ("/usr/sbin/lsof", "/usr/bin/lsof") if os.path.exists(p)]
    lsof = known[0] if known else shutil.which("lsof") or "lsof"
    result = subprocess.run([lsof, "-nP", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"],
                            capture_output=True, text=True, check=False)
    return {int(line) for line in result.stdout.split() if line.isdigit()}


def owned(entry):
    """A recorded service counts as ours only while its command still matches."""
    if not isinstance(entry, dict) or not isinstance(entry.get("pid"), int) or entry["pid"] <= 0:
        return False
    command = process_command(entry["pid"])
    if not command or entry.get("marker", "\0") not in command:
        return False
    return entry.get("port") is None or f"--port {entry['port']}" in command


# -- services ----------------------------------------------------------------

def port_free(port):
    """True only when nothing answers on the port and it can be bound on loopback."""
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
        probe.settimeout(0.5)
        if probe.connect_ex((LOOPBACK, port)) == 0:
            return False
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
        try:
            probe.bind((LOOPBACK, port))
        except OSError:
            return False
    return True


def port_candidates(kind):
    """The port from the setting (if any), then our own range."""
    raw = setting(PORT_SETTINGS[kind])
    if raw is None:
        return list(PORTS[kind])
    try:
        port = int(str(raw).strip())
    except ValueError:
        port = None
    if port is None or not 1024 <= port <= 65535 or port in RESERVED:
        raise VoiceError(f"{PORT_SETTINGS[kind]}={raw!r} is not usable: give a port from 1024 to 65535 "
                         "outside 8880-8882, 2022, 9000 and FELIX's 18880-18899 and 12022-12041.",
                         f"Fix or remove {PORT_SETTINGS[kind]}, then restart the voice connector.")
    return [port, *[p for p in PORTS[kind] if p != port]]


def choose_port(kind, taken):
    candidates = port_candidates(kind)
    port = next((p for p in candidates if p not in taken and port_free(p)), None)
    if port is None:
        raise VoiceError(f"No free local port for the {kind.upper()} service "
                         f"(tried {candidates[0]} to {candidates[-1]}).",
                         f"Close what uses them, or set {PORT_SETTINGS[kind]}, then restart.")
    return port


def service_command(kind, runtime, port):
    """(command, marker): the marker is what identifies the process as ours later."""
    if kind == "tts":
        script = runtime / "services/kokoro-onnx-server.py"
        return [str(venv_program(runtime, "python")), str(script), "--host", LOOPBACK, "--port", str(port),
                "--model-dir", str(runtime / "models/kokoro")], str(script)
    whisper = str(whisper_program(runtime)) if WINDOWS else (on_path("whisper-server") or "whisper-server")
    model = runtime / "models/whisper/ggml-base.bin"
    # No --convert: it needs ffmpeg, and whisper-server will not start without it (FELIX, 2026-11-04).
    return [whisper, "--host", LOOPBACK, "--port", str(port), "-m", str(model), "-l", "auto",
            "--inference-path", "/v1/audio/transcriptions"], str(model)


def powershell():
    system = os.environ.get("SystemRoot", r"C:\Windows")
    return str(Path(system) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")


def optional_command(kind, runtime):
    """The pill window or the push-to-talk listener. Their native modes are tasks 6 and 7."""
    script = REPO / ("pill-windows.ps1" if kind == "pill" else "ptt-listener-windows.ps1")
    command = [powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]
    if kind == "pill":
        command[2:2] = ["-STA"]
        command += ["-BackendCommand", f'"{venv_program(runtime, "python")}" -m voice_mode.voice_pill --stdio']
    return command, str(script)


def valid_response(kind, data):
    if kind == "tts":
        return isinstance(data, dict) and isinstance(data.get("data"), list) and any(
            isinstance(m, dict) and m.get("id") == "kokoro-onnx" for m in data["data"])
    return isinstance(data, dict) and data.get("status") == "ok"


def check_service(kind, port):
    address = url(port) + "/models" if kind == "tts" else f"http://{LOOPBACK}:{port}/health"
    try:
        response = _local_opener.open(address, timeout=2)
    except (URLError, OSError) as error:
        raise RuntimeError(f"Local {kind.upper()} service unavailable at {address}: {error}") from error
    with response:
        if response.status != 200 or not valid_response(kind, json.load(response)):
            raise RuntimeError(f"Incompatible local {kind.upper()} service at {address}.")


@contextlib.contextmanager
def service_lock(run_dir):
    run_dir.mkdir(parents=True, exist_ok=True)
    with exclusive(run_dir / "services.lock"):
        yield


def live_leases(run_dir):
    """Connector leases whose launcher, or the voice-mode it started, still runs; the rest go."""
    clients = run_dir / "clients"
    clients.mkdir(parents=True, exist_ok=True)
    alive = []
    for lease in clients.iterdir():
        if lease.name.startswith("."):
            continue  # being written
        try:
            pid = int(lease.name)
        except ValueError:
            lease.unlink(missing_ok=True)
            continue
        command = process_command(pid)
        record = read_json(lease, {}) if lease.stat().st_size else {}
        child = record.get("child") if isinstance(record, dict) else None
        child_command = process_command(child) if isinstance(child, int) and child > 0 else None
        if (command and any(m in command for m in LAUNCH_MARKERS)) or (child_command and "voice-mode" in child_command):
            alive.append(pid)
        else:
            lease.unlink(missing_ok=True)
    return alive


def stop_recorded(run_dir, state):
    for kind in list(state):
        entry = state.pop(kind)
        if not owned(entry):
            continue  # gone already, or the pid now belongs to another program
        if WINDOWS:
            kill_tree(entry["pid"])
            continue
        with contextlib.suppress(ProcessLookupError):
            os.kill(entry["pid"], signal.SIGTERM)
        deadline = time.monotonic() + STOP_TIMEOUT
        while owned(entry) and time.monotonic() < deadline:
            time.sleep(0.1)
        if owned(entry):
            with contextlib.suppress(ProcessLookupError):
                os.kill(entry["pid"], signal.SIGKILL)
    atomic_json(run_dir / "state.json", state)


def ensure_services(root, runtime, env, optional=None):
    """Register a lease, make sure the watchdog runs, then start (or reuse) the speech services and
    the enabled optional ones; wait until both speech services answer. Returns their ports."""
    optional = optional_services() if optional is None else optional
    run_dir = root / "run"
    started = {}
    job = services_job(root)
    with service_lock(run_dir):
        # The lease and the watchdog first: a launcher killed at any later point cannot leave
        # services without supervision.
        if not (runtime / ".complete.json").is_file():
            raise VoiceError(f"The runtime {runtime.name} was replaced while starting.",
                             "Restart the voice connector.")
        (run_dir / "clients").mkdir(exist_ok=True)
        atomic_json(run_dir / "clients" / str(os.getpid()), {"runtime": str(runtime)})
        start_watchdog(root)
        state = read_json(run_dir / "state.json", {}) or {}
        for kind in (*SPEECH, *optional):
            if owned(state.get(kind)):
                continue
            state.pop(kind, None)
            if kind in SPEECH:
                # A service still loading has not bound its port yet: never pick it twice.
                port = choose_port(kind, {e.get("port") for e in state.values() if isinstance(e, dict)})
                command, marker = service_command(kind, runtime, port)
            else:
                port = None
                command, marker = optional_command(kind, runtime)
            log = run_dir / f"{kind}.log"
            with log.open("a") as output:
                process = spawn(command, job=job, env=env, stdin=subprocess.DEVNULL, stdout=output,
                                stderr=output, **background_options())
            state[kind] = {"pid": process.pid, "port": port, "marker": marker, "log": str(log),
                           "command": [str(c) for c in command], "runtime": str(runtime)}
            started[kind] = process
            try:
                atomic_json(run_dir / "state.json", state)
            except BaseException:
                process.kill()
                raise
    deadline = time.monotonic() + START_TIMEOUT
    for kind in SPEECH:
        entry = state[kind]
        while True:
            try:
                check_service(kind, entry["port"])
                # The answer must come from the process we recorded, not another that took the port.
                listeners = listener_pids(entry["port"])
                if not process_tree(entry["pid"]) & listeners or not owned(entry):
                    raise VoiceError(f"Something other than our {kind.upper()} service answered on "
                                     f"{LOOPBACK}:{entry['port']} (ours is process {entry['pid']}; listening: "
                                     f"{sorted(listeners) or 'none found'}); voice was not connected to it.",
                                     f"Restart the voice connector, or set {PORT_SETTINGS[kind]}.")
                break
            except VoiceError:
                raise
            except RuntimeError as error:
                process = started.get(kind)
                if process is not None and process.poll() is not None:
                    raise VoiceError(f"The local {kind.upper()} service stopped while starting.",
                                     f"See {entry['log']}.") from error
                if time.monotonic() >= deadline:
                    raise VoiceError(f"Local speech services did not become ready: {error}",
                                     f"See {entry['log']}.") from error
                time.sleep(0.5)
    return {kind: state[kind]["port"] for kind in SPEECH}


def start_watchdog(root):
    """Start (once) the process that stops the services when every connector is gone."""
    pid_file = root / "run" / "watch.pid"
    with contextlib.suppress(ValueError, OSError):
        command = process_command(int(pid_file.read_text()))
        if command and "voicemode_native.py watch" in command:
            return
    env = dict(os.environ, VOICEMODE_NATIVE_ROOT=str(root))
    command = [sys.executable, str(SCRIPT), "watch", *(["--allow-root-outside-profile"] if ALLOW_OUTSIDE else [])]
    process = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **background_options())
    pid_file.write_text(str(process.pid))


def watch(root):
    """Hold the services' job open; restart a crashed speech service; stop the services once no
    lease is alive, then exit."""
    services_job(root)
    restarts = {}
    while True:
        time.sleep(WATCH_INTERVAL)
        if not watch_tick(root, restarts):
            return


def _watch_log(run_dir, line):
    with (run_dir / "watch.log").open("a", encoding="utf-8") as log:
        log.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")


def watch_tick(root, restarts):
    """One look: False once everything is stopped. A TTS or STT service that died is started again
    with its recorded command on its recorded port, at most MAX_RESTARTS times in RESTART_WINDOW
    seconds, and only while that port is free."""
    run_dir = root / "run"
    with service_lock(run_dir):
        if not live_leases(run_dir):
            stop_recorded(run_dir, read_json(run_dir / "state.json", {}) or {})
            return False
        state = read_json(run_dir / "state.json", {}) or {}
        changed = False
        now = time.monotonic()
        for kind in SPEECH:
            entry = state.get(kind)
            if not isinstance(entry, dict) or not entry.get("command") or owned(entry):
                continue
            recent = [t for t in restarts.get(kind, []) if now - t < RESTART_WINDOW]
            restarts[kind] = recent
            if len(recent) >= MAX_RESTARTS:
                continue  # the next connector starts a new one
            if not port_free(entry["port"]):
                _watch_log(run_dir, f"{kind} died; port {entry['port']} is taken, not restarted")
                continue
            with open(entry["log"], "a") as output:
                process = spawn(entry["command"], job=services_job(root),
                                env=runtime_environment(Path(entry["runtime"])), stdin=subprocess.DEVNULL,
                                stdout=output, stderr=output, **background_options())
            _watch_log(run_dir, f"restarted {kind} on port {entry['port']}: process {process.pid} "
                                f"(was {entry['pid']})")
            entry["pid"] = process.pid
            restarts[kind].append(now)
            changed = True
        if changed:
            atomic_json(run_dir / "state.json", state)
    return True


def release(root):
    """Drop this connector's lease; stop the services when no connector is left."""
    run_dir = root / "run"
    with service_lock(run_dir):
        (run_dir / "clients" / str(os.getpid())).unlink(missing_ok=True)
        if not live_leases(run_dir):
            stop_recorded(run_dir, read_json(run_dir / "state.json", {}) or {})


def stop(root):
    run_dir = root / "run"
    with service_lock(run_dir):
        others = live_leases(run_dir)
        state = read_json(run_dir / "state.json", {}) or {}
        running = [kind for kind in state if owned(state[kind])]
        stop_recorded(run_dir, state)
    print(f"Stopped voicemode-local's native services: {', '.join(running) or 'none were running'}.")
    if others:
        print(f"{len(others)} voice connector(s) still running will have no speech until they restart.")


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


def launch(root):
    """The MCP command: services, then the optional pill and listener, then voice-mode on stdio."""
    data, expected = manifest()
    runtime, current = configured_runtime(root, expected)
    if not current:
        print("voicemode-local: the native runtime is out of date; the previous one is used. "
              f'To update, run: "{sys.executable}" "{SCRIPT}" setup', file=sys.stderr, flush=True)
    check_models(runtime)
    child = None
    stopping = []
    try:
        with forwarded_signals(lambda: child, stopping):
            ports = ensure_services(root, runtime, runtime_environment(runtime))
            env = runtime_environment(runtime, ports)
            child = spawn([str(venv_program(runtime, "voice-mode"))], job=connector_job(), env=env,
                          **connector_options())
            # If this launcher dies, its voice-mode keeps the lease alive until it ends too.
            atomic_json(root / "run" / "clients" / str(os.getpid()), {"child": child.pid, "runtime": str(runtime)})
            while True:
                try:
                    return child.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    if stopping and time.monotonic() - stopping[0] > STOP_TIMEOUT:
                        child.kill()
    except Stopped as stopped:
        return 128 + stopped.args[0]
    finally:
        if child is not None and child.poll() is None:
            if WINDOWS:
                kill_tree(child.pid)
            else:
                child.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    child.wait(STOP_TIMEOUT)
        release(root)


def serve(root):
    """The leased speech services alone, until stdin closes. Prints their ports as one JSON line."""
    data, expected = manifest()
    runtime, _ = configured_runtime(root, expected)
    check_models(runtime)
    try:
        ports = ensure_services(root, runtime, runtime_environment(runtime), optional=[])
        print(json.dumps(ports), flush=True)
        sys.stdin.read()
        return 0
    finally:
        release(root)


def status(root):
    data, expected = manifest()
    runtime, current = configured_runtime(root, expected)
    check_models(runtime)
    state = read_json(root / "run/state.json", {}) or {}
    running = {kind: entry for kind, entry in state.items() if owned(entry)}
    for kind in SPEECH:
        if kind in running:
            check_service(kind, running[kind]["port"])
    if running:
        print("Running: " + ", ".join(
            f"{kind} {LOOPBACK}:{e['port']}" if e.get("port") else kind for kind, e in running.items()) + ".")
    else:
        print("Not running; the services start with the voice connector.")
    print(f"Installed at {runtime}.")
    if not current:
        print("The runtime is out of date: run setup.")
        return 1
    return 0


def restart_with_runtime_python(root, command):
    """Reading other processes needs psutil, which the runtime has. None means carry on here."""
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
    job = Job(silent_breakaway=True)
    flag = ["--allow-root-outside-profile"] if ALLOW_OUTSIDE else []
    child = spawn([str(python), str(SCRIPT), command, *flag], job=job,
                  env=dict(os.environ, VOICEMODE_NATIVE_ROOT=str(root)), **connector_options())
    return child.wait()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["setup", "launch", "serve", "status", "stop", "watch"])
    parser.add_argument("--allow-root-outside-profile", action="store_true",
                        help="accept a VOICEMODE_NATIVE_ROOT outside the profile and LOCALAPPDATA")
    args = parser.parse_args(argv)
    global ALLOW_OUTSIDE
    ALLOW_OUTSIDE = args.allow_root_outside_profile
    try:
        root = native_root()
        if WINDOWS and args.command != "setup":
            restarted = restart_with_runtime_python(root, args.command)
            if restarted is not None:
                return restarted
        if args.command == "setup":
            setup(root)
            return 0
        if args.command == "stop":
            stop(root)
            return 0
        if args.command == "watch":
            watch(root)
            return 0
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
