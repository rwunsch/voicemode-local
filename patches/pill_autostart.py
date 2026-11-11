"""The voice server brings the voice pill's window up: no launcher needed.

(Robert, 2026-11-10) The pill opens with the first voice session and closes with the last,
on every host. voice_pill_state.enter() calls ensure() after a session has entered a turn.
It is cheap when there is nothing to do: a few file reads and no process. It starts the
window only when all of these hold:

  - VOICEMODE_PILL is true (environment, then voicemode.env: voice_pill_state.setting)
  - VOICEMODE_PILL_AUTOSTART is not "off" (off: this server never starts or stops a window)
  - the user has not closed the pill (<base>/pill/closed; see voice_pill_state)
  - no pill backend is alive (<base>/pill/backend.json, written by voice_pill.main)
  - no start was tried in the last LAUNCH_SPACING_S (two sessions entering at once)

The window closes itself when no session is left (voice_pill.EXIT_DONE), so nothing this
starts outlives the last voice session for long.

Per OS, from the files apply.py copies into the voice_mode package:
  native Windows  powershell.exe -STA pill-windows.ps1 -BackendExe <python>
                  -BackendArgs "-m voice_mode.voice_pill --stdio" -DataDir <data dir>
  WSL             pill-windows.ps1 on the Windows side through Start-Process, backend
                  "<python>" "<package>/voice_pill.py" --stdio run through wsl.exe
  macOS           <pill python> voice_pill_mac.py --backend "<python>" "<package>/voice_pill.py"

Settings (environment first, as voice_pill_state.setting reads them):
  VOICEMODE_PILL_DATA_DIR  the window's folder on Windows (log, position, WebView2, and the
                           mutex and stop-event names). Default <VOICEMODE_NATIVE_ROOT>\\pill,
                           else <base>/pill/window.
  VOICEMODE_PILL_PYTHON    the backend's interpreter natively (default: this one); on macOS
                           the pill venv's python (default ~/.voicemode/pill-venv/bin/python).

Stdlib only, like voice_pill_state.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

if __package__ == "voice_mode":
    from voice_mode import voice_pill_state as st
else:  # loaded from patches/ (tests): the sibling file, never an installed voice_mode
    import voice_pill_state as st

HERE = Path(__file__).resolve().parent
AUTOSTART_SETTING = "VOICEMODE_PILL_AUTOSTART"
DATA_DIR_SETTING = "VOICEMODE_PILL_DATA_DIR"
PYTHON_SETTING = "VOICEMODE_PILL_PYTHON"
LAUNCH_SPACING_S = 20.0   # the WSL window takes up to ~15 s to start its backend
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def platform_kind() -> str | None:
    """"windows", "wsl", "macos", or None where there is no window to start."""
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if os.environ.get("WSL_DISTRO_NAME") and shutil.which("powershell.exe"):
        return "wsl"
    return None


def autostart_on() -> bool:
    return str(st.setting(AUTOSTART_SETTING, "") or "").strip().strip('"').lower() != "off"


def backend_alive(base=None) -> bool:
    """A pill backend has written its pid and is still that process."""
    info = st._read_json(st._base(base) / "backend.json")
    pid = info.get("pid")
    return isinstance(pid, int) and st._alive_same(pid, info.get("start"))


def _launch_due(base) -> bool:
    """Claims the next launch, unless one was claimed in the last LAUNCH_SPACING_S."""
    path = st._base(base) / "autostart.json"
    last = st._read_json(path).get("at")
    now = time.time()
    if isinstance(last, (int, float)) and 0 <= now - last < LAUNCH_SPACING_S:
        return False
    st._write_json(path, {"at": now, "pid": os.getpid()})
    return True


def should_start(base=None) -> bool:
    kind = platform_kind()
    if kind is None or not st.pill_enabled() or not autostart_on():
        return False
    if kind == "macos" and not Path(mac_python()).exists():
        return False   # no pill venv yet: `voicemode-switch pill on` makes it
    return not st.pill_closed(base) and not backend_alive(base)


def data_dir(base=None) -> Path:
    raw = st.setting(DATA_DIR_SETTING) or ""
    if raw:
        return Path(os.path.expanduser(os.path.expandvars(raw)))
    root = os.environ.get("VOICEMODE_NATIVE_ROOT")
    if root:
        return Path(root) / "pill"   # where voicemode_native.py has always kept it
    return st._base(base) / "window"


def backend_python() -> str:
    return os.environ.get(PYTHON_SETTING) or sys.executable


def mac_python() -> str:
    return os.environ.get(PYTHON_SETTING) or str(Path.home() / ".voicemode" / "pill-venv" / "bin" / "python")


def windows_powershell() -> str:
    system = os.environ.get("SystemRoot", r"C:\Windows")
    return str(Path(system) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")


def _ps_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _wsl_windows_path(path: Path) -> str:
    return subprocess.run(["wslpath", "-w", str(path)], capture_output=True, text=True,
                          check=True, timeout=10).stdout.strip()


def start_command(kind: str, base=None) -> list[str]:
    script = HERE / "pill-windows.ps1"
    if kind == "windows":
        return [windows_powershell(), "-STA", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-WindowStyle", "Hidden", "-File", str(script),
                "-BackendExe", backend_python(),
                "-BackendArgs", subprocess.list2cmdline(["-m", "voice_mode.voice_pill", "--stdio"]),
                "-DataDir", str(data_dir(base))]
    if kind == "wsl":
        backend = f'"{backend_python()}" "{HERE / "voice_pill.py"}" --stdio'
        args = ",".join(_ps_quote(a) for a in (
            "-STA", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
            f'"{_wsl_windows_path(script)}"', "-BackendCommand", f'"{backend.replace(chr(34), chr(92) + chr(34))}"'))
        # Start-Process: the window is a Windows process of its own, not a child of this
        # WSL interop call, so it outlives the voice server that started it.
        return ["powershell.exe", "-NoProfile", "-Command",
                f"Start-Process powershell.exe -WindowStyle Hidden -ArgumentList {args}"]
    if kind == "macos":
        return [mac_python(), str(HERE / "voice_pill_mac.py"),
                "--backend", f'"{backend_python()}" "{HERE / "voice_pill.py"}" --stdio']
    raise ValueError(kind)


def _log_file(base):
    return open(st._base(base) / "autostart.log", "ab")


def mac_window_log() -> Path:
    """The Mac window's own log, where voicemode-mcp used to send it."""
    return st.upstream_home() / "logs" / "pill-window.log"


def spawn(command: list[str], kind: str, base=None):
    """Start the window detached. On Windows, break away from the server's Job Object
    where the job allows it, so the window outlives this server; else start it plainly
    (it then lives as long as the job, and the next turn of another session restarts it).
    The start is logged in autostart.log. The Mac window writes its own log to stderr,
    which goes to mac_window_log(); the Windows window keeps its log in its data folder."""
    with _log_file(base) as log:
        log.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} start: {json.dumps(command)}\n".encode())
        log.flush()
        if kind == "macos":
            path = mac_window_log()
            path.parent.mkdir(parents=True, exist_ok=True)
            log.write(f"window log: {path}\n".encode())
            with open(path, "ab") as out:
                return subprocess.Popen(command, start_new_session=True, stdin=subprocess.DEVNULL,
                                        stdout=out, stderr=out, close_fds=True)
        common = dict(stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True)
        if kind == "windows":
            flags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
            try:
                return subprocess.Popen(command, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, **common)
            except OSError as e:
                log.write(f"breakaway refused ({e}); starting inside the job\n".encode())
                return subprocess.Popen(command, creationflags=flags, **common)
        return subprocess.Popen(command, start_new_session=True, **common)


def ensure(base=None) -> bool:
    """Bring the window up if it should be up. True if a start was attempted."""
    if not should_start(base) or not _launch_due(base):
        return False
    kind = platform_kind()
    spawn(start_command(kind, base), kind, base)
    return True
