"""scripts/windows/voicemode_native.py on real Windows: install, serve, launch, stop.

Run by tests/run_windows_transport_tests.sh. Installs a runtime from the lock into a
fresh folder under %TEMP% (short, for the 160-byte phoneme path limit), applies the
patches, starts the speech services on free ports, checks they answer on 127.0.0.1
and on none of the machine's other addresses, then stops them and checks that no
process is left. Then the MCP path: `launch` answers `initialize` over stdio, exits
when stdin closes, and leaves nothing behind when it is killed outright.

No audio and no microphone: nothing calls converse, speech or transcription.
HOME and USERPROFILE point into the temp folder, so ~/.voicemode is never touched.

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
    again =subprocess.run([sys.executable, str(SCRIPT), "setup"], env=install["env"],
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


def _start_launch(install):
    """`launch` under this Python, which lacks psutil: it restarts under the runtime's Python."""
    proc = subprocess.Popen([sys.executable, str(SCRIPT), "launch"], env=install["env"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
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
