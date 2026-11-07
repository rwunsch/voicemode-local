"""scripts/windows/voicemode_native.py on real Windows: install, serve, launch, stop.

Run by tests/run_windows_transport_tests.sh. Installs a runtime from the lock into a
fresh folder under %TEMP% (short, for the 160-byte phoneme path limit), applies the
patches, starts the speech services on free ports, checks they answer on 127.0.0.1
and on none of the machine's other addresses, then stops them and checks that no
process is left. Then the MCP path: `launch` answers `initialize` over stdio, exits
when stdin closes, and leaves nothing behind when it is killed outright.

No audio and no microphone: nothing calls converse, speech or transcription.
HOME and USERPROFILE point into the temp folder, so ~/.voicemode is never touched.
PATH for everything started has no folder holding ffmpeg or ffprobe.

VML_MODEL_SOURCE, a folder laid out <sha256>/<file name> (FELIX's models folder is),
is read to seed the ~500 MB of pinned downloads instead of fetching them; setup still
checks each file's size and SHA-256. Nothing is written there.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="needs Windows")

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "windows" / "voicemode_native.py"
DEPENDENCIES = REPO / "scripts" / "windows" / "dependencies.json"
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _ours(base):
    """Processes whose command line names the temp folder or this copy of the launcher."""
    script = (
        "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine } | "
        "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress")
    out = subprocess.run(["powershell.exe", "-NoProfile", "-Command", script],
                         capture_output=True, text=True, timeout=60).stdout.strip()
    rows = json.loads(out) if out else []
    rows = rows if isinstance(rows, list) else [rows]
    needles = (str(base).lower(), str(SCRIPT).lower())
    return {r["ProcessId"]: r["CommandLine"] for r in rows
            if r["ProcessId"] != os.getpid() and any(n in r["CommandLine"].lower() for n in needles)}


def _wait_none_left(base, seconds):
    deadline = time.monotonic() + seconds
    left = _ours(base)
    while left and time.monotonic() < deadline:
        time.sleep(1)
        left = _ours(base)
    return left


def _seed(root):
    source = os.environ.get("VML_MODEL_SOURCE")
    if not source:
        return
    data = json.loads(DEPENDENCIES.read_text(encoding="utf-8"))
    for item in [*data["models"], data["whisper"]]:
        name = Path(item["path"]).name
        found = Path(source) / item["sha256"] / name
        if found.is_file():
            target = root / "downloads" / item["sha256"] / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(found, target)  # a copy: the source is only read


def _remove(path):
    for _ in range(20):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.5)
    shutil.rmtree(path)


@pytest.fixture(scope="module")
def install():
    base = Path(tempfile.mkdtemp(prefix="vmn-", dir=os.environ["TEMP"]))
    root, home = base / "r", base / "h"
    home.mkdir()
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("VOICEMODE_", "FELIX_")) and k != "CLAUDE_CODE_SESSION_ID"}
    env.update(HOME=str(home), USERPROFILE=str(home), VOICEMODE_NATIVE_ROOT=str(root),
               VOICEMODE_PILL="false", VOICEMODE_PTT_ENABLED="false", PYTHONUTF8="1")
    # No ffmpeg for anything we start, as on a machine without it: drop every PATH folder
    # holding ffmpeg or ffprobe. uv may share such a folder, so it is passed by full path.
    uv = shutil.which("uv")
    assert uv, "uv is needed on PATH to run this test"
    env["VOICEMODE_UV"] = uv
    env["PATH"] = os.pathsep.join(
        f for f in env.get("PATH", "").split(os.pathsep)
        if f and not any(Path(f.strip('"'), n).is_file() for n in ("ffmpeg.exe", "ffprobe.exe")))
    assert shutil.which("ffmpeg", path=env["PATH"]) is None
    assert shutil.which("ffprobe", path=env["PATH"]) is None
    try:
        _seed(root)
        started = time.monotonic()
        setup = subprocess.run([sys.executable, str(SCRIPT), "setup"], env=env, capture_output=True,
                               text=True, timeout=1800)
        print(f"\n[setup] exit {setup.returncode} in {time.monotonic() - started:.0f} s")
        assert setup.returncode == 0, setup.stdout + setup.stderr[-4000:]
        record = json.loads((root / "active.json").read_text(encoding="utf-8"))
        runtime = root / "runtime" / record["runtime"]
        yield {"base": base, "root": root, "home": home, "env": env, "runtime": runtime,
               "python": runtime / ".venv" / "Scripts" / "python.exe"}
    finally:
        left = _ours(base)
        for pid in left:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        _remove(base)
        assert not left, f"processes were still running at teardown: {left}"
        assert not base.exists()


def test_setup_installs_and_selects_a_patched_runtime(install):
    runtime, root = install["runtime"], install["root"]
    assert runtime.parent == root / "runtime"
    complete = json.loads((runtime / ".complete.json").read_text(encoding="utf-8"))
    assert complete["manifest"] == json.loads((root / "active.json").read_text(encoding="utf-8"))["manifest"]
    package = runtime / ".venv" / "Lib" / "site-packages" / "voice_mode"
    for name in ("control_transport.py", "voice_pill_state.py", "voice_barge.py", "tools/voice_barge_tool.py"):
        assert (package / name).is_file(), name
    assert (runtime / "services" / "whisper" / "whisper-server.exe").is_file()
    converse = (package / "tools" / "converse.py").read_text(encoding="utf-8")
    assert "FELIX: NO FFMPEG" in converse and "FELIX: LOAD SCIPY EARLY" in converse
    assert "FELIX: NO FFMPEG WARNING" in (package / "server.py").read_text(encoding="utf-8")
    assert (runtime / "services" / "kokoro-onnx-server.py").is_file()
    for item in json.loads(DEPENDENCIES.read_text(encoding="utf-8"))["models"]:
        assert os.stat(runtime / item["path"]).st_nlink >= 2  # a hard link, not a copy
    assert not (root / ".uv-cache").exists()

    def size(folder, skip=()):
        return sum(p.stat().st_size for p in folder.rglob("*")
                   if p.is_file() and not any(s in p.parts for s in skip))

    print(f"\n[sizes] runtime without model links {size(runtime, skip=('models',)):,} bytes; "
          f".venv {size(runtime / '.venv'):,}; whisper {size(runtime / 'services' / 'whisper'):,}; "
          f"downloads {size(root / 'downloads'):,}; files in .venv "
          f"{sum(1 for p in (runtime / '.venv').rglob('*') if p.is_file()):,}")
    seen, total = set(), 0
    for path in root.rglob("*"):
        if path.is_file():
            info = path.stat()
            if (info.st_dev, info.st_ino) not in seen:  # a hard-linked model counts once
                seen.add((info.st_dev, info.st_ino))
                total += info.st_size
    print(f"[sizes] root after one setup {total:,} bytes in {len(seen):,} files; "
          f"uv Python {size(root / 'python'):,}")
    assert (root / "python").is_dir()  # uv's Python lives under the root
    again = subprocess.run([sys.executable, str(SCRIPT), "setup"], env=install["env"],
                           capture_output=True, text=True, timeout=300)
    assert again.returncode == 0 and "Already installed" in again.stdout, again.stdout + again.stderr


def _other_addresses():
    found = set()
    for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
        found.add(info[4][0])
    return sorted(a for a in found if not a.startswith("127."))


def _listening_addresses(port):
    out = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True).stdout
    return {line.split()[1].rsplit(":", 1)[0] for line in out.splitlines()
            if "LISTENING" in line and line.split()[1].endswith(f":{port}")}


def _read_line(stream, seconds):
    box = []
    reader = threading.Thread(target=lambda: box.append(stream.readline()), daemon=True)
    reader.start()
    reader.join(seconds)
    return box[0] if box else None


def test_services_answer_on_loopback_only_then_stop(install):
    proc = subprocess.Popen([str(install["python"]), str(SCRIPT), "serve"], env=install["env"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        line = _read_line(proc.stdout, 180)
        assert line, f"serve printed no ports; stderr: {proc.stderr.read() if proc.poll() is not None else ''}"
        ports = json.loads(line)
        print(f"\n[serve] ports {ports}")
        assert 28880 <= ports["tts"] < 28900 and 22022 <= ports["stt"] < 22042
        with NO_PROXY.open(f"http://127.0.0.1:{ports['tts']}/v1/models", timeout=5) as r:
            assert any(m["id"] == "kokoro-onnx" for m in json.load(r)["data"])
        with NO_PROXY.open(f"http://127.0.0.1:{ports['stt']}/health", timeout=5) as r:
            assert json.load(r)["status"] == "ok"
        others = _other_addresses()
        print(f"[serve] other addresses tried: {others}")
        assert others, "no non-loopback address to try: the check would prove nothing"
        for port in ports.values():
            assert _listening_addresses(port) == {"127.0.0.1"}, _listening_addresses(port)
            for address in others:
                with socket.socket() as probe:
                    probe.settimeout(2)
                    assert probe.connect_ex((address, port)) != 0, f"{address}:{port} answered"
        running = _ours(install["base"])
        print(f"[serve] our processes while serving: {len(running)}")
        # A crashed TTS service: the watchdog starts it again on the same port.
        state = json.loads((install["root"] / "run" / "state.json").read_text(encoding="utf-8"))
        crashed = state["tts"]["pid"]
        subprocess.run(["taskkill", "/PID", str(crashed), "/T", "/F"], capture_output=True)
        deadline, back = time.monotonic() + 90, False
        while time.monotonic() < deadline and not back:
            time.sleep(1)
            try:
                with NO_PROXY.open(f"http://127.0.0.1:{ports['tts']}/v1/models", timeout=2) as r:
                    back = r.status == 200
            except OSError:
                pass
        state = json.loads((install["root"] / "run" / "state.json").read_text(encoding="utf-8"))
        print(f"[serve] TTS killed (process {crashed}); back on port {ports['tts']}: {back}, "
              f"now process {state['tts']['pid']}")
        assert back and state["tts"]["pid"] != crashed and state["tts"]["port"] == ports["tts"]
        assert _listening_addresses(ports["tts"]) == {"127.0.0.1"}
        proc.stdin.close()
        assert proc.wait(60) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
    left = _wait_none_left(install["base"], 30)
    assert not left, left
    for port in ports.values():
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", port)) != 0


INITIALIZE = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {},
    "clientInfo": {"name": "vml-native-test", "version": "0"}}}) + "\n"


def _start_launch(install, stderr=subprocess.DEVNULL, env=None):
    """`launch` under this Python, which lacks psutil: it restarts under the runtime's Python."""
    proc = subprocess.Popen([sys.executable, str(SCRIPT), "launch"], env=env or install["env"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
                            text=True, encoding="utf-8")
    proc.stdin.write(INITIALIZE)
    proc.stdin.flush()
    started = time.monotonic()
    line = _read_line(proc.stdout, 180)
    assert line, "voice-mode gave no answer to initialize"
    answer = json.loads(line)
    print(f"\n[launch] initialize answered in {time.monotonic() - started:.1f} s by "
          f"{answer['result']['serverInfo']}")
    assert answer["id"] == 1 and "serverInfo" in answer["result"]
    return proc


def test_launch_answers_initialize_and_cleans_up_on_eof(install):
    proc = _start_launch(install)
    try:
        assert len(_ours(install["base"])) >= 4  # inner launcher, voice-mode, two services, watchdog
        proc.stdin.close()
        assert proc.wait(60) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
    left = _wait_none_left(install["base"], 30)
    assert not left, left


def test_killed_launcher_leaves_no_process(install):
    proc = _start_launch(install)
    before = _ours(install["base"])
    proc.kill()  # TerminateProcess: no cleanup code runs in the launcher
    proc.wait(30)
    left = _wait_none_left(install["base"], 45)
    print(f"[kill] {len(before)} of our processes before, {len(left)} after")
    assert not left, left


# -- the native switch and install.ps1 (task 8) --------------------------------
# Every switch test runs against a `serve` (a live lease) or a `launch` in this module's temp
# root and profile. Robert's %USERPROFILE%\.voicemode\voicemode.env and .claude.json are
# never touched: HOME and USERPROFILE point into the temp folder, and the claude CLI is a fake.

import ctypes
import contextlib
import hashlib
import re
from ctypes import wintypes

if sys.platform == "win32":
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _k32.OpenJobObjectW.restype = wintypes.HANDLE
    _k32.OpenJobObjectW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    _k32.IsProcessInJob.argtypes = (wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL))
    _k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    _k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _u32 = ctypes.WinDLL("user32", use_last_error=True)
    _ENUM = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _u32.EnumWindows.argtypes = (_ENUM, wintypes.LPARAM)
    _u32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    _u32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    _u32.IsWindowVisible.argtypes = (wintypes.HWND,)


def _alive(pid):
    handle = _k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        return bool(_k32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
    finally:
        _k32.CloseHandle(handle)


def _in_services_job(root, pid):
    name = "Local\\voicemode-local-services-" + hashlib.sha256(str(Path(root).resolve()).lower().encode()).hexdigest()[:16]
    job = _k32.OpenJobObjectW(0x4, False, name)  # JOB_OBJECT_QUERY
    assert job, f"no job named {name}"
    process = _k32.OpenProcess(0x1000, False, pid)
    try:
        inside = wintypes.BOOL()
        assert _k32.IsProcessInJob(process, job, ctypes.byref(inside))
        return bool(inside.value)
    finally:
        _k32.CloseHandle(process)
        _k32.CloseHandle(job)


def _visible_pill(pid):
    found = []

    def each(hwnd, _):
        owner = wintypes.DWORD()
        _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and _u32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(64)
            _u32.GetWindowTextW(hwnd, buf, 64)
            if buf.value == "Voice pill":
                found.append(hwnd)
        return True

    _u32.EnumWindows(_ENUM(each), 0)
    return found


def _wait(predicate, seconds, what):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.3)
    raise AssertionError(f"timed out after {seconds} s waiting for {what}")


def _switch(install, *args, env=None, python=None):
    """The native switch as a user runs it: this Python (no psutil), so main() restarts it under the runtime's."""
    done = subprocess.run([str(python or sys.executable), str(SCRIPT), *args], env=env or install["env"],
                          capture_output=True, text=True, encoding="utf-8", timeout=180)
    print(f"[switch {' '.join(args)}] exit {done.returncode}: {done.stdout.strip()!r} {done.stderr.strip()[-300:]!r}")
    return done


def _state(install):
    return json.loads((install["root"] / "run" / "state.json").read_text(encoding="utf-8"))


@contextlib.contextmanager
def _serving(install):
    proc = subprocess.Popen([str(install["python"]), str(SCRIPT), "serve"], env=install["env"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        assert _read_line(proc.stdout, 180), "serve printed no ports"
        yield proc
        proc.stdin.close()
        assert proc.wait(60) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
    left = _wait_none_left(install["base"], 30)
    assert not left, left


def test_barge_switch_starts_and_stops_the_trigger_in_the_services_job(install):
    with _serving(install):
        for cycle in range(2):
            done = _switch(install, "barge", "on")
            assert done.returncode == 0 and "started (process" in done.stdout, done.stdout + done.stderr
            pid = _state(install)["barge"]["pid"]
            time.sleep(4)  # it loads Silero, then waits for speech it never hears
            log = (install["root"] / "run" / "barge.log").read_text(encoding="utf-8", errors="replace")
            assert _alive(pid), log[-2000:]
            assert _in_services_job(install["root"], pid)
            status = _switch(install, "barge", "status")
            assert status.stdout.splitlines() == ["on", f"trigger: running (process {pid})"]
            # The same first line as voice_barge's own status, from the runtime.
            own = subprocess.run([str(install["python"]), "-m", "voice_mode.voice_barge", "status"],
                                 env=install["env"], capture_output=True, text=True, timeout=60)
            assert own.stdout.splitlines() == ["on"], own.stdout + own.stderr
            done = _switch(install, "barge", "off")
            assert done.returncode == 0 and f"Stopped the trigger (process {pid})" in done.stdout
            assert not _alive(pid) and "barge" not in _state(install)
            assert _switch(install, "barge", "status").stdout.splitlines() == ["off", "trigger: not running"]
            print(f"[barge] cycle {cycle + 1}: trigger {pid} started in the services job, then stopped")
    env_file = install["home"] / ".voicemode" / "voicemode.env"
    assert "VOICEMODE_VOICE_BARGE=false" in env_file.read_text(encoding="utf-8")


def _seed_webview2(root):
    version = re.search(r'\$WebView2Version = "([^"]+)"', (REPO / "pill-windows.ps1").read_text()).group(1)
    seed = Path(os.environ["LOCALAPPDATA"]) / "voicemode-local" / "webview2" / version
    target = root / "pill" / "webview2" / version
    if (seed / "WebView2Loader.dll").exists() and not target.exists():
        shutil.copytree(seed, target)  # read only from the WSL pill's folder


PILL_BACKEND_ENV = r'''
import json, subprocess, sys, psutil
pill = psutil.Process(int(sys.argv[1]))
backend = [p for p in pill.children(recursive=True) if "voice_mode.voice_pill" in " ".join(p.cmdline())]
env = backend[0].environ()
command = json.loads(env["VOICEMODE_BARGE_CONTROL_COMMAND"])
done = subprocess.run([*command, "status"], env=env, capture_output=True, text=True, timeout=120)
print(json.dumps({"command": command, "rc": done.returncode, "out": done.stdout, "err": done.stderr[-500:]}))
'''


def test_pill_switch_closes_the_window_through_its_stop_event(install):
    _seed_webview2(install["root"])
    with _serving(install):
        for cycle in range(2):
            done = _switch(install, "pill", "on")
            assert done.returncode == 0 and "started (process" in done.stdout, done.stdout + done.stderr
            # The test's environment says VOICEMODE_PILL=false: the switch says so, and starts it anyway.
            assert "Note: VOICEMODE_PILL=false" in done.stdout
            pid = _state(install)["pill"]["pid"]
            assert _in_services_job(install["root"], pid)
            # The window stays hidden while no voice session exists (serve has none): wait for
            # the backend it starts instead.
            window_log = install["root"] / "pill" / "pill-window.log"
            try:
                _wait(lambda: window_log.exists() and window_log.read_text(encoding="utf-8", errors="replace")
                      .count("backend started") == cycle + 1, 90, "the pill backend")
            except AssertionError:
                for log in (install["root"] / "pill" / "pill-window.log", install["root"] / "run" / "pill.log"):
                    if log.exists():
                        print(f"--- {log}\n" + log.read_text(encoding="utf-8", errors="replace")[-3000:])
                print(f"[pill] alive: {_alive(pid)}; ours: {_ours(install['base'])}")
                raise
            assert not _visible_pill(pid)  # no session, no window
            _wait(lambda: subprocess.run([str(install["python"]), "-c", PILL_BACKEND_ENV, str(pid)],
                                         capture_output=True).returncode == 0, 30, "the backend's environment")
            probe = subprocess.run([str(install["python"]), "-c", PILL_BACKEND_ENV, str(pid)],
                                   capture_output=True, text=True, timeout=180)
            result = json.loads(probe.stdout.strip().splitlines()[-1])
            print(f"[pill] the barge command from the backend's environment: {result}")
            assert result["command"][1:] == [str(SCRIPT), "barge"]
            assert result["rc"] == 0 and result["out"].splitlines()[1] == "trigger: not running", result
            assert result["out"].splitlines()[0] in ("on", "off")
            assert _switch(install, "pill", "status").stdout.splitlines() == ["on", f"window: running (process {pid})"]
            done = _switch(install, "pill", "off")
            assert done.returncode == 0 and f"Stopped the window (process {pid})" in done.stdout
            assert not _alive(pid) and "pill" not in _state(install)
            log = (install["root"] / "pill" / "pill-window.log").read_text(encoding="utf-8", errors="replace")
            assert log.count("stop requested") == cycle + 1, log[-2000:]  # closed by the event, not killed
            _wait(lambda: not [c for c in _ours(install["base"]).values() if "voice_pill" in c or "pill-windows" in c],
                  20, "the pill's processes to end")
            print(f"[pill] cycle {cycle + 1}: window {pid} and its backend started, then closed by its stop event")


# -- push-to-talk, native (task 7) ------------------------------------------------
# The listener only polls two key states; nothing here presses or synthesises a key.

def _transport():
    import importlib.util as iu
    spec = iu.spec_from_file_location("vml_ct_native_test", REPO / "patches" / "control_transport.py")
    module = iu.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _control_sock(install):
    """control.sock as voice-mode resolves it for the test's profile (no base dir set)."""
    return Path(install["home"]) / ".voicemode" / "control.sock"


def _command_line(pid):
    out = subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                          f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                         capture_output=True, text=True, timeout=60)
    return out.stdout.strip()


def _listener_ready(install, count):
    log = install["root"] / "run" / "ptt-listener.log"
    _wait(lambda: log.exists() and log.read_text(encoding="utf-8-sig", errors="replace")
          .count("native: sending through " + str(install["python"])) == count, 60, "the listener's start line")


def test_ptt_switch_starts_and_stops_the_listener_in_the_services_job(install):
    log = install["root"] / "run" / "ptt-listener.log"
    if log.exists():
        log.unlink()
    with _serving(install):
        for cycle in range(2):
            done = _switch(install, "ptt", "on")
            assert done.returncode == 0 and "started (process" in done.stdout, done.stdout + done.stderr
            pid = _state(install)["ptt"]["pid"]
            assert _in_services_job(install["root"], pid)
            _listener_ready(install, cycle + 1)
            assert _alive(pid)
            line = _command_line(pid)
            assert str(install["python"]) in line and "voice_mode.ptt_control_client --stdin" in line, line
            assert "-RelayHost" not in line, line
            # Its sender, the runtime's Python, is a child in the same job.
            children = [p for p, c in _ours(install["base"]).items() if "ptt_control_client" in c and p != pid]
            assert children and all(_in_services_job(install["root"], c) for c in children), children
            assert _switch(install, "ptt", "status").stdout.splitlines() == ["on", f"listener: running (process {pid})"]
            done = _switch(install, "ptt", "off")
            assert done.returncode == 0 and f"Stopped the listener (process {pid})" in done.stdout
            assert not _alive(pid) and "ptt" not in _state(install)
            assert _switch(install, "ptt", "status").stdout.splitlines() == ["off", "listener: not running"]
            print(f"[ptt] cycle {cycle + 1}: listener {pid} started in the services job, then stopped")
    env_file = install["home"] / ".voicemode" / "voicemode.env"
    assert "VOICEMODE_PTT_ENABLED=false" in env_file.read_text(encoding="utf-8")


def test_ptt_runtime_sender_reaches_the_pipe_voice_mode_serves(install):
    """The installed voice_mode.ptt_control_client, run as the launcher runs it, names the
    same pipe as the patched server for this profile and delivers the mapped command. The
    server is the transport's own, on that real pipe name; the listener is called through
    -SendAction, so no key is involved."""
    ct = _transport()
    server = ct.create_server(_control_sock(install))
    server.settimeout(10)
    got = []

    def accept():
        try:
            conn, _ = server.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(5)
            got.append(conn.recv(4096))

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    log = install["base"] / "ptt-send.log"
    try:
        done = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                               str(REPO / "ptt-listener-windows.ps1"), "-SenderExe", str(install["python"]),
                               "-LogPath", str(log), "-SendAction", "short_press"],
                              env=install["env"], capture_output=True, text=True, timeout=120)
        thread.join(15)
    finally:
        server.close()
    assert done.returncode == 0, done.stdout + done.stderr
    assert got == [b'{"command": "skip_forward"}\n'], (got, log.read_text(encoding="utf-8-sig"))


def test_ptt_launch_starts_the_listener_and_it_ends_with_the_launcher(install):
    log = install["root"] / "run" / "ptt-listener.log"
    if log.exists():
        log.unlink()
    env = dict(install["env"], VOICEMODE_PTT_ENABLED="true")
    proc = _start_launch(install, env=env)
    try:
        pid = _state(install)["ptt"]["pid"]
        assert _in_services_job(install["root"], pid)
        _listener_ready(install, 1)
        assert _alive(pid)
        # voice-mode got the control channel, without which the pipe never exists.
        records = [json.loads(f.read_text(encoding="utf-8")) for f in (install["root"] / "run" / "clients").iterdir()]
        assert any(r.get("control_channel") is True for r in records), records
        print(f"[ptt] launch started listener {pid} in the services job; control channel on")
        proc.stdin.close()
        assert proc.wait(60) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
    left = _wait_none_left(install["base"], 30)
    assert not left, left
    assert not _alive(pid)


def _call(proc, request_id, name, arguments):
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                                 "params": {"name": name, "arguments": arguments}}) + "\n")
    proc.stdin.flush()
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        line = _read_line(proc.stdout, max(1, deadline - time.monotonic()))
        assert line, f"no answer to tools/call {name}"
        message = json.loads(line)
        if message.get("id") == request_id:
            return "".join(c.get("text", "") for c in message["result"]["content"])
    raise AssertionError(f"no answer to tools/call {name}")


def test_voice_barge_tool_drives_the_native_switch(install):
    proc = _start_launch(install)
    try:
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        proc.stdin.flush()
        text = _call(proc, 2, "voice_barge", {"action": "on"})
        print(f"[tool] voice_barge on: {text!r}")
        assert "started (process" in text
        pid = _state(install)["barge"]["pid"]
        time.sleep(3)
        # Started by voice-mode, which sits in its connector's job: it must land in the services' job.
        assert _alive(pid) and _in_services_job(install["root"], pid)
        assert _call(proc, 3, "voice_barge", {"action": "status"}).splitlines() == [
            "on", f"trigger: running (process {pid})"]
        text = _call(proc, 4, "voice_barge", {"action": "off"})
        assert f"Stopped the trigger (process {pid})" in text and not _alive(pid)
        proc.stdin.close()
        assert proc.wait(60) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
    left = _wait_none_left(install["base"], 30)
    assert not left, left


FAKE_CLAUDE = r'''
import json, os, sys
args = sys.argv[1:]
with open(os.environ["VML_FAKE_CLAUDE_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\n")
path = os.path.join(os.environ["USERPROFILE"], ".claude.json")
try:
    data = json.load(open(path, encoding="utf-8"))
except FileNotFoundError:
    data = {}
servers = data.setdefault("mcpServers", {})
if args[:2] == ["mcp", "add"]:
    rest = args[2:]
    env, name = {}, None
    while rest and rest[0] != "--":
        item = rest.pop(0)
        if item == "--scope":
            rest.pop(0)
        elif item == "-e":
            key, value = rest.pop(0).split("=", 1)
            env[key] = value
        else:
            name = item
    command = rest[1:]
    servers[name] = {"type": "stdio", "command": command[0], "args": command[1:], "env": env}
elif args[:2] == ["mcp", "remove"]:
    servers.pop(args[-1], None)
json.dump(data, open(path, "w", encoding="utf-8"), indent=2)
'''


def _install_ps1(install, profile, *args):
    done = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                           str(REPO / "install.ps1"), "-Mode", "windows-native", *args],
                          env=profile["env"], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=900)
    print(f"\n[install.ps1 {' '.join(args)}] exit {done.returncode}\n{done.stdout[-3000:]}\n{done.stderr[-1500:]}")
    return done


def _calls(profile):
    path = profile["log"]
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def test_install_ps1_native_end_to_end_under_windows_powershell(install):
    """Last in this module: it ends with -Uninstall -RemoveData, which deletes the root."""
    base, root = install["base"], install["root"]
    home, bin_dir = base / "p", base / "bin"
    (home / ".voicemode").mkdir(parents=True)
    bin_dir.mkdir()
    (bin_dir / "fake_claude.py").write_text(FAKE_CLAUDE, encoding="utf-8")
    (bin_dir / "claude.cmd").write_text(f'@echo off\r\n"{sys.executable}" "%~dp0fake_claude.py" %*\r\n')
    claude_json = home / ".claude.json"
    claude_json.write_text(json.dumps({"projects": {"C:/x": {"a": 1}}, "mcpServers": {"other": {"command": "o"}}}))
    env = dict(install["env"], HOME=str(home), USERPROFILE=str(home), VML_FAKE_CLAUDE_LOG=str(base / "claude.log"))
    # VOICEMODE_UV stays: uv's folder here also holds ffmpeg, so it is off PATH (see install()).
    # The fake first, and no folder holding the real claude CLI.
    env["PATH"] = os.pathsep.join([str(bin_dir), *(f for f in env["PATH"].split(os.pathsep)
                                                   if f and not Path(f.strip('"'), "claude.exe").is_file())])
    profile = {"env": env, "log": base / "claude.log"}
    version = subprocess.run(["powershell.exe", "-NoProfile", "-Command", "$PSVersionTable.PSVersion.ToString()"],
                             capture_output=True, text=True).stdout.strip()
    print(f"\n[install.ps1] Windows PowerShell {version}")
    assert version.startswith("5.1")

    done = _install_ps1(install, profile, "-Pill", "-Barge")
    assert done.returncode == 0
    stable = root / "python" / "cpython-3.12-windows-x86_64-none" / "python.exe"
    assert stable.is_file()
    want = ["mcp", "add", "--scope", "user", "voicemode", "-e", f"VOICEMODE_NATIVE_ROOT={root}",
            "--", str(stable), str(SCRIPT), "launch"]
    assert _calls(profile) == [want]
    data = json.loads(claude_json.read_text(encoding="utf-8"))
    assert data["projects"] == {"C:/x": {"a": 1}} and data["mcpServers"]["other"] == {"command": "o"}
    entry = data["mcpServers"]["voicemode"]
    assert entry["command"] == str(stable) and entry["args"] == [str(SCRIPT), "launch"]
    for line in ("Root:", "Runtime:", "Disk:", "MCP entry: voicemode ->", "pill on|off|status", "-Uninstall"):
        assert line in done.stdout, line
    settings = (home / ".voicemode" / "voicemode.env").read_bytes()  # bytes: read_text drops the CR
    assert b"VOICEMODE_PILL=true\r\n" in settings and b"VOICEMODE_VOICE_BARGE=true\r\n" in settings

    # The registered command itself answers initialize; barge, now on in the file, starts with it.
    launch_env = {k: v for k, v in env.items() if k != "VOICEMODE_NATIVE_ROOT"}
    launch_env.update(entry["env"])
    launch_env.pop("VOICEMODE_PILL")  # as Claude Code would start it: no override in the environment
    proc = subprocess.Popen([entry["command"], *entry["args"]], env=launch_env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8")
    try:
        proc.stdin.write(INITIALIZE)
        proc.stdin.flush()
        line = _read_line(proc.stdout, 180)
        assert line and json.loads(line)["id"] == 1
        state = _state(install)
        print(f"[install.ps1] the entry's launch started: {sorted(state)}")
        assert {"tts", "stt", "barge", "pill"} <= set(state)
        proc.stdin.close()
        assert proc.wait(60) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
    assert not _wait_none_left(base, 45)

    # Again: the same entry is kept, with no claude call.
    assert _install_ps1(install, profile).returncode == 0
    assert len(_calls(profile)) == 1
    # A different entry is reported, not replaced, unless -Force.
    data = json.loads(claude_json.read_text(encoding="utf-8"))
    data["mcpServers"]["voicemode"] = {"type": "stdio", "command": "C:\\elsewhere\\voice.exe", "args": []}
    claude_json.write_text(json.dumps(data))
    done = _install_ps1(install, profile)
    assert done.returncode == 0 and "already exists and was not changed" in done.stdout and "elsewhere" in done.stdout
    assert len(_calls(profile)) == 1
    done = _install_ps1(install, profile, "-Force")
    assert done.returncode == 0
    assert _calls(profile)[1:] == [["mcp", "remove", "--scope", "user", "voicemode"], want]

    # Uninstall: the entry goes, the data stays; then -RemoveData deletes the root.
    done = _install_ps1(install, profile, "-Uninstall")
    assert done.returncode == 0 and root.is_dir()
    assert _calls(profile)[-1] == ["mcp", "remove", "--scope", "user", "voicemode"]
    data = json.loads(claude_json.read_text(encoding="utf-8"))
    assert "voicemode" not in data["mcpServers"] and data["mcpServers"]["other"] == {"command": "o"}
    done = _install_ps1(install, profile, "-Uninstall", "-RemoveData")
    assert done.returncode == 0 and not root.exists(), done.stdout
    assert (Path(os.environ["LOCALAPPDATA"]) / "voicemode-local").exists() == WSL_PILL_EXISTED
    real = Path(os.environ["USERPROFILE"]) / ".claude.json"
    assert str(root).lower() not in real.read_text(encoding="utf-8", errors="replace").lower()


WSL_PILL_EXISTED = sys.platform == "win32" and (Path(os.environ.get("LOCALAPPDATA", "")) / "voicemode-local").exists()
