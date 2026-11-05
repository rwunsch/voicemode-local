"""Linux unit tests for scripts/windows/voicemode_native.py, the native Windows launcher.

Logic only: port choice, settings, start order, lease bookkeeping, runtime selection,
and the --host defaults of the two speech servers. Nothing here starts a speech
service, binds a fixed port or touches ~/.voicemode: HOME is a temp folder.
The real thing on Windows is tests/test_voicemode_native_windows.py.
"""
import importlib.util
import json
import re
import socket
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "windows" / "voicemode_native.py"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / ".voicemode").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    for key in ("VOICEMODE_PILL", "VOICEMODE_PTT_ENABLED", "VOICEMODE_NATIVE_ROOT",
                "VOICEMODE_NATIVE_TTS_PORT", "VOICEMODE_NATIVE_STT_PORT", "VOICEMODE_BASE_DIR",
                "FELIX_HOME", "FELIX_VOICE_ROOT"):
        monkeypatch.delenv(key, raising=False)
    return h


@pytest.fixture
def vn(home):
    return _load(SCRIPT, "voicemode_native_under_test")


def _env_file(home, text):
    (home / ".voicemode" / "voicemode.env").write_text(text, encoding="utf-8")


# -- ports ------------------------------------------------------------------

def test_port_ranges_avoid_the_live_setup_and_felix(vn):
    live = {8880, 8881, 8882, 2022, 9000}
    felix = set(range(18880, 18900)) | set(range(12022, 12042))
    assert live <= vn.RESERVED and felix <= vn.RESERVED
    for kind in ("tts", "stt"):
        ports = set(vn.PORTS[kind])
        assert ports and not ports & vn.RESERVED
        assert min(ports) >= 1024 and max(ports) <= 65535
    assert not set(vn.PORTS["tts"]) & set(vn.PORTS["stt"])


def test_port_free_sees_a_listener(vn):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        assert vn.port_free(port) is False
    assert vn.port_free(port) is True


def test_choose_port_skips_taken_and_busy_ports(vn, monkeypatch):
    first, second, third = vn.PORTS["tts"][:3]
    monkeypatch.setattr(vn, "port_free", lambda p: p != first)
    assert vn.choose_port("tts", taken=set()) == second
    assert vn.choose_port("tts", taken={second}) == third


def test_choose_port_tries_the_setting_first(vn, home, monkeypatch):
    monkeypatch.setattr(vn, "port_free", lambda p: True)
    _env_file(home, "VOICEMODE_NATIVE_STT_PORT=23456\n")
    assert vn.port_candidates("stt")[0] == 23456
    assert vn.choose_port("stt", taken=set()) == 23456
    monkeypatch.setenv("VOICEMODE_NATIVE_STT_PORT", "24000")
    assert vn.choose_port("stt", taken=set()) == 24000


@pytest.mark.parametrize("value", ["8880", "2022", "9000", "18880", "12022", "80", "abc", "70000"])
def test_choose_port_refuses_a_reserved_or_bad_setting(vn, monkeypatch, value):
    monkeypatch.setenv("VOICEMODE_NATIVE_TTS_PORT", value)
    with pytest.raises(vn.VoiceError):
        vn.port_candidates("tts")


def test_choose_port_reports_when_every_port_is_busy(vn, monkeypatch):
    monkeypatch.setattr(vn, "port_free", lambda p: False)
    with pytest.raises(vn.VoiceError, match="No free local port"):
        vn.choose_port("stt", taken=set())


# -- settings ---------------------------------------------------------------

def test_setting_environment_beats_voicemode_env(vn, home, monkeypatch):
    _env_file(home, "VOICEMODE_PILL=true\r\nVOICEMODE_PTT_ENABLED=true\r\n")
    assert vn.optional_services() == ["pill", "ptt"]
    monkeypatch.setenv("VOICEMODE_PILL", "false")
    assert vn.optional_services() == ["ptt"]
    monkeypatch.setenv("VOICEMODE_PTT_ENABLED", "false")
    assert vn.optional_services() == []


def test_optional_services_are_off_unless_enabled(vn, home):
    assert vn.optional_services() == []
    _env_file(home, "VOICEMODE_PILL=yes\nVOICEMODE_PTT_ENABLED=1\n")
    assert vn.optional_services() == []  # only the word true, as the pill reads it


def test_native_root_default_and_override(vn, home, tmp_path, monkeypatch):
    monkeypatch.setattr(vn, "WINDOWS", True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    assert vn.native_root() == (tmp_path / "Local" / "voicemode-local").resolve()
    monkeypatch.setenv("VOICEMODE_NATIVE_ROOT", str(tmp_path / "elsewhere"))
    assert vn.native_root() == (tmp_path / "elsewhere").resolve()


@pytest.mark.parametrize("felix_var", [None, "FELIX_HOME", "FELIX_VOICE_ROOT"])
def test_native_root_never_inside_felix(vn, home, tmp_path, monkeypatch, felix_var):
    felix = home / ".felix" if felix_var is None else tmp_path / "felixdir"
    if felix_var:
        monkeypatch.setenv(felix_var, str(felix))
    monkeypatch.setenv("VOICEMODE_NATIVE_ROOT", str(felix / "voice" / "x"))
    with pytest.raises(vn.VoiceError, match="FELIX"):
        vn.native_root()


def test_runtime_environment_points_at_loopback_ports(vn, tmp_path, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:1")
    monkeypatch.setenv("VOICEMODE_TTS_BASE_URLS", "http://127.0.0.1:8880/v1")
    monkeypatch.setenv("VOICEMODE_DEBUG", "true")
    env = vn.runtime_environment(tmp_path / "rt", {"tts": 28881, "stt": 22023})
    assert env["VOICEMODE_TTS_BASE_URLS"] == "http://127.0.0.1:28881/v1"
    assert env["VOICEMODE_STT_BASE_URLS"] == "http://127.0.0.1:22023/v1"
    assert env["VOICEMODE_STT_AUDIO_FORMAT"] == "wav"
    assert "HTTPS_PROXY" not in env and env["NO_PROXY"].startswith("127.0.0.1")
    assert env["VOICEMODE_DEBUG"] == "true"        # the user's own settings stay
    assert env["HOME"] == str(Path.home())          # a personal install: the user's home
    assert env["PYTHONUTF8"] == "1"


# -- services ---------------------------------------------------------------

def test_service_commands_bind_loopback(vn, tmp_path, monkeypatch):
    monkeypatch.setattr(vn, "WINDOWS", True)
    for kind in ("tts", "stt"):
        command, marker = vn.service_command(kind, tmp_path, 28880)
        assert command[command.index("--host") + 1] == "127.0.0.1"
        assert command[command.index("--port") + 1] == "28880"
        assert marker and any(marker in part for part in command)


def test_no_os_kill_zero_anywhere_in_the_launcher():
    import ast
    calls = [node for node in ast.walk(ast.parse(SCRIPT.read_text(encoding="utf-8")))
             if isinstance(node, ast.Call) and ast.unparse(node.func) == "os.kill"]
    assert calls  # POSIX stop still signals; only the probe with 0 is banned
    assert not [c for c in calls if isinstance(c.args[1], ast.Constant) and c.args[1].value == 0]


class FakeWorld:
    """Fakes process creation and inspection, and records what starts in which order."""

    def __init__(self, vn, monkeypatch, root):
        self.vn, self.root, self.events, self.commands, self.next_pid = vn, root, [], {}, 1000
        self.commands[vn.os.getpid()] = "python voicemode_native.py launch"
        monkeypatch.setattr(vn, "spawn", self.spawn)
        monkeypatch.setattr(vn, "process_command", lambda pid: self.commands.get(pid))
        monkeypatch.setattr(vn, "process_tree", lambda pid: {pid})
        monkeypatch.setattr(vn, "listener_pids", self.listeners)
        monkeypatch.setattr(vn, "check_service", lambda kind, port: self.events.append(f"ready:{kind}"))
        monkeypatch.setattr(vn, "start_watchdog", lambda root: self.events.append("watchdog"))
        monkeypatch.setattr(vn, "port_free", lambda p: True)
        monkeypatch.setattr(vn, "kill_tree", self.kill)
        monkeypatch.setattr(vn, "services_job", lambda root: None)
        monkeypatch.setattr(vn, "connector_job", lambda: None)
        monkeypatch.setattr(vn, "connector_options", lambda: {})
        monkeypatch.setattr(vn, "background_options", lambda: {})

    def spawn(self, command, job=None, **kwargs):
        lease = self.root / "run" / "clients" / str(self.vn.os.getpid())
        self.events.append(("lease" if lease.exists() else "NO-LEASE"))
        pid, self.next_pid = self.next_pid, self.next_pid + 1
        self.commands[pid] = " ".join(map(str, command))
        self.events.append(f"spawn:{self.kind_of(command)}")
        return FakeProcess(pid)

    def kind_of(self, command):
        text = " ".join(map(str, command))
        for kind, needle in (("tts", "kokoro-onnx-server"), ("stt", "whisper-server"),
                             ("pill", "pill-windows.ps1"), ("ptt", "ptt-listener-windows.ps1"),
                             ("voice-mode", "voice-mode")):
            if needle in text:
                return kind
        return text

    def listeners(self, port):
        return {pid for pid, cmd in self.commands.items() if f"--port {port}" in cmd}

    def kill(self, pid):
        self.events.append(f"kill:{self.kind_of(self.commands.get(pid, '').split())}")
        self.commands.pop(pid, None)


class FakeProcess:
    def __init__(self, pid):
        self.pid = pid

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


def _fake_runtime(vn, root):
    runtime = root / "runtime" / "abc"
    (runtime / "services" / "whisper").mkdir(parents=True)
    return runtime


def _non_events(events):
    return [e for e in events if e not in ("lease", "NO-LEASE")]


@pytest.mark.parametrize("enabled", [[], ["pill"], ["pill", "ptt"]])
def test_start_order(vn, home, tmp_path, monkeypatch, enabled):
    root = tmp_path / "root"
    world = FakeWorld(vn, monkeypatch, root)
    runtime = _fake_runtime(vn, root)
    monkeypatch.setattr(vn, "WINDOWS", True)
    monkeypatch.setattr(vn, "configured_runtime", lambda root, expected: (runtime, True))
    monkeypatch.setattr(vn, "check_models", lambda runtime: None)
    monkeypatch.setattr(vn, "optional_services", lambda: list(enabled))
    assert vn.launch(root) == 0
    assert "NO-LEASE" not in world.events          # the lease exists before anything starts
    expected = (["watchdog", "spawn:tts", "spawn:stt"] + [f"spawn:{k}" for k in enabled]
                + ["ready:tts", "ready:stt", "spawn:voice-mode"])
    assert _non_events(world.events)[:len(expected)] == expected
    # The last connector left: every service it started is stopped, the lease is gone.
    stops = [e for e in world.events if e.startswith("kill:")]
    assert sorted(stops) == sorted(["kill:tts", "kill:stt"] + [f"kill:{k}" for k in enabled])
    assert not list((root / "run" / "clients").iterdir())


def test_services_are_reused_not_restarted(vn, home, tmp_path, monkeypatch):
    root = tmp_path / "root"
    world = FakeWorld(vn, monkeypatch, root)
    runtime = _fake_runtime(vn, root)
    monkeypatch.setattr(vn, "WINDOWS", True)
    env = vn.runtime_environment(runtime)
    first = vn.ensure_services(root, runtime, env, optional=[])
    world.events.clear()
    second = vn.ensure_services(root, runtime, env, optional=[])
    assert first == second
    assert not [e for e in world.events if e.startswith("spawn:")]


def test_two_kinds_never_share_a_port(vn, home, tmp_path, monkeypatch):
    root = tmp_path / "root"
    FakeWorld(vn, monkeypatch, root)
    monkeypatch.setattr(vn, "WINDOWS", True)
    ports = vn.ensure_services(root, _fake_runtime(vn, root), {}, optional=[])
    assert ports["tts"] in vn.PORTS["tts"] and ports["stt"] in vn.PORTS["stt"]


# -- leases -----------------------------------------------------------------

def test_live_leases_keeps_live_and_removes_the_rest(vn, tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    clients = run_dir / "clients"
    clients.mkdir(parents=True)
    commands = {
        11: r"C:\rt\python.exe C:\repo\scripts\windows\voicemode_native.py launch",
        12: r"C:\rt\python.exe C:\repo\scripts\windows\voicemode_native.py serve",
        31: r"C:\rt\.venv\Scripts\voice-mode.exe",
        41: "notepad.exe",
    }
    monkeypatch.setattr(vn, "process_command", lambda pid: commands.get(pid))
    for name in ("11", "12", "21", "41"):
        (clients / name).write_text("")
    (clients / "30").write_text(json.dumps({"child": 31}))  # launcher gone, its voice-mode lives
    (clients / "not-a-pid").write_text("")
    (clients / ".lease-being-written").write_text("")
    assert sorted(vn.live_leases(run_dir)) == [11, 12, 30]
    assert sorted(p.name for p in clients.iterdir()) == [".lease-being-written", "11", "12", "30"]


def test_release_stops_services_only_after_the_last_connector(vn, tmp_path, monkeypatch):
    root = tmp_path / "root"
    clients = root / "run" / "clients"
    clients.mkdir(parents=True)
    me = vn.os.getpid()
    other = me + 1
    (clients / str(me)).write_text("")
    (clients / str(other)).write_text("")
    alive = {other: "python voicemode_native.py launch"}
    monkeypatch.setattr(vn, "process_command", lambda pid: alive.get(pid))
    stopped = []
    monkeypatch.setattr(vn, "stop_recorded", lambda run_dir, state: stopped.append(state))
    vn.atomic_json(root / "run" / "state.json", {"tts": {"pid": 5, "port": 28880, "marker": "m"}})
    vn.release(root)
    assert stopped == [] and not (clients / str(me)).exists()
    alive.clear()
    vn.release(root)
    assert stopped == [{"tts": {"pid": 5, "port": 28880, "marker": "m"}}]


def test_owned_needs_marker_and_port(vn, monkeypatch):
    monkeypatch.setattr(vn, "process_command", lambda pid: "python kokoro-onnx-server.py --port 28880")
    assert vn.owned({"pid": 5, "port": 28880, "marker": "kokoro-onnx-server.py"})
    assert not vn.owned({"pid": 5, "port": 28881, "marker": "kokoro-onnx-server.py"})
    assert not vn.owned({"pid": 5, "port": 28880, "marker": "whisper"})
    assert vn.owned({"pid": 5, "port": None, "marker": "kokoro-onnx-server.py"})  # pill, ptt: no port
    assert not vn.owned(None) and not vn.owned({"pid": 0, "port": 1, "marker": "x"})


# -- runtime selection ------------------------------------------------------

def test_runtime_inputs_cover_lock_servers_and_every_patch(vn):
    names = {p.relative_to(REPO).as_posix() for p in vn.runtime_inputs()}
    manifest = json.loads((REPO / "patches" / "manifest.windows.json").read_text(encoding="utf-8"))
    listed = {f"patches/{s.get('patch') or s.get('copy')}" for s in manifest["steps"]}
    assert {"scripts/windows/dependencies.json", "scripts/windows/requirements.lock",
            "kokoro-onnx-server.py", "patches/apply.py", "patches/manifest.windows.json",
            "patches/patch_no_ffmpeg_gate.py", "patches/patch_load_scipy_early.py"} <= names
    assert listed <= names
    data, digest = vn.manifest()
    assert re.fullmatch(r"[0-9a-f]{64}", digest)


def test_setup_patches_with_the_native_windows_list():
    source = SCRIPT.read_text(encoding="utf-8")
    assert '"--native-windows"' in source


def test_dependencies_pin_url_size_and_hash(vn):
    data, _ = vn.manifest()
    for item in [*data["models"], data["whisper"]]:
        assert item["url"].startswith("https://")
        assert isinstance(item["size"], int) and item["size"] > 0
        assert re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
    lock = (REPO / "scripts" / "windows" / "requirements.lock").read_text(encoding="utf-8")
    pins = re.findall(r"^([A-Za-z0-9_.-]+)==", lock, flags=re.M)
    assert "voice-mode" in pins and "psutil" in pins and "kokoro-onnx" in pins
    assert lock.count("--hash=sha256:") >= len(pins)


def test_configured_runtime_checks_the_record(vn, tmp_path):
    root = tmp_path / "root"
    (root / "runtime").mkdir(parents=True)
    with pytest.raises(vn.VoiceError, match="not set up"):
        vn.configured_runtime(root, "d1")
    vn.atomic_json(root / "active.json", {"manifest": "d1", "runtime": "../escape"})
    with pytest.raises(vn.VoiceError, match="Invalid"):
        vn.configured_runtime(root, "d1")
    runtime = root / "runtime" / "d1abc-x"
    runtime.mkdir()
    vn.atomic_json(root / "active.json", {"manifest": "d1", "runtime": runtime.name})
    with pytest.raises(vn.VoiceError, match="incomplete"):
        vn.configured_runtime(root, "d1")
    vn.atomic_json(runtime / ".complete.json", {"manifest": "d1", "layout": vn.LAYOUT})
    with pytest.raises(vn.VoiceError, match="missing"):
        vn.configured_runtime(root, "d1")
    for name in vn.REQUIRED_FILES:
        (runtime / name).parent.mkdir(parents=True, exist_ok=True)
        (runtime / name).write_text("")
    assert vn.configured_runtime(root, "d1") == (runtime.resolve(), True)
    assert vn.configured_runtime(root, "d2") == (runtime.resolve(), False)


def test_atomic_json_leaves_no_temp_file(vn, tmp_path):
    folder = tmp_path / "records"
    vn.atomic_json(folder / "active.json", {"runtime": "x"})
    assert [p.name for p in folder.iterdir()] == ["active.json"]
    assert json.loads((folder / "active.json").read_text()) == {"runtime": "x"}


# -- --host on the two speech servers ---------------------------------------

def _serve_once(module, monkeypatch, server_attr, argv):
    bound = []

    class FakeServer:
        def __init__(self, address, handler):
            bound.append(address)

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            pass

    monkeypatch.setattr(module, server_attr, FakeServer)
    monkeypatch.setattr(sys, "argv", argv)
    module.main()
    return bound[0]


@pytest.fixture
def kokoro(monkeypatch):
    module = _load(REPO / "kokoro-onnx-server.py", "kokoro_onnx_server_under_test")
    monkeypatch.setattr(module, "get_kokoro", lambda: None)
    monkeypatch.setattr(module, "get_kokoro_de", lambda: None)
    return module


def test_kokoro_host_defaults_to_todays_bind(kokoro, monkeypatch):
    assert _serve_once(kokoro, monkeypatch, "HTTPServer", ["k", "--port", "18"]) == ("0.0.0.0", 18)


def test_kokoro_host_loopback(kokoro, monkeypatch):
    argv = ["k", "--host", "127.0.0.1", "--port", "19"]
    assert _serve_once(kokoro, monkeypatch, "HTTPServer", argv) == ("127.0.0.1", 19)


def test_whisper_proxy_host_default_and_loopback(monkeypatch):
    module = _load(REPO / "whisper-proxy.py", "whisper_proxy_under_test")
    assert _serve_once(module, monkeypatch, "ThreadingHTTPServer", ["w"]) == ("0.0.0.0", 2022)
    argv = ["w", "--host", "127.0.0.1", "--port", "20"]
    assert _serve_once(module, monkeypatch, "ThreadingHTTPServer", argv) == ("127.0.0.1", 20)
