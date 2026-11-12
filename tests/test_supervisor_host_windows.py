"""vml_supervisor.host on real Windows: a host started through Win32_Process.Create is outside the
job of the chat that asked for it, and outlives that job.

Run by tests/run_windows_transport_tests.sh. A helper Python stands in for a chat's connector: it is
put in a job of ours with KILL_ON_JOB_CLOSE (as the Copilot app does with a chat), and from there it
starts the host. The test then closes that job, which kills the helper, and checks what lives on.

test_start_host_via_cim_leaves_the_callers_job needs only ctypes: the started process is a
sleeper, checked with IsProcessInJob and GetExitCodeProcess.
test_host_started_via_cim_is_outside_the_callers_job_and_survives_it runs the real host with fake
speech services; the host needs psutil (leases and ownership), so it skips without it.
"""
import ctypes
import json
import random
import socket
import subprocess
import sys
import time
import uuid
from ctypes import wintypes
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Objects and CIM")

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

STILL_ACTIVE = 259

if sys.platform == "win32":
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _k32.IsProcessInJob.argtypes = (wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL))
    _k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    _k32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _k32.CloseHandle.argtypes = (wintypes.HANDLE,)

# The stand-in connector: started inside the "chat" job; runs one call, prints it, waits on stdin.
CONNECTOR = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
from vml_supervisor import host
from vml_supervisor.profile import HostProfile
profile = HostProfile.from_file(sys.argv[2])
if sys.argv[3] == "start_host":
    result = host.start_host(profile)
    print(json.dumps({"code": result.code, "detail": result.detail, "pid": result.data.get("pid")}), flush=True)
else:
    ports, results = host.ensure(profile, os.getpid())
    print(json.dumps({"ports": ports, "codes": [r.code for r in results],
                      "details": [r.detail for r in results]}), flush=True)
sys.stdin.read()
"""

HOST_SCRIPT = ("import sys; sys.path.insert(0, sys.argv[1]); from vml_supervisor import host; "
               "sys.exit(host.main(['host', sys.argv[-1]]))")

FAKE_SERVICE = """
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
port = int(sys.argv[sys.argv.index("--port") + 1])
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass
    def do_GET(self):
        body = json.dumps({"status": "ok", "data": [{"id": "kokoro-onnx"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
HTTPServer(("127.0.0.1", port), Handler).serve_forever()
"""


def _process(pid, access=0x1000 | 0x0001):   # QUERY_LIMITED_INFORMATION | TERMINATE
    handle = _k32.OpenProcess(access, False, pid)
    return handle or None


def _alive(pid) -> bool:
    handle = _process(pid)
    if handle is None:
        return False
    try:
        code = wintypes.DWORD()
        return bool(_k32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == STILL_ACTIVE
    finally:
        _k32.CloseHandle(handle)


def _in_job(pid, job_handle) -> bool:
    handle = _process(pid)
    assert handle, ctypes.WinError(ctypes.get_last_error())
    try:
        inside = wintypes.BOOL()
        assert _k32.IsProcessInJob(handle, job_handle, ctypes.byref(inside)), ctypes.WinError(ctypes.get_last_error())
        return bool(inside.value)
    finally:
        _k32.CloseHandle(handle)


def _terminate(pid) -> None:
    handle = _process(pid)
    if handle:
        _k32.TerminateProcess(handle, 1)
        _k32.CloseHandle(handle)


def _free_pair():
    for _ in range(500):
        port = random.randrange(30000, 60000)
        ok = True
        for candidate in (port, port + 1):
            with socket.socket() as probe:
                try:
                    probe.bind(("127.0.0.1", candidate))
                except OSError:
                    ok = False
        if ok:
            return port, port + 1
    raise RuntimeError("no two consecutive free ports")


def _profile(tmp_path, host_command, **over):
    from vml_supervisor.profile import HostProfile, PortOverride, ServiceSpec
    fake = tmp_path / "fake_service.py"
    fake.write_text(FAKE_SERVICE)
    root = tmp_path / "voice"
    runtime = root / "runtime"
    runtime.mkdir(parents=True)
    (runtime / ".complete.json").write_text("{}")
    tts, stt = _free_pair(), _free_pair()
    fields = dict(
        name="test", root=root, run_dir=root / "run", runtime_dir=runtime, python=Path(sys.executable),
        working_dir=root / "work", env={},
        services={"tts": ServiceSpec([sys.executable, str(fake), "tts", "--port", "{port}"], f"{fake.name} tts",
                                     tts, "/v1/models", "kokoro"),
                  "stt": ServiceSpec([sys.executable, str(fake), "stt", "--port", "{port}"], f"{fake.name} stt",
                                     stt, "/health", "whisper")},
        helpers={}, reserved_ports=(), port_override=PortOverride("none"),
        start_timeout=60.0, stop_timeout=10.0, watch_interval=1.0, restart_limit=(3, 300.0),
        lease_markers=("vml-test-connector",), voice_server_marker="vml-test-voice-server",
        host_command=host_command, host_marker="vml-test-host", watch_command=[], watch_marker="",
        job_name=f"Local\\vml-test-{uuid.uuid4().hex}", host_start_grace=30.0)
    fields.update(over)
    profile = HostProfile(**fields)
    profile_file = tmp_path / "connector-profile.json"
    profile.to_file(profile_file)
    return profile, profile_file


def _connector_in_a_chat_job(profile_file, mode):
    """The helper in a KILL_ON_JOB_CLOSE job, like a chat's connector; its report and the job."""
    from vml_supervisor import winjob
    chat = winjob.Job()   # KILL_ON_JOB_CLOSE, no breakaway allowed: as strict as a chat's job
    helper, placed = winjob.spawn([sys.executable, "-c", CONNECTOR, str(REPO), str(profile_file), mode,
                                   "vml-test-connector"], chat, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  text=True, creationflags=subprocess.CREATE_NO_WINDOW)
    report = json.loads(helper.stdout.readline())
    assert _in_job(helper.pid, chat.handle), "the connector must be in the chat job for the check to mean anything"
    return chat, helper, report


def test_start_host_via_cim_leaves_the_callers_job(tmp_path):
    profile, profile_file = _profile(tmp_path, [sys.executable, "-c", "import time; time.sleep(120)", "vml-test-host"])
    chat, helper, report = _connector_in_a_chat_job(profile_file, "start_host")
    pid = report["pid"]
    print(f"[cim] {report}")
    try:
        assert report["code"] == "host_started", report
        assert not _in_job(pid, chat.handle)
        chat.close()   # the app unloads the chat: everything in its job ends
        helper.wait(30)
        time.sleep(1.0)
        assert _alive(pid), "the process Windows started for us ended with the chat's job"
    finally:
        chat.close()
        _terminate(pid)


def test_host_started_via_cim_is_outside_the_callers_job_and_survives_it(tmp_path):
    pytest.importorskip("psutil", reason="the host needs psutil for leases and ownership on Windows")
    from vml_supervisor import services
    from vml_supervisor.state import write_lease
    profile, profile_file = _profile(tmp_path, [sys.executable, "-c", HOST_SCRIPT, str(REPO), "vml-test-host"])
    # A second connector, outside the chat job, keeps a lease: the host must stay for it.
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)", "vml-test-connector"],
                             creationflags=subprocess.CREATE_NO_WINDOW)
    write_lease(profile, other.pid, {})
    chat = None
    try:
        chat, helper, report = _connector_in_a_chat_job(profile_file, "ensure")
        print(f"[host] {report}")
        assert set(report["ports"]) == {"tts", "stt"}, report
        host_pid = int((profile.run_dir / "host.pid").read_text())
        assert not _in_job(host_pid, chat.handle)
        chat.close()
        helper.wait(30)
        time.sleep(3 * profile.watch_interval)
        assert _alive(host_pid), (profile.run_dir / "watch.log").read_text()
        for kind, port in report["ports"].items():
            spec = profile.services[kind]
            assert services.check_service(spec.ready_kind, port, spec.ready_path), f"{kind} on {port} is gone"
    finally:
        if chat is not None:
            chat.close()
        print(services.stop(profile))
        other.kill()
