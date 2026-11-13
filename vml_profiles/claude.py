"""voicemode-local's Claude Code profile over vml_supervisor, and its two hooks.

scripts/windows/voicemode_native.py builds this profile (personal_profile) and hands it to the
package, which starts, leases, watches and stops the processes. The values are the launcher's own
from before Task 8, unchanged: ports 28880-28899 (TTS) and 22022-22041 (STT), never the WSL and
Docker setup's 8880, 8881, 8882, 2022 and 9000 nor FELIX's 18880-18899 and 12022-12041; the lease
markers `voicemode_native.py launch|serve`; the services' Job Object name, so a running install's
job is still found; 60 s to start, 5 s to stop, a look every 3 s, at most 3 restarts in 300 s.

Hooks (vml_supervisor.hooks; loaded once by the host, so this module imports what it needs at
import time and takes plain data):
- read_mode(profile, kind): the trigger's (barge) or the listener's (ptt) mode: "true" or "auto"
  for the trigger, "true" for the listener, "off" for neither.
- environment(profile, runtime, ports, modes): the connector's environment (profile.env, built by
  the launcher from runtime_environment, tool_selection and voice_mode_settings), pointed at the
  services' actual ports, with a helper's mode as its setting.

Where a mode comes from, first match wins:
1. run/modes.json: a helper a launch's settings turned on (a launch never turns one off:
   connectors are a union), or what a switch decided since. The host looks every few seconds, so without this a switch would be undone by the next
   look whenever the connector's environment said otherwise, and launch could not overrule a value
   an earlier switch left in voicemode.env, as it did before.
2. The connector's own environment, for a setting the launcher's environment held
   (VOICEMODE_NATIVE_ENV_SETTINGS lists them; voice-mode copies voicemode.env into its environment,
   which is no override).
3. The connector's ~/.voicemode/voicemode.env (its home from profile.env: the host's own may be
   another), read with patches/voice_pill_state.py's parser when it loads, else a plain read.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

from vml_supervisor.profile import HelperSpec, HostProfile, PortOverride, ServiceSpec
from vml_supervisor.state import atomic_json, service_lock

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "windows" / "voicemode_native.py"
PATCHES = REPO / "patches"
LOOPBACK = "127.0.0.1"

TTS_PORTS = (28880, 28899)
STT_PORTS = (22022, 22041)
FELIX_PORTS = (*range(18880, 18900), *range(12022, 12042))
PORTS_FILE = "ports.json"   # in run_dir: {"tts": port, "stt": port}, the launcher's port settings
MODES_FILE = "modes.json"   # in run_dir: {"barge": mode, "ptt": mode}, the latest decision
MODE_SETTINGS = {"barge": "VOICEMODE_VOICE_BARGE", "ptt": "VOICEMODE_PTT_ENABLED"}
ENV_SETTINGS = "VOICEMODE_NATIVE_ENV_SETTINGS"
SILERO = "models/silero/silero_vad.onnx"
LEASE_MARKERS = ("voicemode_native.py launch", "voicemode_native.py serve")
WATCH_MARKER = "voicemode_native.py watch"
HOST_MARKER = "voicemode_native.py host"
VOICE_SERVER_MARKER = "voice-mode"
START_TIMEOUT = 60.0
STOP_TIMEOUT = 5.0
WATCH_INTERVAL = 3.0
RESTART_LIMIT = (3, 300.0)
ALLOW_FLAG = "--allow-root-outside-profile"
WINDOWS = sys.platform == "win32"

# What profile.env may hold (final review I6): run/host-profile.json carries it, and before this
# branch nothing persisted the environment. The OS essentials, our own VOICEMODE_* settings, and
# the few keys runtime_environment sets itself; never a provider key or anything else of the user's.
WINDOWS_ENV = ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH", "PATHEXT", "COMSPEC", "USERPROFILE",
               "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "NUMBER_OF_PROCESSORS",
               "PROCESSOR_ARCHITECTURE", "OS")
POSIX_ENV = ("HOME", "PATH", "USER", "LANG", "TMPDIR")
OWN_ENV = ("FASTMCP_CHECK_FOR_UPDATES", "PYTHONUTF8", "PYTHONNOUSERSITE", "NO_PROXY", "no_proxy")


def allowed_env(env: dict, windows: bool = WINDOWS) -> dict:
    """env reduced to the allowlist above. Windows names are case-insensitive there."""
    out = {}
    for key, value in env.items():
        key, value = str(key), str(value)
        name = key.upper() if windows else key
        if key.startswith("VOICEMODE_") and "API_KEY" not in key.upper():
            out[key] = value
        elif key in OWN_ENV or (windows and name in WINDOWS_ENV) or (
                not windows and (name in POSIX_ENV or name.startswith("LC_"))):
            out[key] = value
    return out


def _load_settings_reader():
    """patches/voice_pill_state.py, the shared settings parser, or None when it cannot load."""
    try:
        spec = importlib.util.spec_from_file_location("vml_profiles_voice_pill_state",
                                                      PATCHES / "voice_pill_state.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:  # noqa: BLE001 - the plain read below stands in
        return None


_SETTINGS = _load_settings_reader()


# -- the commands ------------------------------------------------------------------------------

def job_name(root) -> str:
    """The services' Job Object, named as voicemode_native.py named it before Task 8."""
    return "Local\\voicemode-local-services-" + hashlib.sha256(str(root).lower().encode()).hexdigest()[:16]


def venv_program(runtime, name, windows=WINDOWS) -> Path:
    return Path(runtime) / (f".venv/Scripts/{name}.exe" if windows else f".venv/bin/{name}")


def powershell() -> str:
    """Windows PowerShell by its full path: the current folder is searched first otherwise."""
    system = os.environ.get("SystemRoot", r"C:\Windows")
    return str(Path(system) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")


def pill_data_dir(root) -> Path:
    """The pill window's own folder for this install: log, position, page copy, WebView2."""
    return Path(root) / "pill"


def ptt_log(root) -> Path:
    return Path(root) / "run" / "ptt-listener.log"


def optional_command(kind, runtime, root=None, windows=WINDOWS):
    """(command, marker) of the pill window, the push-to-talk listener or the barge trigger, which
    hears the mic through sounddevice. root is the install; by default the one that holds runtime.
    The listener runs in native mode: -SenderExe, the runtime's Python running
    voice_mode.ptt_control_client --stdin, which names the control pipe from the environment
    voice-mode also gets and writes the command; never the WSL relay."""
    runtime = Path(runtime)
    python = str(venv_program(runtime, "python", windows))
    if kind == "barge":
        return [python, "-m", "voice_mode.voice_barge", str(runtime / SILERO)], "voice_mode.voice_barge"
    script = REPO / ("pill-windows.ps1" if kind == "pill" else "ptt-listener-windows.ps1")
    command = [powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]
    root = Path(root) if root is not None else runtime.parent.parent
    if kind == "ptt":
        command += ["-SenderExe", python,
                    "-SenderArgs", subprocess.list2cmdline(["-m", "voice_mode.ptt_control_client", "--stdin"]),
                    "-LogPath", str(ptt_log(root))]
    if kind == "pill":
        command[2:2] = ["-STA"]
        # Native mode: the backend is started directly, no wsl.exe. -BackendArgs is one Windows
        # command-line tail, quoted as CreateProcess expects.
        command += ["-BackendExe", python,
                    "-BackendArgs", subprocess.list2cmdline(["-m", "voice_mode.voice_pill", "--stdio"]),
                    "-DataDir", str(pill_data_dir(root))]
    return command, str(script)


def service_specs(runtime, windows=WINDOWS) -> dict:
    """Kokoro and whisper.cpp on loopback; `--port {port}` as two arguments, so procs.owned matches."""
    runtime = Path(runtime)
    script = runtime / "services/kokoro-onnx-server.py"
    tts = ServiceSpec([str(venv_program(runtime, "python", windows)), str(script), "--host", LOOPBACK,
                       "--port", "{port}", "--model-dir", str(runtime / "models/kokoro")],
                      str(script), TTS_PORTS, "/v1/models", "kokoro")
    # Off Windows (tests, a Mac) a bare name: Popen searches PATH, and only Windows searches the
    # current folder first. Looking it up here would scan PATH on every profile built.
    whisper = str(runtime / "services/whisper/whisper-server.exe") if windows else "whisper-server"
    model = runtime / "models/whisper/ggml-base.bin"
    # No --convert: it needs ffmpeg, and whisper-server will not start without it (FELIX, 2026-11-04).
    stt = ServiceSpec([whisper, "--host", LOOPBACK, "--port", "{port}", "-m", str(model), "-l", "auto",
                       "--inference-path", "/v1/audio/transcriptions"],
                      str(model), STT_PORTS, "/health", "whisper")
    return {"tts": tts, "stt": stt}


def helper_specs(root, runtime, windows=WINDOWS) -> dict:
    """The trigger and the listener: Windows only, each started and stopped by its mode."""
    specs = {}
    for kind in ("barge", "ptt"):
        argv, marker = optional_command(kind, runtime, root, windows)
        specs[kind] = HelperSpec(argv=argv, marker=marker, log=f"{kind}.log", platforms=("win32",),
                                 needs_mode=True)
    return specs


def personal_profile(root: Path, runtime: Path, *, env: dict | None = None, allow_outside: bool = False,
                     windows: bool = WINDOWS, watch_python: str | None = None) -> HostProfile:
    """The install at root, running the runtime folder `runtime`. env is what services and helpers
    get (the launcher passes runtime_environment + tool_selection + voice_mode_settings)."""
    root, runtime = Path(root), Path(runtime)
    python = venv_program(runtime, "python", windows)
    flag = [ALLOW_FLAG] if allow_outside else []
    return HostProfile(
        name="voicemode-local",
        root=root,
        run_dir=root / "run",
        runtime_dir=runtime,
        python=python,
        working_dir=root / "voice-mode",
        env=allowed_env(env or {}, windows),
        services=service_specs(runtime, windows),
        helpers=helper_specs(root, runtime, windows),
        reserved_ports=FELIX_PORTS,
        port_override=PortOverride("file", root / "run" / PORTS_FILE),
        start_timeout=START_TIMEOUT,
        stop_timeout=STOP_TIMEOUT,
        watch_interval=WATCH_INTERVAL,
        restart_limit=RESTART_LIMIT,
        lease_markers=LEASE_MARKERS,
        voice_server_marker=VOICE_SERVER_MARKER,
        # The host needs no flag: everything it needs is in the profile file appended to this.
        host_command=[str(python), str(SCRIPT), "host"],
        host_marker=HOST_MARKER,
        watch_command=[watch_python or sys.executable, str(SCRIPT), "watch", *flag],
        watch_marker=WATCH_MARKER,
        job_name=job_name(root),
        hooks={"read_mode": "vml_profiles.claude:read_mode",
               "environment": "vml_profiles.claude:environment"},
        hook_paths=(REPO,),
    )


# -- modes -------------------------------------------------------------------------------------

def mode_of(kind: str, value) -> str:
    """A setting's value as the helper's mode, read as voice_pill_state reads it: spaces and quotes
    around it dropped, any case. The trigger: true, auto or off; the listener: true or off."""
    word = (value or "").strip().strip("\"'").strip().lower()
    if word == "true" or (kind == "barge" and word == "auto"):
        return word
    return "off"


def read_modes(run_dir) -> dict:
    """run/modes.json; {} when it does not exist. A corrupt file raises (the hook's caller records
    hook_failed and leaves the helper as it is): a guess must not start or stop one."""
    try:
        text = (Path(run_dir) / MODES_FILE).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{Path(run_dir) / MODES_FILE} is not a JSON object")
    return data


def write_modes(run_dir, modes: dict) -> None:
    """Merge modes into run/modes.json under the services' lock (two connectors or a switch may
    write at once), atomically (the host reads it meanwhile). A corrupt file must not stop every
    launch and switch, and setup would not repair it: it is read as {} and named on stderr."""
    path = Path(run_dir) / MODES_FILE
    with service_lock(run_dir):
        try:
            current = read_modes(run_dir)
        except ValueError as error:
            print(f"voicemode-local: {path} is corrupt ({error}); it is written again from the modes "
                  "decided now", file=sys.stderr, flush=True)
            current = {}
        current.update({kind: mode for kind, mode in modes.items() if kind in MODE_SETTINGS})
        atomic_json(path, current)


def _voicemode_home(env: dict) -> Path:
    """The connector's ~/.voicemode: from its home in profile.env (USERPROFILE on Windows, HOME
    elsewhere, as Path.home() reads them), else this process's. The Windows host is started with
    the user's default environment, not the connector's, so its own home may be another."""
    base = env.get("USERPROFILE" if sys.platform == "win32" else "HOME")
    return Path(base) / ".voicemode" if base else Path.home() / ".voicemode"


def _file_settings(env: dict) -> dict:
    home = _voicemode_home(env)
    if _SETTINGS is not None:
        return _SETTINGS.read_env_file(home)
    path = home / "voicemode.env"
    try:
        text = path.read_bytes().decode("utf-8-sig", errors="replace")
    except OSError:
        return {}
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        out[key] = value.strip()
    return out


def read_mode(profile: dict, kind: str):
    """The hook: see the module docstring. None for anything that is not a helper here."""
    name = MODE_SETTINGS.get(kind)
    if name is None:
        return None
    decided = read_modes(profile["run_dir"])
    if isinstance(decided.get(kind), str):
        return decided[kind]
    env = profile.get("env") or {}
    listed = env.get(ENV_SETTINGS)
    held = name in env and (listed is None or name in listed.split(","))
    return mode_of(kind, env[name] if held else _file_settings(env).get(name, ""))


def environment(profile: dict, runtime: str, ports: dict, modes: dict) -> dict:
    """The hook: see the module docstring."""
    env = {str(k): str(v) for k, v in (profile.get("env") or {}).items()}
    for kind, name in (("tts", "VOICEMODE_TTS_BASE_URLS"), ("stt", "VOICEMODE_STT_BASE_URLS")):
        port = ports.get(kind)
        if isinstance(port, int) and not isinstance(port, bool):
            env[name] = f"http://{LOOPBACK}:{port}/v1"
    for kind, mode in modes.items():
        name = MODE_SETTINGS.get(kind)
        if name and isinstance(mode, str) and mode != "off":
            env[name] = mode
    return env
