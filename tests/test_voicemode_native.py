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
    for key in ("VOICEMODE_PILL", "VOICEMODE_PTT_ENABLED", "VOICEMODE_VOICE_BARGE", "VOICEMODE_NATIVE_ROOT",
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
    assert vn.native_root() == (tmp_path / "Local" / "voicemode-local-native").resolve()
    monkeypatch.setenv("VOICEMODE_NATIVE_ROOT", str(tmp_path / "Local" / "elsewhere"))
    assert vn.native_root() == (tmp_path / "Local" / "elsewhere").resolve()
    monkeypatch.setenv("VOICEMODE_NATIVE_ROOT", str(home / "vml"))
    assert vn.native_root() == (home / "vml").resolve()


def test_default_root_is_not_the_wsl_pills_folder(vn, home, tmp_path, monkeypatch):
    import re as _re
    text = (REPO / "pill-windows.ps1").read_text(encoding="utf-8")
    pill_name = _re.search(r'Join-Path \$env:LOCALAPPDATA "([^"]+)"', text).group(1)
    monkeypatch.setattr(vn, "WINDOWS", True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    root, pill = vn.native_root(), (tmp_path / "Local" / pill_name).resolve()
    assert root != pill and pill not in root.parents and root not in pill.parents


def test_root_outside_profile_is_refused_unless_allowed(vn, home, tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("VOICEMODE_NATIVE_ROOT", str(tmp_path / "shared"))
    with pytest.raises(vn.VoiceError, match="outside your profile"):
        vn.native_root()
    assert vn.native_root(allow_outside=True) == (tmp_path / "shared").resolve()
    rc = vn.main(["status"])
    assert rc == 1


def test_allow_flag_reaches_the_watchdog(vn, tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(vn, "process_command", lambda pid: None)
    monkeypatch.setattr(vn.subprocess, "Popen", lambda args, **kw: seen.append(args) or type("P", (), {"pid": 7})())
    monkeypatch.setattr(vn, "ALLOW_OUTSIDE", True)
    (tmp_path / "run").mkdir()
    vn.start_watchdog(tmp_path)
    assert seen[0][-2:] == ["watch", "--allow-root-outside-profile"]


def test_push_to_talk_is_refused_natively_until_task_7(vn, home, monkeypatch, capsys):
    monkeypatch.setattr(vn, "WINDOWS", True)
    _env_file(home, "VOICEMODE_PILL=true\nVOICEMODE_PTT_ENABLED=true\n")
    assert vn.optional_services() == ["pill"]
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and "push-to-talk" in err[0] and "8765" in err[0]
    _env_file(home, "VOICEMODE_PILL=true\n")
    assert vn.optional_services() == ["pill"]
    assert capsys.readouterr().err == ""


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
        # Fake pids are real numbers on this machine: no signal may ever reach them.
        monkeypatch.setattr(vn, "WINDOWS", True)
        monkeypatch.setattr(vn.os, "kill", self.no_signals)
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

    @staticmethod
    def no_signals(pid, sig):
        raise AssertionError(f"a test sent signal {sig} to real pid {pid}")

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
                             ("barge", "voice_mode.voice_barge"),
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
    (runtime / ".complete.json").write_text("{}")
    return runtime


def _non_events(events):
    return [e for e in events if e not in ("lease", "NO-LEASE")]


@pytest.mark.parametrize("enabled", [[], ["pill"], ["pill", "ptt"], ["barge"]])
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


# -- leases name their runtime; old runtimes are pruned ----------------------

def test_lease_records_its_runtime(vn, home, tmp_path, monkeypatch):
    root = tmp_path / "root"
    FakeWorld(vn, monkeypatch, root)
    runtime = _fake_runtime(vn, root)
    vn.ensure_services(root, runtime, {}, optional=[])
    lease = json.loads((root / "run" / "clients" / str(vn.os.getpid())).read_text())
    assert lease["runtime"] == str(runtime)
    state = json.loads((root / "run" / "state.json").read_text())
    assert state["tts"]["runtime"] == str(runtime) and state["tts"]["command"]


def test_a_runtime_replaced_meanwhile_is_not_started(vn, home, tmp_path, monkeypatch):
    root = tmp_path / "root"
    world = FakeWorld(vn, monkeypatch, root)
    runtime = _fake_runtime(vn, root)
    (runtime / ".complete.json").unlink()
    with pytest.raises(vn.VoiceError, match="replaced"):
        vn.ensure_services(root, runtime, {}, optional=[])
    assert not [e for e in world.events if e.startswith("spawn:")]


def test_prune_keeps_active_leased_and_serving_runtimes(vn, tmp_path, monkeypatch):
    root = tmp_path / "root"
    for name in ("active", "leased", "serving", "old1", "old2"):
        (root / "runtime" / name / ".venv").mkdir(parents=True)
    clients = root / "run" / "clients"
    clients.mkdir(parents=True)
    (clients / "50").write_text(json.dumps({"runtime": str(root / "runtime" / "leased")}))
    (clients / "51").write_text(json.dumps({"runtime": str(root / "runtime" / "old2")}))  # dead
    commands = {50: "python voicemode_native.py launch", 60: "python kokoro-onnx-server.py --port 28880"}
    monkeypatch.setattr(vn, "process_command", lambda pid: commands.get(pid))
    vn.atomic_json(root / "run" / "state.json", {"tts": {
        "pid": 60, "port": 28880, "marker": "kokoro-onnx-server.py", "runtime": str(root / "runtime" / "serving")}})
    removed = vn.prune_runtimes(root, "active")
    assert sorted(removed) == ["old1", "old2"]
    assert sorted(p.name for p in (root / "runtime").iterdir()) == ["active", "leased", "serving"]


# -- setup: uv under the root, cache removed on failure, Kokoro synthesises ----

def test_failed_setup_removes_uv_cache_and_runtime(vn, home, tmp_path, monkeypatch):
    root = tmp_path / "root"
    calls = []

    def fake_run(args, **kwargs):
        calls.append((list(map(str, args)), kwargs.get("env") or {}))
        Path(kwargs["env"]["UV_CACHE_DIR"]).mkdir(parents=True, exist_ok=True)
        if "sync" in map(str, args):
            raise vn.subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(vn, "check_machine", lambda: None)
    monkeypatch.setattr(vn, "MAX_ESPEAK_PATH", 10_000)  # pytest's tmp paths are long
    monkeypatch.setattr(vn, "download", lambda item, target: None)
    monkeypatch.setattr(vn, "run", fake_run)
    monkeypatch.setattr(vn, "package_index_reachable", lambda: True)
    monkeypatch.setenv("VOICEMODE_UV", "uv")
    with pytest.raises(vn.subprocess.CalledProcessError):
        vn.setup(root)
    venv_call = calls[0]
    assert venv_call[1]["UV_PYTHON_INSTALL_DIR"] == str(root / "python")
    assert venv_call[1]["UV_CACHE_DIR"] == str(root / ".uv-cache")
    assert not (root / ".uv-cache").exists()
    assert not list((root / "runtime").iterdir())


def _fake_kokoro(tmp_path, samples):
    pkg = tmp_path / "fake" / "kokoro_onnx"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(
        "class Kokoro:\n"
        "    def __init__(self, model, voices):\n        self.args = (model, voices)\n"
        f"    def create(self, text, voice, lang):\n        return {samples!r}, 24000\n")
    return tmp_path / "fake"


@pytest.mark.parametrize("samples,ok", [([0.1, 0.2], True), ([], False)])
def test_synth_check_needs_audio_and_plays_none(vn, tmp_path, samples, ok):
    import subprocess as sp
    assert "play" not in vn.SYNTH_CHECK and "sounddevice" not in vn.SYNTH_CHECK
    env = dict(vn.os.environ, PYTHONPATH=str(_fake_kokoro(tmp_path, samples)))
    r = sp.run([sys.executable, "-c", vn.SYNTH_CHECK, "m.onnx", "v.bin", "af_sky"],
               capture_output=True, text=True, env=env)
    assert (r.returncode == 0) is ok, r.stderr


def test_setup_runs_the_synth_check():
    assert "SYNTH_CHECK" in SCRIPT.read_text(encoding="utf-8").split("def setup(", 1)[1]


# -- the watchdog restarts a crashed speech service on its port ---------------

def _serving(vn, monkeypatch, tmp_path):
    root = tmp_path / "root"
    world = FakeWorld(vn, monkeypatch, root)
    runtime = _fake_runtime(vn, root)
    monkeypatch.setattr(vn, "runtime_environment", lambda runtime, ports=None: {})
    ports = vn.ensure_services(root, runtime, {}, optional=[])
    return root, world, ports


def _state(root):
    return json.loads((root / "run" / "state.json").read_text())


def test_watchdog_restarts_a_crashed_service_on_the_same_port(vn, home, tmp_path, monkeypatch):
    root, world, ports = _serving(vn, monkeypatch, tmp_path)
    old = _state(root)["tts"]["pid"]
    world.commands.pop(old)  # it crashed
    world.events.clear()
    assert vn.watch_tick(root, {}) is True
    entry = _state(root)["tts"]
    assert entry["pid"] != old and entry["port"] == ports["tts"]
    assert [e for e in world.events if e.startswith("spawn:")] == ["spawn:tts"]
    assert "restarted tts" in (root / "run" / "watch.log").read_text()


def test_watchdog_does_not_restart_on_a_taken_port_or_too_often(vn, home, tmp_path, monkeypatch):
    root, world, ports = _serving(vn, monkeypatch, tmp_path)
    world.commands.pop(_state(root)["stt"]["pid"])
    monkeypatch.setattr(vn, "port_free", lambda p: False)
    world.events.clear()
    assert vn.watch_tick(root, {}) is True
    assert not [e for e in world.events if e.startswith("spawn:")]
    monkeypatch.setattr(vn, "port_free", lambda p: True)
    restarts = {}
    for _ in range(vn.MAX_RESTARTS + 2):
        world.commands.pop(_state(root)["stt"]["pid"], None)
        vn.watch_tick(root, restarts)
    assert len([e for e in world.events if e == "spawn:stt"]) == vn.MAX_RESTARTS


def test_watchdog_stops_everything_when_no_lease_is_left(vn, home, tmp_path, monkeypatch):
    root, world, ports = _serving(vn, monkeypatch, tmp_path)
    (root / "run" / "clients" / str(vn.os.getpid())).unlink()
    assert vn.watch_tick(root, {}) is False
    assert sorted(e for e in world.events if e.startswith("kill:")) == ["kill:stt", "kill:tts"]


def test_voice_mode_starts_in_the_runtime_folder_not_the_clients(vn, home, tmp_path, monkeypatch):
    """Upstream loads every .voicemode/voicemode.env found walking up from its working folder.
    Started in the client's folder (2026-11-06: \\\\wsl.localhost\\...\\voicemode-local) it read
    the WSL setup's file: streaming off, debug on, audio saved. The client's folder still
    reaches the session label through VOICEMODE_CLIENT_FOLDER."""
    root = tmp_path / "root"
    world = FakeWorld(vn, monkeypatch, root)
    runtime = _fake_runtime(vn, root)
    client = tmp_path / "work" / "my-project"
    client.mkdir(parents=True)
    monkeypatch.chdir(client)
    seen = {}
    plain = world.spawn

    def spawn(command, job=None, **kwargs):
        if world.kind_of(command) == "voice-mode":
            seen.update(kwargs)
        return plain(command, job=job, **kwargs)

    monkeypatch.setattr(vn, "spawn", spawn)
    monkeypatch.setattr(vn, "configured_runtime", lambda root, expected: (runtime, True))
    monkeypatch.setattr(vn, "check_models", lambda runtime: None)
    monkeypatch.setattr(vn, "optional_services", lambda: [])
    assert vn.launch(root) == 0
    assert Path(seen["cwd"]) == runtime
    assert seen["env"]["VOICEMODE_CLIENT_FOLDER"] == str(client)


def test_barge_trigger_is_an_optional_service_with_the_pinned_silero(vn, tmp_path, monkeypatch):
    runtime = tmp_path / "runtime" / "abc"
    command, marker = vn.optional_command("barge", runtime)
    assert command[1:3] == ["-m", "voice_mode.voice_barge"]
    assert Path(command[3]) == runtime / "models" / "silero" / "silero_vad.onnx"
    assert marker == "voice_mode.voice_barge"
    deps = json.loads((vn.HERE / "dependencies.json").read_text(encoding="utf-8"))
    silero = [m for m in deps["models"] if m["path"] == "models/silero/silero_vad.onnx"]
    assert silero == [{"path": "models/silero/silero_vad.onnx",
                       "url": "https://raw.githubusercontent.com/snakers4/silero-vad/"
                              "bfdc0193023f121ea5b3cc7b176dbed570a68a59/src/silero_vad/data/silero_vad.onnx",
                       "size": 2327524,
                       "sha256": "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"}]


@pytest.mark.parametrize("value,started", [("true", True), ("false", False), ("", False), ("yes", False)])
def test_barge_starts_only_when_its_setting_is_exactly_true(vn, monkeypatch, value, started):
    monkeypatch.setattr(vn, "setting", lambda name, default=None: value if name == "VOICEMODE_VOICE_BARGE" else "")
    assert ("barge" in vn.optional_services()) is started


# -- the pill window, native (task 6) ----------------------------------------

def test_pill_command_starts_the_backend_directly_with_a_data_dir_per_install(vn, tmp_path, monkeypatch):
    monkeypatch.setattr(vn, "WINDOWS", True)
    root = tmp_path / "root"
    runtime = root / "runtime" / "abc"
    command, marker = vn.optional_command("pill", runtime, root)
    assert marker == str(vn.REPO / "pill-windows.ps1")
    assert command[0] == vn.powershell()
    assert command.index("-STA") < command.index("-File") and command[command.index("-File") + 1] == marker

    def value(flag):
        return command[command.index(flag) + 1]

    assert value("-BackendExe") == str(vn.venv_program(runtime, "python"))
    assert value("-BackendArgs") == "-m voice_mode.voice_pill --stdio"
    assert value("-DataDir") == str(vn.pill_data_dir(root)) == str(root / "pill")
    text = " ".join(command)
    assert "-BackendCommand" not in text and "-Distro" not in text and "wsl" not in text.lower()


def test_pill_command_root_defaults_to_the_runtimes_install(vn, tmp_path):
    root = tmp_path / "root"
    runtime = root / "runtime" / "abc"
    assert vn.optional_command("pill", runtime) == vn.optional_command("pill", runtime, root)


def test_pill_names_are_per_data_dir_and_ignore_case_and_trailing_slash(vn):
    mutex, stop = vn.pill_names(r"C:\Users\Me\AppData\Local\voicemode-local-native\pill")
    assert re.fullmatch(r"Local\\VoiceModePillWindow-[0-9a-f]{12}", mutex)
    assert re.fullmatch(r"Local\\VoiceModePillStop-[0-9a-f]{12}", stop)
    assert mutex[-12:] == stop[-12:]
    assert vn.pill_names(r"c:\users\me\appdata\local\voicemode-local-native\pill\\") == (mutex, stop)
    other = vn.pill_names(r"C:\Users\Me\AppData\Local\other-install\pill")
    assert other[0] != mutex and other[1] != stop
    # Never the WSL pill's machine-wide names, which voicemode-switch signals.
    assert mutex != "Local\\VoiceModePillWindow" and stop != "Local\\VoiceModePillStop"


def test_pill_names_hash_is_sha256_of_the_lowercased_utf8_path(vn):
    import hashlib
    path = "C:\\Users\\J\u00fcrgen M\u00fcller\\vml\\pill"
    want = hashlib.sha256(path.lower().encode("utf-8")).hexdigest()[:12]
    assert vn.pill_names(path) == (f"Local\\VoiceModePillWindow-{want}", f"Local\\VoiceModePillStop-{want}")


def test_pill_stop_event_is_the_stop_name_of_the_installs_data_dir(vn, tmp_path):
    root = tmp_path / "root"
    assert vn.pill_stop_event(root) == vn.pill_names(root / "pill")[1]
