"""Linux unit tests for scripts/windows/voicemode_native.py, the native Windows launcher.

Logic only: port choice, settings, start order, lease bookkeeping, runtime selection,
and the --host defaults of the two speech servers. Nothing here starts a speech
service, binds a fixed port or touches ~/.voicemode: HOME is a temp folder.
The real thing on Windows is tests/test_voicemode_native_windows.py.
"""
import ast
import importlib.util
import json
import re
import socket
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "windows" / "voicemode_native.py"
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from vml_supervisor import helpers as sv_helpers  # noqa: E402
from vml_supervisor import host as sv_host  # noqa: E402
from vml_supervisor import ports as sv_ports  # noqa: E402
from vml_supervisor import procs as sv_procs  # noqa: E402
from vml_supervisor import services as sv_services  # noqa: E402
from vml_supervisor import state as sv_state  # noqa: E402
from vml_supervisor import winjob as sv_winjob  # noqa: E402
from vml_supervisor.hooks import load_hooks  # noqa: E402
from vml_supervisor.profile import BASE_RESERVED_PORTS  # noqa: E402
from vml_supervisor.result import Result  # noqa: E402


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


# -- vml_supervisor's seams ----------------------------------------------------------
# Since Task 8 the launcher's lifecycle is vml_supervisor's, through the profile in
# vml_profiles/claude.py. These adapters reach the behaviour each test checks; they first ran
# green against the launcher's own functions (commit 0f4025c), then against the package.

def _fake_commands(monkeypatch, vn, lookup):
    """Every process-command lookup in the package answers from lookup. owned and live_leases
    bind process_command as a default argument: those are replaced too, in every copy the
    modules hold (another test's importlib.reload(procs) leaves services with the old one)."""
    owned = {id(f): f for f in (getattr(m, "owned", None) for m in (sv_procs, sv_services, sv_helpers, sv_host)) if f}
    leases = {id(f): f for f in (getattr(m, "live_leases", None) for m in (sv_state, sv_services, sv_host)) if f}
    for function in [*owned.values(), *leases.values()]:
        monkeypatch.setattr(function, "__defaults__", (lookup,))
    for module in (sv_procs, sv_services, sv_host):
        monkeypatch.setattr(module, "process_command", lookup)


def _set_port_free(vn, monkeypatch, check):
    for module in (sv_ports, sv_host):
        monkeypatch.setattr(module, "port_free", check)


def _ranges(vn, root):
    """({kind: [ports]}, reserved ports)."""
    profile = vn.profile_of(root)
    ranges = {kind: list(range(spec.port_range[0], spec.port_range[1] + 1))
              for kind, spec in profile.services.items()}
    return ranges, set(BASE_RESERVED_PORTS) | set(profile.reserved_ports)


def _candidates(vn, root, kind):
    vn.write_port_settings(root)
    candidates, problem = sv_ports.port_candidates(vn.profile_of(root), kind)
    assert problem is None, problem
    return candidates


def _choose(vn, root, kind, taken):
    vn.write_port_settings(root)
    return sv_ports.choose_port(vn.profile_of(root), kind, taken)


def _ensure(vn, root, runtime, env=None):
    """The leased speech services alone (serve's start); returns their ports."""
    return vn.ensure_speech(vn.profile_of(root, runtime), helpers=False)


def _release(vn, root):
    return vn.leave(vn.profile_of(root))


def _fake_stop_recorded(vn, monkeypatch, record):
    monkeypatch.setattr(sv_services, "stop_recorded", lambda profile, state: record(state) or [])


def _live_leases(vn, root):
    return sv_state.live_leases(vn.profile_of(root))


def _owned(vn, entry):
    return sv_procs.owned(entry)


def _service_command(vn, kind, runtime, port):
    spec = vn.profile_of(runtime.parent, runtime).services[kind]
    return [a.replace("{port}", str(port)) for a in spec.argv], spec.marker


def _watch_tick(vn, root, restarts):
    profile = vn.profile_of(root)
    hooks, problems = load_hooks(profile)
    assert not problems, problems
    return sv_host.watch_tick(profile, hooks, restarts.setdefault("watch", sv_host.WatchState()))


def _max_restarts(vn, root):
    return vn.profile_of(root).restart_limit[0]


def _fake_services_job(vn, monkeypatch):
    """The services' Job Object as ("job", <install root>)."""
    monkeypatch.setattr(sv_winjob, "services_job", lambda profile: ("job", profile.root))
    monkeypatch.setattr(sv_helpers, "services_job", lambda profile: ("job", profile.root))


# -- ports ------------------------------------------------------------------

def test_port_ranges_avoid_the_live_setup_and_felix(vn, tmp_path):
    ranges, reserved = _ranges(vn, tmp_path / "root")
    live = {8880, 8881, 8882, 2022, 9000}
    felix = set(range(18880, 18900)) | set(range(12022, 12042))
    assert live <= reserved and felix <= reserved
    for kind in ("tts", "stt"):
        ports = set(ranges[kind])
        assert ports and not ports & reserved
        assert min(ports) >= 1024 and max(ports) <= 65535
    assert not set(ranges["tts"]) & set(ranges["stt"])
    assert ranges["tts"] == list(range(28880, 28900)) and ranges["stt"] == list(range(22022, 22042))


def test_port_free_sees_a_listener(vn):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        assert sv_procs.port_free(port) is False
    assert sv_procs.port_free(port) is True


def test_choose_port_skips_taken_and_busy_ports(vn, home, tmp_path, monkeypatch):
    root = tmp_path / "root"
    first, second, third = _ranges(vn, root)[0]["tts"][:3]
    _set_port_free(vn, monkeypatch, lambda p: p != first)
    assert _choose(vn, root, "tts", set()) == second
    assert _choose(vn, root, "tts", {second}) == third


def test_choose_port_tries_the_setting_first(vn, home, tmp_path, monkeypatch):
    root = tmp_path / "root"
    _set_port_free(vn, monkeypatch, lambda p: True)
    _env_file(home, "VOICEMODE_NATIVE_STT_PORT=23456\n")
    assert _candidates(vn, root, "stt")[0] == 23456
    assert _choose(vn, root, "stt", set()) == 23456
    monkeypatch.setenv("VOICEMODE_NATIVE_STT_PORT", "24000")
    assert _choose(vn, root, "stt", set()) == 24000


@pytest.mark.parametrize("value", ["8880", "2022", "9000", "18880", "12022", "80", "abc", "70000"])
def test_choose_port_refuses_a_reserved_or_bad_setting(vn, home, tmp_path, monkeypatch, value):
    monkeypatch.setenv("VOICEMODE_NATIVE_TTS_PORT", value)
    with pytest.raises(vn.VoiceError):
        _candidates(vn, tmp_path / "root", "tts")


def test_choose_port_reports_when_every_port_is_busy(vn, home, tmp_path, monkeypatch):
    root = tmp_path / "root"
    world = FakeWorld(vn, monkeypatch, root)
    _set_port_free(vn, monkeypatch, lambda p: False)
    with pytest.raises(vn.VoiceError, match="No free local port"):
        _ensure(vn, root, _fake_runtime(vn, root))
    assert not [e for e in world.events if e.startswith("spawn:")]


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
    monkeypatch.setattr(vn, "ALLOW_OUTSIDE", True)
    assert vn.profile_of(tmp_path).watch_command[-2:] == ["watch", "--allow-root-outside-profile"]


def test_push_to_talk_starts_natively(vn, home, monkeypatch, capsys):
    monkeypatch.setattr(vn, "WINDOWS", True)
    _env_file(home, "VOICEMODE_PILL=true\nVOICEMODE_PTT_ENABLED=true\n")
    assert vn.optional_services() == ["pill", "ptt"]
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
        command, marker = _service_command(vn, kind, tmp_path, 28880)
        assert command[command.index("--host") + 1] == "127.0.0.1"
        assert command[command.index("--port") + 1] == "28880"
        assert marker and any(marker in part for part in command)


def _os_kill_calls(source):
    return [node for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call) and ast.unparse(node.func) == "os.kill"]


def test_no_os_kill_zero_anywhere_in_the_launcher():
    # The check itself sees a probe with 0 (the old `assert calls` guarded against a vacuous
    # check; the launcher's POSIX stop signals moved into vml_supervisor with Task 8).
    probe = _os_kill_calls("import os\nos.kill(5, 0)\n")
    assert probe and probe[0].args[1].value == 0
    calls = _os_kill_calls(SCRIPT.read_text(encoding="utf-8"))
    assert not [c for c in calls if isinstance(c.args[1], ast.Constant) and c.args[1].value == 0]


class FakeWorld:
    """Fakes process creation and inspection in vml_supervisor's seams, and records what starts
    in which order."""

    def __init__(self, vn, monkeypatch, root):
        self.vn, self.root, self.events, self.commands, self.next_pid = vn, root, [], {}, 1000
        self.observers = []   # f(command, job, kwargs) for every start
        self.replace = {}     # kind -> f(command, kwargs) returning a real process
        self.allowed = set()  # real pids a test may signal
        self.commands[vn.os.getpid()] = "python voicemode_native.py launch"
        # Fake pids are real numbers on this machine: no signal may ever reach them.
        self._kill = vn.os.kill
        monkeypatch.setattr(vn, "WINDOWS", True)
        monkeypatch.setattr(vn.os, "kill", self.no_signals)
        monkeypatch.setattr(vn, "connector_job", lambda: None)
        _fake_commands(monkeypatch, vn, self.commands.get)
        for module in (sv_winjob, sv_services, sv_helpers, sv_host):
            monkeypatch.setattr(module, "spawn", self.spawn_in_job)
            monkeypatch.setattr(module, "services_job", lambda profile: None)
        for module in (sv_procs, sv_services, sv_host):
            monkeypatch.setattr(module, "kill_tree", self.kill)
        monkeypatch.setattr(sv_services, "process_tree", lambda pid: {pid})
        monkeypatch.setattr(sv_services, "listener_pids", self.listeners)
        monkeypatch.setattr(sv_services, "check_service", self.check)
        monkeypatch.setattr(sv_services, "_start_watchdog",
                            lambda profile, working_dir, results: self.events.append("watchdog"))
        _set_port_free(vn, monkeypatch, lambda p: True)
        monkeypatch.setattr(sv_helpers, "running_commands", lambda: (list(self.commands.items()), None))
        monkeypatch.setattr(sv_helpers, "HELPER_START_CHECK", 0)
        for module in (sv_helpers, sv_host):   # the trigger and the listener are Windows' helpers
            monkeypatch.setattr(module, "PLATFORM", "win32")

    def no_signals(self, pid, sig):
        if pid in self.allowed:
            return self._kill(pid, sig)
        raise AssertionError(f"a test sent signal {sig} to real pid {pid}")

    def spawn(self, command, job=None, **kwargs):
        for observe in self.observers:
            observe(command, job, kwargs)
        lease = self.root / "run" / "clients" / str(self.vn.os.getpid())
        self.events.append(("lease" if lease.exists() else "NO-LEASE"))
        kind = self.kind_of(command)
        if kind in self.replace:
            process = self.replace[kind](command, kwargs)
            self.allowed.add(process.pid)
            self.events.append(f"spawn:{kind}")
            return process
        pid, self.next_pid = self.next_pid, self.next_pid + 1
        self.commands[pid] = " ".join(map(str, command))
        self.events.append(f"spawn:{kind}")
        return FakeProcess(self, pid)

    def spawn_in_job(self, command, job, **kwargs):
        return self.spawn(command, job=job, **kwargs), Result("no_job")

    def check(self, kind, port, path=None):
        self.events.append("ready:" + {"kokoro": "tts", "whisper": "stt"}.get(kind, kind))
        return True

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

    def kill(self, pid, timeout=None):
        self.events.append(f"kill:{self.kind_of(self.commands.get(pid, '').split())}")
        self.commands.pop(pid, None)


class FakeProcess:
    """Alive while the world still has its command; wait() is an exit."""

    def __init__(self, world, pid):
        self.world, self.pid, self.returncode = world, pid, None

    def poll(self):
        if self.returncode is None and self.pid not in self.world.commands:
            self.returncode = 0
        return self.returncode

    def wait(self, timeout=None):
        self.world.commands.pop(self.pid, None)
        self.returncode = 0
        return 0


def _fake_runtime(vn, root):
    runtime = root / "runtime" / "abc"
    (runtime / "services" / "whisper").mkdir(parents=True)
    (runtime / ".complete.json").write_text("{}")
    return runtime


def _non_events(events):
    return [e for e in events if e not in ("lease", "NO-LEASE")]


_SETTING_OF = {"pill": "VOICEMODE_PILL", "ptt": "VOICEMODE_PTT_ENABLED", "barge": "VOICEMODE_VOICE_BARGE"}


@pytest.mark.parametrize("enabled", [[], ["pill"], ["pill", "ptt"], ["barge"]])
def test_start_order(vn, home, tmp_path, monkeypatch, enabled):
    root = tmp_path / "root"
    world = FakeWorld(vn, monkeypatch, root)
    runtime = _fake_runtime(vn, root)
    monkeypatch.setattr(vn, "WINDOWS", True)
    monkeypatch.setattr(vn, "configured_runtime", lambda root, expected: (runtime, True))
    monkeypatch.setattr(vn, "check_models", lambda runtime: None)
    for kind in enabled:
        monkeypatch.setenv(_SETTING_OF[kind], "true")
    assert vn.launch(root) == 0
    assert "NO-LEASE" not in world.events          # the lease exists before anything starts
    # The pill is the voice server's to open (pill_autostart, 2026-11-10), never the launcher's.
    started = [k for k in enabled if k != "pill"]
    events = _non_events(world.events)
    assert sorted(e for e in events if e.startswith("spawn:")) == sorted(
        ["spawn:tts", "spawn:stt", "spawn:voice-mode"] + [f"spawn:{k}" for k in started])
    # The watchdog first; the services, then their readiness, then voice-mode. The trigger and
    # the listener start after the services and before voice-mode (since Task 8 after the
    # services answer, as vml_supervisor orders them; before it, while they loaded).
    at = events.index
    assert events[0] == "watchdog"
    assert at("spawn:tts") < at("spawn:stt") < at("ready:tts") < at("ready:stt") < at("spawn:voice-mode")
    for kind in started:
        assert at("spawn:stt") < at(f"spawn:{kind}") < at("spawn:voice-mode")
    assert "spawn:pill" not in world.events
    # The last connector left: every service it started is stopped, the lease is gone.
    stops = [e for e in world.events if e.startswith("kill:")]
    assert sorted(stops) == sorted(["kill:tts", "kill:stt"] + [f"kill:{k}" for k in started])
    assert not list((root / "run" / "clients").iterdir())


def test_services_are_reused_not_restarted(vn, home, tmp_path, monkeypatch):
    root = tmp_path / "root"
    world = FakeWorld(vn, monkeypatch, root)
    runtime = _fake_runtime(vn, root)
    monkeypatch.setattr(vn, "WINDOWS", True)
    env = vn.runtime_environment(runtime)
    first = _ensure(vn, root, runtime, env)
    world.events.clear()
    second = _ensure(vn, root, runtime, env)
    assert first == second
    assert not [e for e in world.events if e.startswith("spawn:")]


def test_two_kinds_never_share_a_port(vn, home, tmp_path, monkeypatch):
    root = tmp_path / "root"
    FakeWorld(vn, monkeypatch, root)
    monkeypatch.setattr(vn, "WINDOWS", True)
    ports = _ensure(vn, root, _fake_runtime(vn, root))
    ranges = _ranges(vn, root)[0]
    assert ports["tts"] in ranges["tts"] and ports["stt"] in ranges["stt"]


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
    _fake_commands(monkeypatch, vn, lambda pid: commands.get(pid))
    for name in ("11", "12", "21", "41"):
        (clients / name).write_text("")
    (clients / "30").write_text(json.dumps({"child": 31}))  # launcher gone, its voice-mode lives
    (clients / "not-a-pid").write_text("")
    (clients / ".lease-being-written").write_text("")
    assert sorted(_live_leases(vn, tmp_path)) == [11, 12, 30]
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
    _fake_commands(monkeypatch, vn, lambda pid: alive.get(pid))
    stopped = []
    _fake_stop_recorded(vn, monkeypatch, stopped.append)
    vn.atomic_json(root / "run" / "state.json", {"tts": {"pid": 5, "port": 28880, "marker": "m"}})
    _release(vn, root)
    assert stopped == [] and not (clients / str(me)).exists()
    alive.clear()
    _release(vn, root)
    assert stopped == [{"tts": {"pid": 5, "port": 28880, "marker": "m"}}]


def test_owned_needs_marker_and_port(vn, monkeypatch):
    _fake_commands(monkeypatch, vn, lambda pid: "python kokoro-onnx-server.py --port 28880")
    assert _owned(vn, {"pid": 5, "port": 28880, "marker": "kokoro-onnx-server.py"})
    assert not _owned(vn, {"pid": 5, "port": 28881, "marker": "kokoro-onnx-server.py"})
    assert not _owned(vn, {"pid": 5, "port": 28880, "marker": "whisper"})
    assert _owned(vn, {"pid": 5, "port": None, "marker": "kokoro-onnx-server.py"})  # pill, ptt: no port
    assert not _owned(vn, None) and not _owned(vn, {"pid": 0, "port": 1, "marker": "x"})


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
    _ensure(vn, root, runtime)
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
        _ensure(vn, root, runtime)
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
    _fake_commands(monkeypatch, vn, lambda pid: commands.get(pid))
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
    ports = _ensure(vn, root, runtime)
    return root, world, ports


def _state(root):
    return json.loads((root / "run" / "state.json").read_text())


def test_watchdog_restarts_a_crashed_service_on_the_same_port(vn, home, tmp_path, monkeypatch):
    root, world, ports = _serving(vn, monkeypatch, tmp_path)
    old = _state(root)["tts"]["pid"]
    world.commands.pop(old)  # it crashed
    world.events.clear()
    assert _watch_tick(vn, root, {}) is True
    entry = _state(root)["tts"]
    assert entry["pid"] != old and entry["port"] == ports["tts"]
    assert [e for e in world.events if e.startswith("spawn:")] == ["spawn:tts"]
    assert "restarted tts" in (root / "run" / "watch.log").read_text()


def test_watchdog_does_not_restart_on_a_taken_port_or_too_often(vn, home, tmp_path, monkeypatch):
    root, world, ports = _serving(vn, monkeypatch, tmp_path)
    world.commands.pop(_state(root)["stt"]["pid"])
    _set_port_free(vn, monkeypatch, lambda p: False)
    world.events.clear()
    assert _watch_tick(vn, root, {}) is True
    assert not [e for e in world.events if e.startswith("spawn:")]
    _set_port_free(vn, monkeypatch, lambda p: True)
    restarts = {}
    for _ in range(_max_restarts(vn, root) + 2):
        world.commands.pop(_state(root)["stt"]["pid"], None)
        _watch_tick(vn, root, restarts)
    assert len([e for e in world.events if e == "spawn:stt"]) == _max_restarts(vn, root)


def test_watchdog_stops_everything_when_no_lease_is_left(vn, home, tmp_path, monkeypatch):
    root, world, ports = _serving(vn, monkeypatch, tmp_path)
    (root / "run" / "clients" / str(vn.os.getpid())).unlink()
    assert _watch_tick(vn, root, {}) is False
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
    world.observers.append(lambda command, job, kwargs: world.kind_of(command) == "voice-mode"
                           and seen.update(kwargs))
    monkeypatch.setattr(vn, "configured_runtime", lambda root, expected: (runtime, True))
    monkeypatch.setattr(vn, "check_models", lambda runtime: None)
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


# -- push-to-talk, native (task 7) -------------------------------------------

def test_ptt_command_runs_the_sender_from_the_runtime(vn, home, tmp_path, monkeypatch):
    monkeypatch.setattr(vn, "WINDOWS", True)
    root = tmp_path / "root"
    runtime = root / "runtime" / "abc"
    command, marker = vn.optional_command("ptt", runtime, root)
    assert marker == str(vn.REPO / "ptt-listener-windows.ps1")
    assert command[0] == vn.powershell() and command[command.index("-File") + 1] == marker

    def value(flag):
        return command[command.index(flag) + 1]

    # The sender names the pipe itself, per send, from the environment voice-mode also gets.
    assert value("-SenderExe") == str(vn.venv_program(runtime, "python"))
    assert value("-SenderArgs") == "-m voice_mode.ptt_control_client --stdin"
    assert value("-LogPath") == str(root / "run" / "ptt-listener.log")
    text = " ".join(command)
    assert "-RelayHost" not in text and "-Port" not in text and "wsl" not in text.lower()


def test_the_sender_names_the_pipe_voice_mode_serves(home, tmp_path, monkeypatch):
    """ptt_control_client resolves control.sock as voice_pill_state does (upstream's
    VOICEMODE_CONTROL_SOCKET, else the base dir), and the transport turns it into the pipe."""
    client = _load(REPO / "patches" / "ptt_control_client.py", "ptt_cc_pipe")
    ct = _load(REPO / "patches" / "control_transport.py", "ct_ptt_pipe")
    monkeypatch.delenv("VOICEMODE_CONTROL_SOCKET", raising=False)
    monkeypatch.delenv("VOICEMODE_CONTROL_SOCKET_PATH", raising=False)
    monkeypatch.setenv("VOICEMODE_BASE_DIR", str(tmp_path / "base"))
    assert client.socket_path() == tmp_path / "base" / "control.sock"
    seen = []
    monkeypatch.setattr(client, "_control_transport", lambda: type("T", (), {
        "connect": staticmethod(lambda path, timeout: seen.append(ct.pipe_name(path)) or (_ for _ in ()).throw(
            FileNotFoundError(2, "no pipe")))})())
    monkeypatch.delattr(client.socket, "AF_UNIX", raising=False)
    assert client.action_outcome("short_press") == "no-channel"
    assert seen == [ct.pipe_name(tmp_path / "base" / "control.sock")]


def test_ptt_command_root_defaults_to_the_runtimes_install(vn, tmp_path):
    root = tmp_path / "root"
    runtime = root / "runtime" / "abc"
    assert vn.optional_command("ptt", runtime) == vn.optional_command("ptt", runtime, root)


def test_ptt_is_a_switch(vn):
    assert vn.SWITCHES["ptt"] == vn.OPTIONAL_SETTINGS["ptt"] == "VOICEMODE_PTT_ENABLED"
    assert vn.SWITCH_NOUNS["ptt"] == "listener"


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


# -- the native switch (task 8) ------------------------------------------------

def test_write_setting_keeps_other_lines_comments_and_crlf(vn, tmp_path):
    path = tmp_path / ".voicemode" / "voicemode.env"
    path.parent.mkdir()
    path.write_bytes(b"\xef\xbb\xbf# mine\r\nVOICEMODE_VOICES=af_sky\r\n# VOICEMODE_PILL=true\r\n"
                     b"export VOICEMODE_PILL=false\r\nOTHER=1")
    vn.write_setting(path, "VOICEMODE_PILL", "true")
    assert path.read_bytes() == (b"\xef\xbb\xbf# mine\r\nVOICEMODE_VOICES=af_sky\r\n# VOICEMODE_PILL=true\r\n"
                                 b"export VOICEMODE_PILL=true\r\nOTHER=1")
    vn.write_setting(path, "VOICEMODE_VOICE_BARGE", "false")
    assert path.read_bytes().endswith(b"OTHER=1\r\nVOICEMODE_VOICE_BARGE=false\r\n")
    assert sorted(p.name for p in path.parent.iterdir()) == ["voicemode.env", "voicemode.env.lock"]  # no temp file


def test_write_setting_replaces_every_assignment_and_keeps_lf(vn, tmp_path):
    path = tmp_path / "voicemode.env"
    path.write_text("A=1\nVOICEMODE_PILL=true\nB=2\n VOICEMODE_PILL = \"true\"\n", encoding="utf-8")
    vn.write_setting(path, "VOICEMODE_PILL", "false")
    assert path.read_text(encoding="utf-8") == "A=1\nVOICEMODE_PILL=false\nB=2\n VOICEMODE_PILL=false\n"


def test_write_setting_creates_the_folder_and_file(vn, tmp_path, monkeypatch):
    monkeypatch.setattr(vn, "WINDOWS", True)
    path = tmp_path / "new" / ".voicemode" / "voicemode.env"
    vn.write_setting(path, "VOICEMODE_PILL", "true")
    assert path.read_bytes() == b"VOICEMODE_PILL=true\r\n"


class Switch:
    """FakeWorld plus a configured runtime with the Silero model, and the stop event faked."""

    def __init__(self, vn, home, tmp_path, monkeypatch, lease=True):
        self.vn, self.home = vn, home
        self.root = tmp_path / "root"
        self.world = FakeWorld(vn, monkeypatch, self.root)
        self.runtime = _fake_runtime(vn, self.root)
        silero = self.runtime / vn.SILERO
        silero.parent.mkdir(parents=True)
        silero.write_bytes(b"x")
        self.events = self.world.events
        monkeypatch.setattr(vn, "configured_runtime", lambda root, expected: (self.runtime, True))
        monkeypatch.setattr(vn, "silero_present", lambda runtime: (runtime / vn.SILERO).is_file())
        monkeypatch.setattr(vn, "set_pill_stop_event", lambda root: self.events.append("stop-event") or True)
        monkeypatch.setattr(vn, "STOP_WAIT", 0)
        monkeypatch.setattr(vn, "can_inspect", lambda: True)
        monkeypatch.delenv("VOICEMODE_NATIVE_ENV_SETTINGS", raising=False)
        monkeypatch.setattr(vn.resolver(), "capture_backend", lambda: "sounddevice")
        if lease:
            # A running connector: its launcher's command line is this process's (FakeWorld).
            (self.root / "run" / "clients").mkdir(parents=True)
            (self.root / "run" / "clients" / str(vn.os.getpid())).write_text(
                json.dumps({"runtime": str(self.runtime)}))

    def env_file(self):
        return (self.home / ".voicemode" / "voicemode.env").read_text(encoding="utf-8")

    def state(self):
        return json.loads((self.root / "run" / "state.json").read_text())


@pytest.mark.parametrize("kind,name", [("barge", "VOICEMODE_VOICE_BARGE"), ("pill", "VOICEMODE_PILL"),
                                       ("ptt", "VOICEMODE_PTT_ENABLED")])
def test_switch_on_writes_the_setting_and_starts_the_service_for_a_running_connector(
        vn, home, tmp_path, monkeypatch, capsys, kind, name):
    s = Switch(vn, home, tmp_path, monkeypatch)
    assert vn.switch(s.root, kind, "on") == 0
    assert f"{name}=true" in s.env_file()
    assert [e for e in s.events if e.startswith("spawn:")] == [f"spawn:{kind}"]
    entry = s.state()[kind]
    assert _owned(vn, entry)
    # It runs from the serving runtime. A pill record names it; the trigger's and the
    # listener's records (vml_supervisor helpers since Task 8) carry it in their command.
    assert (entry["runtime"] == str(s.runtime) if kind == "pill"
            else str(s.runtime) in " ".join(entry["command"]))
    out = capsys.readouterr().out
    assert f"started (process {entry['pid']})" in out
    s.events.clear()
    assert vn.switch(s.root, kind, "on") == 0                # one per install
    assert not [e for e in s.events if e.startswith("spawn:")]
    assert "already running" in capsys.readouterr().out


def test_switch_starts_into_the_services_job_like_ensure_services(vn, home, tmp_path, monkeypatch):
    s = Switch(vn, home, tmp_path, monkeypatch)
    jobs, envs = [], []
    s.world.observers.append(lambda command, job, kwargs: jobs.append(job) or envs.append(kwargs["env"]))
    _fake_services_job(vn, monkeypatch)
    vn.switch(s.root, "pill", "on")
    assert jobs == [("job", s.root)]
    assert envs[0]["VOICEMODE_PILL"] == "true"   # the window checks it; a stale false must not stop it
    assert json.loads(envs[0]["VOICEMODE_BARGE_CONTROL_COMMAND"])[-1] == "barge"


@pytest.mark.parametrize("kind", ["barge", "pill", "ptt"])
def test_switch_on_without_a_connector_only_writes_the_setting(vn, home, tmp_path, monkeypatch, capsys, kind):
    s = Switch(vn, home, tmp_path, monkeypatch, lease=False)
    assert vn.switch(s.root, kind, "on") == 0
    assert not [e for e in s.events if e.startswith("spawn:")]
    assert "with the next voice connector" in capsys.readouterr().out


def test_barge_off_kills_the_trigger(vn, home, tmp_path, monkeypatch):
    s = Switch(vn, home, tmp_path, monkeypatch)
    vn.switch(s.root, "barge", "on")
    s.events.clear()
    assert vn.switch(s.root, "barge", "off") == 0
    assert "VOICEMODE_VOICE_BARGE=false" in s.env_file()
    assert s.events == ["kill:barge"]
    assert "barge" not in s.state()


def test_pill_off_sets_the_stop_event_first_then_kills_what_is_left(vn, home, tmp_path, monkeypatch):
    s = Switch(vn, home, tmp_path, monkeypatch)
    vn.switch(s.root, "pill", "on")
    pid = s.state()["pill"]["pid"]
    s.events.clear()
    assert vn.switch(s.root, "pill", "off") == 0
    assert s.events == ["stop-event", "kill:pill"]          # still there after the wait
    assert "pill" not in s.state()
    # A window that closes on the event is not killed.
    vn.switch(s.root, "pill", "on")
    s.events.clear()
    pid = s.state()["pill"]["pid"]
    monkeypatch.setattr(vn, "set_pill_stop_event", lambda root: s.world.commands.pop(pid) and True)
    vn.switch(s.root, "pill", "off")
    assert s.events == []


def test_ptt_off_kills_the_listener(vn, home, tmp_path, monkeypatch):
    s = Switch(vn, home, tmp_path, monkeypatch)
    vn.switch(s.root, "ptt", "on")
    s.events.clear()
    assert vn.switch(s.root, "ptt", "off") == 0
    assert "VOICEMODE_PTT_ENABLED=false" in s.env_file()
    assert s.events == ["kill:ptt"]                          # no stop event: it holds no state
    assert "ptt" not in s.state()


def test_ptt_status_is_on_or_off_then_the_listener(vn, home, tmp_path, monkeypatch, capsys):
    s = Switch(vn, home, tmp_path, monkeypatch)
    vn.switch(s.root, "ptt", "status")
    assert capsys.readouterr().out.splitlines() == ["off", "listener: not running"]
    vn.switch(s.root, "ptt", "on")
    capsys.readouterr()
    vn.switch(s.root, "ptt", "status")
    pid = s.state()["ptt"]["pid"]
    assert capsys.readouterr().out.splitlines() == ["on", f"listener: running (process {pid})"]


def test_barge_on_refuses_without_the_silero_model(vn, home, tmp_path, monkeypatch, capsys):
    s = Switch(vn, home, tmp_path, monkeypatch)
    (s.runtime / vn.SILERO).unlink()
    with pytest.raises(vn.VoiceError, match="Silero model"):
        vn.switch(s.root, "barge", "on")          # main() prints it and exits 1
    assert not (home / ".voicemode" / "voicemode.env").exists()
    assert not [e for e in s.events if e.startswith("spawn:")]


def _barge_cannot_run():
    import ast
    tree = ast.parse((REPO / "patches" / "voice_barge.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "CANNOT_RUN":
            return ast.literal_eval(node.value)
    raise AssertionError("CANNOT_RUN not found")


def test_barge_status_is_voice_barges_one_line_contract_then_the_trigger(vn, home, tmp_path, monkeypatch, capsys):
    s = Switch(vn, home, tmp_path, monkeypatch)
    assert vn.switch(s.root, "barge", "status") == 0
    assert capsys.readouterr().out.splitlines() == ["off", "trigger: not running"]
    vn.switch(s.root, "barge", "on")
    capsys.readouterr()
    vn.switch(s.root, "barge", "status")
    pid = s.state()["barge"]["pid"]
    assert capsys.readouterr().out.splitlines() == ["on", f"trigger: running (process {pid})"]
    monkeypatch.setattr(vn.resolver(), "capture_backend", lambda: None)
    vn.switch(s.root, "barge", "status")
    line = capsys.readouterr().out.splitlines()[0]
    assert line == _barge_cannot_run() and line.startswith("cannot run: ")


def test_pill_status_is_on_or_off_then_the_window(vn, home, tmp_path, monkeypatch, capsys):
    s = Switch(vn, home, tmp_path, monkeypatch)
    vn.switch(s.root, "pill", "status")
    assert capsys.readouterr().out.splitlines() == ["off", "window: not running"]
    vn.switch(s.root, "pill", "on")
    capsys.readouterr()
    vn.switch(s.root, "pill", "status")
    pid = s.state()["pill"]["pid"]
    assert capsys.readouterr().out.splitlines() == ["on", f"window: running (process {pid})"]


def test_switch_says_when_the_environment_overrides_the_file(vn, home, tmp_path, monkeypatch, capsys):
    s = Switch(vn, home, tmp_path, monkeypatch, lease=False)
    monkeypatch.setenv("VOICEMODE_PILL", "false")
    vn.switch(s.root, "pill", "on")
    out = capsys.readouterr().out
    assert "VOICEMODE_PILL=false" in out and "environment" in out
    # Under a connector, only what the connector's own environment held counts: voice-mode
    # copies voicemode.env's old values into its environment, which is no override.
    monkeypatch.setenv("VOICEMODE_NATIVE_ENV_SETTINGS", "")
    vn.switch(s.root, "pill", "on")
    assert "environment" not in capsys.readouterr().out
    monkeypatch.setenv("VOICEMODE_NATIVE_ENV_SETTINGS", "VOICEMODE_PILL")
    vn.switch(s.root, "pill", "on")
    assert "VOICEMODE_PILL=false" in capsys.readouterr().out


def test_runtime_environment_passes_the_native_barge_command(vn, tmp_path, monkeypatch):
    monkeypatch.setattr(vn, "WINDOWS", True)
    monkeypatch.setenv("VOICEMODE_PILL", "true")
    monkeypatch.delenv("VOICEMODE_NATIVE_ENV_SETTINGS", raising=False)
    root = tmp_path / "root"
    runtime = root / "runtime" / "abc"
    env = vn.runtime_environment(runtime)
    python = str(vn.venv_program(runtime, "python"))
    assert json.loads(env["VOICEMODE_BARGE_CONTROL_COMMAND"]) == [python, str(vn.SCRIPT), "barge"]
    assert env["VOICEMODE_BARGE_PYTHON"] == python
    assert env["VOICEMODE_NATIVE_ROOT"] == str(root)
    assert env["VOICEMODE_NATIVE_ENV_SETTINGS"] == "VOICEMODE_PILL"
    monkeypatch.setattr(vn, "ALLOW_OUTSIDE", True)
    assert json.loads(vn.runtime_environment(runtime)["VOICEMODE_BARGE_CONTROL_COMMAND"]) == [
        python, str(vn.SCRIPT), "--allow-root-outside-profile", "barge"]


def test_launcher_env_reaches_voice_mode_and_the_pill_backend(vn, home, tmp_path, monkeypatch):
    for name in vn.TOOL_SETTINGS:  # another test in this process may have loaded a voicemode.env
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "root"
    world = FakeWorld(vn, monkeypatch, root)
    runtime = _fake_runtime(vn, root)
    envs = {}
    world.observers.append(lambda command, job, kwargs: envs.update({world.kind_of(command): kwargs["env"]}))
    monkeypatch.setattr(vn, "configured_runtime", lambda root, expected: (runtime, True))
    monkeypatch.setattr(vn, "check_models", lambda runtime: None)
    monkeypatch.setenv("VOICEMODE_PILL", "true")
    assert vn.launch(root) == 0
    # The pill window is now started by voice-mode itself (pill_autostart), so its backend
    # inherits voice-mode's environment: reaching voice-mode is reaching the pill.
    assert "pill" not in envs
    command = json.loads(envs["voice-mode"]["VOICEMODE_BARGE_CONTROL_COMMAND"])
    assert command[1:] == [str(vn.SCRIPT), "barge"]
    assert envs["voice-mode"]["VOICEMODE_TOOLS_ENABLED"] == "converse,service,voice_barge_tool"


def test_the_barge_command_is_accepted_by_voice_pill_state(vn, tmp_path, monkeypatch):
    st = _load(REPO / "patches" / "voice_pill_state.py", "vps_switch_test")
    python = tmp_path / "python"
    python.write_text("")
    monkeypatch.setenv("VOICEMODE_BARGE_CONTROL_COMMAND", json.dumps([str(python), str(vn.SCRIPT), "barge"]))
    assert st.barge_command() == ([str(python), str(vn.SCRIPT), "barge"], None)


def test_main_parses_the_switch_and_restarts_with_its_action(vn, home, tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(vn, "WINDOWS", True)
    monkeypatch.setattr(vn, "native_root", lambda: tmp_path)
    monkeypatch.setattr(vn, "restart_with_runtime_python", lambda root, args: seen.append(args) or 0)
    assert vn.main(["barge", "on"]) == 0
    assert vn.main(["pill", "status"]) == 0
    assert vn.main(["ptt", "off"]) == 0
    assert seen == [["barge", "on"], ["pill", "status"], ["ptt", "off"]]
    with pytest.raises(SystemExit):
        vn.main(["barge", "maybe"])
    with pytest.raises(SystemExit):
        vn.main(["barge"])


def test_status_reports_disk_use_counting_hard_links_once(vn, tmp_path):
    root = tmp_path / "root"
    (root / "downloads" / "x").mkdir(parents=True)
    (root / "runtime" / "abc").mkdir(parents=True)
    (root / "downloads" / "x" / "m").write_bytes(b"1" * 100)
    import os
    os.link(root / "downloads" / "x" / "m", root / "runtime" / "abc" / "m")
    (root / "runtime" / "abc" / "f").write_bytes(b"2" * 10)
    use = vn.disk_use(root)
    assert use["root"] == 110 and use["downloads"] == 100 and use["runtime"] == 110


def test_info_names_a_python_that_survives_runtime_rebuilds(vn, tmp_path, monkeypatch, capsys):
    root = tmp_path / "root"
    monkeypatch.setattr(vn, "WINDOWS", True)
    link = root / "python" / "cpython-3.12-windows-x86_64-none"
    (link / "").mkdir(parents=True)
    (link / "python.exe").write_text("")
    (root / "active.json").write_text(json.dumps({"runtime": "abc-1", "manifest": "m"}))
    assert vn.info(root) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["root"] == str(root) and data["script"] == str(vn.SCRIPT)
    assert data["python"] == str(link / "python.exe")
    assert data["runtime"] == str(root / "runtime" / "abc-1")
    (link / "python.exe").unlink()
    vn.info(root)
    assert json.loads(capsys.readouterr().out)["python"] is None


def test_barge_on_adds_its_tool_to_an_existing_whitelist_only(vn, home, tmp_path, monkeypatch):
    s = Switch(vn, home, tmp_path, monkeypatch, lease=False)
    vn.switch(s.root, "barge", "on")
    assert "VOICEMODE_TOOLS_ENABLED" not in s.env_file()          # never creates one
    _env_file(home, "VOICEMODE_TOOLS_ENABLED=converse,service\n")
    vn.switch(s.root, "barge", "on")
    assert "VOICEMODE_TOOLS_ENABLED=converse,service,voice_barge_tool\n" in s.env_file()
    vn.switch(s.root, "barge", "on")
    assert s.env_file().count("voice_barge_tool") == 1


def test_voice_mode_gets_the_barge_tool_unless_the_user_chose_tools(vn, home, monkeypatch):
    for name in vn.TOOL_SETTINGS:
        monkeypatch.delenv(name, raising=False)
    assert vn.tool_selection() == {"VOICEMODE_TOOLS_ENABLED": "converse,service,voice_barge_tool"}
    monkeypatch.setenv("VOICEMODE_TOOLS_DISABLED", "statistics")
    assert vn.tool_selection() == {}
    monkeypatch.delenv("VOICEMODE_TOOLS_DISABLED")
    _env_file(home, "VOICEMODE_TOOLS_ENABLED=converse\n")
    assert vn.tool_selection() == {}


def test_upstream_default_tools_are_what_tool_selection_extends():
    """DEFAULT_TOOLS is upstream 8.12's default plus voice_barge_tool: check against its source."""
    import os
    src = os.environ.get("VM812_SRC")
    init = Path(src).parent / "pkg" / "voice_mode" / "tools" / "__init__.py" if src else None
    if not init or not init.is_file():
        pytest.skip("needs VM812_SRC beside a pkg/voice_mode tree")
    assert 'default_tools = {"converse", "service"}' in init.read_text(encoding="utf-8")


# -- controller additions: control channel and streaming (2026-11-06) ----------

def _clear(monkeypatch, *names):
    for name in names:
        monkeypatch.delenv(name, raising=False)


SPEECH_SETTINGS = ("VOICEMODE_CONTROL_CHANNEL_ENABLED", "VOICEMODE_STREAMING_ENABLED")


@pytest.mark.parametrize("enabled", ["VOICEMODE_PILL", "VOICEMODE_VOICE_BARGE", "VOICEMODE_PTT_ENABLED"])
def test_control_channel_is_on_when_an_optional_service_is(vn, home, monkeypatch, enabled):
    _clear(monkeypatch, *SPEECH_SETTINGS, "VOICEMODE_PTT_ENABLED")
    assert "VOICEMODE_CONTROL_CHANNEL_ENABLED" not in vn.voice_mode_settings()
    monkeypatch.setenv(enabled, "true")
    assert vn.voice_mode_settings()["VOICEMODE_CONTROL_CHANNEL_ENABLED"] == "true"


def test_control_channel_follows_the_file_the_switch_writes(vn, home, monkeypatch):
    _clear(monkeypatch, *SPEECH_SETTINGS)
    _env_file(home, "VOICEMODE_PILL=true\n")
    assert vn.voice_mode_settings()["VOICEMODE_CONTROL_CHANNEL_ENABLED"] == "true"


@pytest.mark.parametrize("where", ["env", "file"])
def test_an_explicit_control_channel_or_streaming_value_is_kept(vn, home, monkeypatch, where):
    _clear(monkeypatch, *SPEECH_SETTINGS)
    monkeypatch.setenv("VOICEMODE_VOICE_BARGE", "true")
    if where == "env":
        monkeypatch.setenv("VOICEMODE_CONTROL_CHANNEL_ENABLED", "false")
        monkeypatch.setenv("VOICEMODE_STREAMING_ENABLED", "true")
    else:
        _env_file(home, "VOICEMODE_CONTROL_CHANNEL_ENABLED=false\nVOICEMODE_STREAMING_ENABLED=true\n")
    assert vn.voice_mode_settings() == {}


def test_barge_turns_streaming_off_unless_the_user_set_it(vn, home, monkeypatch):
    _clear(monkeypatch, *SPEECH_SETTINGS)
    monkeypatch.setenv("VOICEMODE_PILL", "true")
    assert "VOICEMODE_STREAMING_ENABLED" not in vn.voice_mode_settings()   # the pill alone: streaming stays
    monkeypatch.setenv("VOICEMODE_VOICE_BARGE", "true")
    assert vn.voice_mode_settings()["VOICEMODE_STREAMING_ENABLED"] == "false"


def test_voice_mode_gets_the_control_channel_and_streaming_settings(vn, home, tmp_path, monkeypatch):
    _clear(monkeypatch, *SPEECH_SETTINGS, "VOICEMODE_PILL", "VOICEMODE_PTT_ENABLED")
    monkeypatch.setenv("VOICEMODE_VOICE_BARGE", "true")
    root = tmp_path / "root"
    world = FakeWorld(vn, monkeypatch, root)
    runtime = _fake_runtime(vn, root)
    envs = {}
    world.observers.append(lambda command, job, kwargs: envs.update({world.kind_of(command): kwargs["env"]}))
    monkeypatch.setattr(vn, "configured_runtime", lambda root, expected: (runtime, True))
    monkeypatch.setattr(vn, "check_models", lambda runtime: None)
    assert vn.launch(root) == 0
    assert envs["voice-mode"]["VOICEMODE_CONTROL_CHANNEL_ENABLED"] == "true"
    assert envs["voice-mode"]["VOICEMODE_STREAMING_ENABLED"] == "false"


def test_switch_on_says_when_a_running_connector_has_no_control_channel(vn, home, tmp_path, monkeypatch, capsys):
    s = Switch(vn, home, tmp_path, monkeypatch)
    lease = s.root / "run" / "clients" / str(vn.os.getpid())
    lease.write_text(json.dumps({"runtime": str(s.runtime), "control_channel": False}))
    vn.switch(s.root, "barge", "on")
    assert "reconnect" in capsys.readouterr().out
    vn.switch(s.root, "barge", "off")
    lease.write_text(json.dumps({"runtime": str(s.runtime), "control_channel": True}))
    capsys.readouterr()
    vn.switch(s.root, "barge", "on")
    assert "reconnect" not in capsys.readouterr().out


def test_native_control_channel_needs_the_pipe_transport_in_the_native_manifest(vn):
    """FELIX point 5: the launcher may turn the channel on natively, and Windows has no
    AF_UNIX, so the native manifest must carry the named-pipe transport."""
    assert "VOICEMODE_CONTROL_CHANNEL_ENABLED" in (vn.SCRIPT.read_text(encoding="utf-8"))
    steps = json.loads((REPO / "patches" / "manifest.windows.json").read_text(encoding="utf-8"))["steps"]
    patches = {s.get("patch") for s in steps}
    copies = {s.get("copy") for s in steps}
    assert "patch_control_transport.py" in patches and "control_transport.py" in copies
    targets = next(s["targets"] for s in steps if s.get("patch") == "patch_control_transport.py")
    assert "control_socket.py" in targets


# -- fix round 1 (review of task 8) -------------------------------------------

@pytest.mark.parametrize("which", ["home", "localappdata", "ancestor"])
def test_native_root_refuses_the_profile_localappdata_and_their_ancestors(vn, home, tmp_path, monkeypatch, which):
    local = home / "AppData" / "Local"
    local.mkdir(parents=True)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    root = {"home": home, "localappdata": local, "ancestor": home.parent}[which]
    monkeypatch.setenv("VOICEMODE_NATIVE_ROOT", str(root))
    with pytest.raises(vn.VoiceError, match="holds your|is your"):
        vn.native_root(allow_outside=True)
    monkeypatch.setenv("VOICEMODE_NATIVE_ROOT", str(local / "voicemode-local-native"))
    assert vn.native_root() == (local / "voicemode-local-native").resolve()


def _root_with_data(path):
    (path / "runtime").mkdir(parents=True)
    (path / "active.json").write_text("{}")
    return path


@pytest.fixture
def protected(home, tmp_path, monkeypatch):
    local = home / "AppData" / "Local"
    (local / "voicemode-local").mkdir(parents=True)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    return local


def test_remove_data_deletes_an_install_folder(vn, protected, capsys):
    root = _root_with_data(protected / "voicemode-local-native")
    assert vn.remove_data(root) == 0
    assert not root.exists() and (protected / "voicemode-local").is_dir()


@pytest.mark.parametrize("target", ["wsl-pill", "inside-wsl-pill", "localappdata", "profile", "felix", "inside-felix"])
def test_remove_data_never_touches_a_protected_folder_or_one_that_holds_it(vn, home, protected, monkeypatch, target):
    felix = home / ".felix"
    (felix / "voice").mkdir(parents=True)
    path = {"wsl-pill": protected / "voicemode-local",
            "inside-wsl-pill": protected / "voicemode-local" / "x",
            "localappdata": protected, "profile": home, "felix": felix, "inside-felix": felix / "voice"}[target]
    _root_with_data(path)
    with pytest.raises(vn.VoiceError, match="Refusing"):
        vn.remove_data(path)
    assert path.is_dir() and (protected / "voicemode-local").is_dir() and felix.is_dir()


def test_remove_data_refuses_a_folder_that_contains_a_protected_one(vn, home, protected, tmp_path, monkeypatch):
    # An ancestor of LOCALAPPDATA's voicemode-local, given as the root from outside the profile.
    outer = tmp_path / "outer"
    inner = outer / "voicemode-local"
    inner.mkdir(parents=True)
    _root_with_data(outer)
    monkeypatch.setenv("LOCALAPPDATA", str(outer))
    with pytest.raises(vn.VoiceError, match="Refusing"):
        vn.remove_data(outer)
    assert inner.is_dir()


def test_remove_data_refuses_a_folder_that_is_not_an_install(vn, protected):
    stranger = protected / "something"
    stranger.mkdir()
    with pytest.raises(vn.VoiceError, match="not an install"):
        vn.remove_data(stranger)
    assert stranger.is_dir()


def test_remove_data_is_not_restarted_under_the_runtime_it_deletes(vn, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(vn, "WINDOWS", True)
    monkeypatch.setattr(vn, "native_root", lambda: tmp_path)
    monkeypatch.setattr(vn, "restart_with_runtime_python", lambda root, args: calls.append(args) or 0)
    monkeypatch.setattr(vn, "remove_data", lambda root: 0)
    assert vn.main(["remove-data"]) == 0
    assert calls == []


def test_info_names_the_default_root(vn, home, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(vn, "WINDOWS", True)
    monkeypatch.setenv("LOCALAPPDATA", str(home / "L"))
    vn.info(tmp_path / "root")
    data = json.loads(capsys.readouterr().out)
    assert data["default_root"] == str(home / "L" / "voicemode-local-native")


_WRITER = r'''
import importlib.util, sys
spec = importlib.util.spec_from_file_location("vn_writer", sys.argv[1])
vn = importlib.util.module_from_spec(spec); spec.loader.exec_module(vn)
from pathlib import Path
for i in range(int(sys.argv[4])):
    vn.write_setting(Path(sys.argv[2]), sys.argv[3], str(i))
'''


def test_two_concurrent_writers_keep_both_keys(vn, tmp_path):
    import subprocess
    path = tmp_path / ".voicemode" / "voicemode.env"
    path.parent.mkdir()
    path.write_text("KEEP=1\n")
    writer = tmp_path / "writer.py"
    writer.write_text(_WRITER)
    procs = [subprocess.Popen([sys.executable, str(writer), str(vn.SCRIPT), str(path), key, "150"])
             for key in ("VOICEMODE_PILL", "VOICEMODE_VOICE_BARGE")]
    assert [p.wait(120) for p in procs] == [0, 0]
    text = path.read_text()
    assert "KEEP=1\n" in text and "VOICEMODE_PILL=149\n" in text and "VOICEMODE_VOICE_BARGE=149\n" in text
    assert (path.parent / "voicemode.env.lock").exists()


def test_settings_lock_is_held_around_read_modify_write(vn, tmp_path, monkeypatch):
    held = []
    real = vn.exclusive

    @vn.contextlib.contextmanager
    def watched(path):
        with real(path):
            held.append(path)
            yield
            held.append("released")

    monkeypatch.setattr(vn, "exclusive", watched)
    path = tmp_path / "voicemode.env"
    path.write_text("VOICEMODE_TOOLS_ENABLED=converse\n")
    vn.write_setting(path, "VOICEMODE_PILL", "true")
    vn.expose_barge_tool(path)
    assert held == [tmp_path / "voicemode.env.lock", "released"] * 2
    assert "converse,voice_barge_tool" in path.read_text()


class _FakeJob:
    def assign(self, handle):
        raise OSError("access denied")


class _FakePopen:
    killed = []

    def __init__(self, command, **kwargs):
        self.pid, self._handle = 4242, 7
        self.stdin = self.stdout = self.stderr = None

    def kill(self):
        _FakePopen.killed.append(self.pid)

    def wait(self, timeout=None):
        return -9


def test_a_failed_job_assignment_kills_the_child_and_raises(vn, monkeypatch):
    monkeypatch.setattr(vn, "WINDOWS", True)
    monkeypatch.setattr(sv_winjob, "WINDOWS", True)
    monkeypatch.setattr(vn.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(sv_winjob, "_resume_threads", lambda pid: (_ for _ in ()).throw(AssertionError("resumed")))
    _FakePopen.killed.clear()
    with pytest.raises(vn.VoiceError, match="Job Object"):
        vn.start_in_job(["x.exe"], _FakeJob())
    assert _FakePopen.killed == [4242]


@pytest.mark.parametrize("value,on", [("true", True), ("TRUE", True), ('"true"', True), (" True ", True),
                                      ("'true'", True), ("yes", False), ("", False)])
def test_on_parsing_matches_barge_state(vn, monkeypatch, value, on):
    monkeypatch.setattr(vn, "setting", lambda name, default=None: value if name == "VOICEMODE_PILL" else "")
    assert ("pill" in vn.optional_services()) is on
    assert vn.truthy(value) is on


# -- carried from FELIX (Task 8) ------------------------------------------------

def test_launch_stops_a_voice_server_that_ignores_the_stop_request(vn, home, tmp_path, monkeypatch):
    """FELIX 454f20d0e tests/repo/test_voicemode_setup.py
    test_launch_stops_a_voice_server_that_ignores_the_stop_request, its launch half (the
    services half is vml_supervisor's test_stop_ends_a_service_that_ignores_the_stop_request).
    A stop request reaches launch; voice-mode ignores the SIGTERM launch forwards; after
    STOP_TIMEOUT launch kills it, releases its lease, and the last connector's services stop.
    Adapted: the speech services are FakeWorld's; voice-mode is a real process."""
    import signal
    import subprocess
    import threading
    import time
    root = tmp_path / "root"
    world = FakeWorld(vn, monkeypatch, root)
    runtime = _fake_runtime(vn, root)
    monkeypatch.setattr(vn, "STOP_TIMEOUT", 1)
    monkeypatch.setattr(vn, "configured_runtime", lambda root, expected: (runtime, True))
    monkeypatch.setattr(vn, "check_models", lambda runtime: None)
    begin = []
    # This signals the whole test process, so it must not outlive this test. It starts once the
    # voice server is ignoring the signal.
    stop_request = threading.Timer(0.5, lambda: signal.raise_signal(signal.SIGTERM))

    def voice_mode(command, kwargs):
        child = subprocess.Popen([sys.executable, "-c",
                                  "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                                  "print('ready', flush=True); time.sleep(60)"],
                                 stdout=subprocess.PIPE, text=True)
        assert child.stdout.readline().strip() == "ready"
        begin.append(time.monotonic())
        stop_request.start()
        return child

    world.replace["voice-mode"] = voice_mode
    try:
        assert vn.launch(root) == -signal.SIGKILL
    finally:
        stop_request.cancel()
        if stop_request.is_alive():
            stop_request.join()  # A timer already past its check would still send the signal.
    assert time.monotonic() - begin[0] < 15
    assert json.loads((root / "run/state.json").read_text()) == {}
    assert sorted(e for e in world.events if e.startswith("kill:")) == ["kill:stt", "kill:tts"]


def test_a_refused_breakaway_is_said_not_silent(vn, monkeypatch, capsys):
    """Task 4 ruling: the old spawn retried without breakaway and said nothing. Where the
    connector's job forbids breakaway, the child is also in that job and ends with it: said."""
    monkeypatch.setattr(sv_winjob, "WINDOWS", True)
    flags = []

    class Popen:
        def __init__(self, command, creationflags=0, **kwargs):
            flags.append(creationflags)
            if creationflags & sv_winjob.CREATE_BREAKAWAY_FROM_JOB:
                raise PermissionError("breakaway is not allowed")
            self.pid, self._handle = 4243, 8

    monkeypatch.setattr(vn.subprocess, "Popen", Popen)
    monkeypatch.setattr(sv_winjob, "_resume_threads", lambda pid: None)
    job = type("Job", (), {"assign": lambda self, handle: None})()
    process = vn.start_in_job(["x.exe"], job)
    assert process.pid == 4243 and len(flags) == 2
    err = capsys.readouterr().err
    assert "breakaway" in err and "x.exe" in err


def test_the_relaunched_launcher_keeps_its_lease_marker_with_the_allow_flag(vn, tmp_path, monkeypatch):
    """A lease is alive while its launcher's command holds `voicemode_native.py launch`. The
    relaunch under the runtime's Python put --allow-root-outside-profile between the two, so
    the marker never matched and only voice-mode's own marker kept the lease (found 2026-11-13)."""
    monkeypatch.setattr(vn, "WINDOWS", True)
    monkeypatch.setattr(vn, "ALLOW_OUTSIDE", True)
    monkeypatch.setitem(sys.modules, "psutil", None)          # this Python cannot inspect
    root = tmp_path / "root"
    (root / "runtime" / "abc" / ".venv" / "Scripts").mkdir(parents=True)
    (root / "runtime" / "abc" / ".venv" / "Scripts" / "python.exe").write_text("")
    (root / "active.json").write_text(json.dumps({"runtime": "abc"}))
    seen = []
    done = type("P", (), {"pid": 9, "wait": lambda self: 0})()
    monkeypatch.setattr(sv_winjob, "Job", lambda **kw: None)
    monkeypatch.setattr(sv_winjob, "spawn", lambda command, job, **kw: seen.append(command) or (done, Result("breakaway")))
    assert vn.restart_with_runtime_python(root, ["launch"]) == 0
    line = " ".join(seen[0])
    assert "voicemode_native.py launch" in line and "--allow-root-outside-profile" in line


def _host_mode(vn, root, kind):
    """What the voice host's next look decides for a helper (its read_mode hook)."""
    from vml_supervisor import hooks as hook_calls
    profile = vn.profile_of(root)
    hooks, problems = load_hooks(profile)
    assert not problems
    return hook_calls.read_mode(hooks, profile, kind)


def test_a_switch_is_not_undone_by_the_hosts_next_look(vn, home, tmp_path, monkeypatch, capsys):
    """The connector's environment says false (it wins over voicemode.env at launch); `ptt on`
    still starts the listener, as before, and the host's next look must keep it (Windows test
    test_ptt_switch_starts_and_stops_the_listener_in_the_services_job runs exactly this)."""
    s = Switch(vn, home, tmp_path, monkeypatch)
    monkeypatch.setenv("VOICEMODE_PTT_ENABLED", "false")
    monkeypatch.setenv("VOICEMODE_NATIVE_ENV_SETTINGS", "VOICEMODE_PTT_ENABLED")
    assert _host_mode(vn, s.root, "ptt") == ("off", None)
    assert vn.switch(s.root, "ptt", "on") == 0
    assert "started (process" in capsys.readouterr().out
    assert _host_mode(vn, s.root, "ptt") == ("true", None)
    vn.switch(s.root, "ptt", "off")
    assert _host_mode(vn, s.root, "ptt") == ("off", None)


def test_launch_decides_the_modes_again_from_its_settings(vn, home, tmp_path, monkeypatch):
    """An earlier switch's decision does not outlive the next launch: launch reads its settings,
    the environment first, as it always did."""
    root = tmp_path / "root"
    world = FakeWorld(vn, monkeypatch, root)
    runtime = _fake_runtime(vn, root)
    monkeypatch.setattr(vn, "configured_runtime", lambda root, expected: (runtime, True))
    monkeypatch.setattr(vn, "check_models", lambda runtime: None)
    (root / "run").mkdir(parents=True)
    (root / "run" / "modes.json").write_text(json.dumps({"ptt": "off", "barge": "off"}))
    monkeypatch.setenv("VOICEMODE_PTT_ENABLED", "true")
    assert vn.launch(root) == 0
    assert "spawn:ptt" in world.events and "spawn:barge" not in world.events


def test_setup_prunes_nothing_when_it_cannot_check_the_leases(vn, tmp_path, monkeypatch, capsys):
    """Setup runs under the user's Python, which may lack psutil. Before Task 8 a lease lookup
    there raised; through the package every lease would look dead and be deleted, and the
    runtime a running connector uses could go. Nothing is pruned, and that is said."""
    root = tmp_path / "root"
    for name in ("active", "leased", "old"):
        (root / "runtime" / name / ".venv").mkdir(parents=True)
    clients = root / "run" / "clients"
    clients.mkdir(parents=True)
    (clients / "50").write_text(json.dumps({"runtime": str(root / "runtime" / "leased")}))
    monkeypatch.setattr(sv_procs, "require_psutil", lambda: Result("psutil_missing", "no psutil"))
    _fake_commands(monkeypatch, vn, lambda pid: None)   # what a lookup gives without psutil
    assert vn.prune_runtimes(root, "active") == []
    assert sorted(p.name for p in (root / "runtime").iterdir()) == ["active", "leased", "old"]
    assert (clients / "50").exists()
    assert "kept" in capsys.readouterr().err
    (clients / "50").unlink()                           # nothing to look up: it prunes as before
    assert sorted(vn.prune_runtimes(root, "active")) == ["leased", "old"]


def test_a_failed_launch_never_leaves_a_voice_mode_that_ignores_sigterm(vn, home, tmp_path, monkeypatch):
    """launch's own cleanup on POSIX: SIGTERM, and after STOP_TIMEOUT a kill (before Task 8 a
    voice-mode ignoring SIGTERM was left running when launch failed after starting it)."""
    import subprocess
    root = tmp_path / "root"
    world = FakeWorld(vn, monkeypatch, root)
    runtime = _fake_runtime(vn, root)
    monkeypatch.setattr(vn, "WINDOWS", False)
    monkeypatch.setattr(vn, "STOP_TIMEOUT", 1)
    monkeypatch.setattr(vn, "configured_runtime", lambda root, expected: (runtime, True))
    monkeypatch.setattr(vn, "check_models", lambda runtime: None)
    started = []

    def voice_mode(command, kwargs):
        child = subprocess.Popen([sys.executable, "-c",
                                  "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                                  "print('ready', flush=True); time.sleep(60)"],
                                 stdout=subprocess.PIPE, text=True)
        assert child.stdout.readline().strip() == "ready"
        started.append(child)
        return child

    world.replace["voice-mode"] = voice_mode
    real = sv_state.atomic_json

    def lease_write(path, value):
        if isinstance(value, dict) and "child" in value:
            raise OSError("disk full")
        return real(path, value)

    monkeypatch.setattr(sv_state, "atomic_json", lease_write)
    with pytest.raises(OSError, match="disk full"):
        vn.launch(root)
    assert started and started[0].poll() == -9


def test_the_launcher_compiles_without_warnings():
    """Every MCP start compiles it: a SyntaxWarning (an invalid escape such as run\\m in a
    docstring) lands on the connector's stderr each time (seen in the Windows run, 2026-11-13)."""
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        compile(SCRIPT.read_text(encoding="utf-8"), str(SCRIPT), "exec")


def test_every_launcher_name_the_windows_only_tests_use_exists(vn):
    """The *_windows.py tests run only on Windows; a name they use from the launcher that is
    gone fails there and nowhere else (Task 8 deleted background_options and kill_tree, and
    test_pill_windows_native_windows.py failed 10 times on the laptop, 2026-11-13)."""
    used = set()
    for path in sorted((REPO / "tests").glob("*_windows.py")):
        used |= {(path.name, name) for name in re.findall(r"\bvn\.([A-Za-z_]\w*)", path.read_text(encoding="utf-8"))}
    assert used   # the check sees something
    assert [(f, name) for f, name in sorted(used) if not hasattr(vn, name)] == []
