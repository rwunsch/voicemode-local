"""vml_profiles/claude.py: voicemode-local's Claude Code profile over vml_supervisor.

The values are the native launcher's (scripts/windows/voicemode_native.py before Task 8): its
ports, markers, Job Object name, timeouts and commands, now as one HostProfile, plus the two
hooks the host calls: read_mode and environment.
"""
import hashlib
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from vml_profiles import claude  # noqa: E402
from vml_supervisor import hooks as hook_calls  # noqa: E402
from vml_supervisor.hooks import load_hooks  # noqa: E402
from vml_supervisor.profile import load_profile  # noqa: E402

SCRIPT = REPO / "scripts" / "windows" / "voicemode_native.py"


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / ".voicemode").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    for key in ("VOICEMODE_VOICE_BARGE", "VOICEMODE_PTT_ENABLED", "VOICEMODE_NATIVE_ENV_SETTINGS"):
        monkeypatch.delenv(key, raising=False)
    return h


def _profile(tmp_path, **kw):
    root = tmp_path / "root"
    return claude.personal_profile(root, root / "runtime" / "abc", **kw)


def test_the_profile_holds_the_launchers_values(tmp_path):
    root = tmp_path / "root"
    runtime = root / "runtime" / "abc"
    p = claude.personal_profile(root, runtime, windows=True)
    assert p.name == "voicemode-local"
    assert p.root == root and p.run_dir == root / "run" and p.runtime_dir == runtime
    assert p.working_dir == root / "voice-mode"
    assert p.python == runtime / ".venv" / "Scripts" / "python.exe"
    assert p.services["tts"].port_range == (28880, 28899)
    assert p.services["stt"].port_range == (22022, 22041)
    assert (p.services["tts"].ready_path, p.services["tts"].ready_kind) == ("/v1/models", "kokoro")
    assert (p.services["stt"].ready_path, p.services["stt"].ready_kind) == ("/health", "whisper")
    assert set(range(18880, 18900)) | set(range(12022, 12042)) <= set(p.reserved_ports)
    assert p.port_override.kind == "file" and p.port_override.path == root / "run" / "ports.json"
    assert (p.start_timeout, p.stop_timeout, p.watch_interval) == (60, 5, 3)
    assert p.restart_limit == (3, 300)
    assert p.lease_markers == ("voicemode_native.py launch", "voicemode_native.py serve")
    assert p.voice_server_marker == "voice-mode"
    assert p.host_command == [str(p.python), str(SCRIPT), "host"]
    assert p.host_marker == "voicemode_native.py host" and p.host_marker in " ".join(p.host_command)
    assert p.watch_command[1:] == [str(SCRIPT), "watch"] and p.watch_marker == "voicemode_native.py watch"
    assert p.watch_marker in " ".join(p.watch_command)
    assert p.hooks == {"read_mode": "vml_profiles.claude:read_mode",
                       "environment": "vml_profiles.claude:environment"}
    assert p.hook_paths == (REPO,)
    assert p.validate() == []


def test_the_job_name_is_the_one_a_running_install_already_holds(tmp_path):
    """voicemode_native.py before Task 8: sha256 of str(root).lower(), 16 hex digits."""
    root = tmp_path / "Root With Case"
    want = "Local\\voicemode-local-services-" + hashlib.sha256(str(root).lower().encode()).hexdigest()[:16]
    assert claude.personal_profile(root, root / "runtime" / "x").job_name == want
    assert claude.job_name(root) == want


def test_the_allow_flag_reaches_the_watchdog_command(tmp_path):
    p = _profile(tmp_path, allow_outside=True)
    assert p.watch_command[-2:] == ["watch", "--allow-root-outside-profile"]


@pytest.mark.parametrize("windows", [True, False])
def test_service_commands_bind_loopback_and_can_be_owned(tmp_path, windows):
    p = _profile(tmp_path, windows=windows)
    for kind, spec in p.services.items():
        argv = spec.argv
        assert argv[argv.index("--host") + 1] == "127.0.0.1"
        assert argv[argv.index("--port") + 1] == "{port}"   # two arguments: procs.owned matches it
        assert spec.marker and spec.marker in " ".join(argv)
    runtime = p.runtime_dir
    assert p.services["tts"].argv[:2] == [str(p.python), str(runtime / "services/kokoro-onnx-server.py")]
    assert p.services["tts"].marker == str(runtime / "services/kokoro-onnx-server.py")
    assert p.services["stt"].marker == str(runtime / "models/whisper/ggml-base.bin")
    if windows:
        assert p.services["stt"].argv[0] == str(runtime / "services/whisper/whisper-server.exe")
    assert "--convert" not in p.services["stt"].argv   # it needs ffmpeg


def test_the_helpers_are_the_trigger_and_the_listener(tmp_path):
    root = tmp_path / "root"
    p = claude.personal_profile(root, root / "runtime" / "abc", windows=True)
    assert set(p.helpers) == {"barge", "ptt"}
    barge, ptt = p.helpers["barge"], p.helpers["ptt"]
    assert barge.argv == [str(p.python), "-m", "voice_mode.voice_barge",
                          str(p.runtime_dir / "models/silero/silero_vad.onnx")]
    assert barge.marker == "voice_mode.voice_barge" and barge.log == "barge.log"
    assert ptt.marker == str(REPO / "ptt-listener-windows.ps1") and ptt.log == "ptt.log"
    assert ptt.argv[ptt.argv.index("-LogPath") + 1] == str(root / "run" / "ptt-listener.log")
    for spec in (barge, ptt):
        assert spec.platforms == ("win32",) and spec.needs_mode
        assert spec.marker in " ".join(spec.argv)
        # stop_unrecorded finds an unrecorded one only by the marker AND this install's folder.
        assert str(root) in " ".join(spec.argv)
    assert claude.optional_command("ptt", p.runtime_dir, root, windows=True) == (ptt.argv, ptt.marker)
    assert claude.optional_command("barge", p.runtime_dir, root, windows=True) == (barge.argv, barge.marker)


def test_the_profile_survives_the_file_the_host_reads(tmp_path):
    p = _profile(tmp_path, env={"A": "1"})
    path = tmp_path / "host-profile.json"
    p.to_file(path)
    again, problems = load_profile(path)
    assert problems == [] and again == p


def test_the_hooks_load(tmp_path):
    hooks, problems = load_hooks(_profile(tmp_path))
    assert problems == [] and set(hooks.funcs) == {"read_mode", "environment"}


# -- read_mode ------------------------------------------------------------------

def _env_file(home, text):
    (home / ".voicemode" / "voicemode.env").write_text(text, encoding="utf-8")


@pytest.mark.parametrize("value,barge,ptt", [("true", "true", "true"), (" TRUE ", "true", "true"),
                                             ('"true"', "true", "true"), ("auto", "auto", "off"),
                                             ("false", "off", "off"), ("yes", "off", "off"), ("", "off", "off")])
def test_read_mode_follows_voicemode_env(home, tmp_path, value, barge, ptt):
    p = _profile(tmp_path).to_dict()
    _env_file(home, f"VOICEMODE_VOICE_BARGE={value}\nVOICEMODE_PTT_ENABLED={value}\n")
    assert claude.read_mode(p, "barge") == barge
    assert claude.read_mode(p, "ptt") == ptt


def test_read_mode_without_a_file_is_off(home, tmp_path):
    p = _profile(tmp_path).to_dict()
    assert claude.read_mode(p, "barge") == "off" and claude.read_mode(p, "ptt") == "off"
    assert claude.read_mode(p, "pill") is None   # not a helper


def test_the_connectors_own_environment_wins_over_the_file(home, tmp_path):
    """As setting() decides at launch: a value the launcher's own environment held wins."""
    _env_file(home, "VOICEMODE_VOICE_BARGE=false\n")
    env = {"VOICEMODE_VOICE_BARGE": "true", "VOICEMODE_NATIVE_ENV_SETTINGS": "VOICEMODE_VOICE_BARGE"}
    assert claude.read_mode(_profile(tmp_path, env=env).to_dict(), "barge") == "true"
    # Under a connector, voice-mode copies voicemode.env into the environment: that is no override.
    env["VOICEMODE_NATIVE_ENV_SETTINGS"] = ""
    assert claude.read_mode(_profile(tmp_path, env=env).to_dict(), "barge") == "off"


def test_the_latest_decision_wins(home, tmp_path):
    """run/modes.json holds what launch decided at its start or a switch decided since; it wins
    over the settings, so a switch is not undone by the host's next look."""
    p = _profile(tmp_path, env={"VOICEMODE_PTT_ENABLED": "false",
                                "VOICEMODE_NATIVE_ENV_SETTINGS": "VOICEMODE_PTT_ENABLED"})
    assert claude.read_mode(p.to_dict(), "ptt") == "off"
    claude.write_modes(p.run_dir, {"ptt": "true"})
    assert claude.read_mode(p.to_dict(), "ptt") == "true"
    claude.write_modes(p.run_dir, {"barge": "auto"})   # merged, not replaced
    assert claude.read_modes(p.run_dir) == {"ptt": "true", "barge": "auto"}
    assert claude.read_mode(p.to_dict(), "barge") == "auto"


def test_a_corrupt_modes_file_is_a_hook_failure_not_a_guess(home, tmp_path):
    p = _profile(tmp_path)
    p.run_dir.mkdir(parents=True)
    (p.run_dir / "modes.json").write_text("{not json")
    hooks, _ = load_hooks(p)
    mode, problem = hook_calls.read_mode(hooks, p, "barge")
    assert mode is None and problem.code == "hook_failed"


# -- environment ----------------------------------------------------------------

def test_environment_is_the_connectors_with_the_ports_and_the_mode(tmp_path):
    env = {"HOME": "/h", "VOICEMODE_TTS_BASE_URLS": "http://127.0.0.1:28880/v1", "PATH": "/rt/bin"}
    p = _profile(tmp_path, env=env).to_dict()
    out = claude.environment(p, str(tmp_path), {"tts": 28885, "stt": 22030}, {})
    assert out["HOME"] == "/h" and out["PATH"] == "/rt/bin"
    assert out["VOICEMODE_TTS_BASE_URLS"] == "http://127.0.0.1:28885/v1"
    assert out["VOICEMODE_STT_BASE_URLS"] == "http://127.0.0.1:22030/v1"
    out = claude.environment(p, str(tmp_path), {}, {"barge": "auto"})
    assert out["VOICEMODE_VOICE_BARGE"] == "auto"
    assert out["VOICEMODE_TTS_BASE_URLS"] == "http://127.0.0.1:28880/v1"   # no port given: kept
    # The window or trigger checks its own setting: a stale false in the copy must not stop it.
    p = _profile(tmp_path, env={"VOICEMODE_PTT_ENABLED": "false"}).to_dict()
    assert claude.environment(p, str(tmp_path), {}, {"ptt": "true"})["VOICEMODE_PTT_ENABLED"] == "true"
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in out.items())


def test_the_environment_hook_passes_the_cores_shape_check(tmp_path):
    p = _profile(tmp_path, env={"VOICEMODE_A": "1"})
    hooks, _ = load_hooks(p)
    env, problem = hook_calls.environment(hooks, p, str(p.runtime_dir), {"tts": 28880}, {})
    assert problem is None and env["VOICEMODE_A"] == "1"
    assert json.dumps(env)


def test_read_mode_reads_the_connectors_voicemode_env_not_the_hosts(home, tmp_path):
    """The Windows host is started by Windows' process service with the user's default
    environment, not the connector's: the file is the one in the connector's home (profile.env)."""
    other = tmp_path / "connector-home"
    (other / ".voicemode").mkdir(parents=True)
    (other / ".voicemode" / "voicemode.env").write_text("VOICEMODE_VOICE_BARGE=auto\n", encoding="utf-8")
    _env_file(home, "VOICEMODE_VOICE_BARGE=false\n")     # the host's own home
    p = _profile(tmp_path, env={"HOME": str(other), "USERPROFILE": str(other)}).to_dict()
    assert claude.read_mode(p, "barge") == "auto"
    assert claude.read_mode(_profile(tmp_path).to_dict(), "barge") == "off"   # no home given: this one's


def test_a_corrupt_modes_file_does_not_block_a_write_and_is_said(tmp_path, capsys):
    """Every launch and switch writes modes.json: a corrupt one must not make them fail (setup
    would not repair it). The write starts from {} and names the file once on stderr."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "modes.json").write_text("{not json")
    claude.write_modes(run_dir, {"ptt": "true"})
    assert claude.read_modes(run_dir) == {"ptt": "true"}
    err = capsys.readouterr().err
    assert err.count(str(run_dir / "modes.json")) == 1 and "corrupt" in err


def test_write_modes_holds_the_service_lock(tmp_path, monkeypatch):
    import contextlib
    held = []
    real = claude.service_lock

    @contextlib.contextmanager
    def watched(run_dir):
        with real(run_dir):
            held.append(("in", claude.read_modes(run_dir)))
            yield
            held.append("out")

    monkeypatch.setattr(claude, "service_lock", watched)
    claude.write_modes(tmp_path / "run", {"barge": "auto"})
    assert held == [("in", {}), "out"]          # read and write both inside
    assert claude.read_modes(tmp_path / "run") == {"barge": "auto"}


# ----- final review I6: profile.env is an allowlist, not the user's whole environment ----------

SECRETS = {"AWS_SECRET_ACCESS_KEY": "s1", "GITHUB_TOKEN": "s2", "MY_DB_PASSWORD": "s3",
           "VOICEMODE_SOMEPROVIDER_API_KEY": "s4", "PYTHONPATH": "/x"}


@pytest.mark.parametrize("windows", [False, True])
def test_profile_env_keeps_only_the_allowlist(tmp_path, windows):
    os_keys = ({"SystemRoot": "C:\\Windows", "PATH": "C:\\bin", "USERPROFILE": "C:\\Users\\a", "TEMP": "C:\\t",
                "LOCALAPPDATA": "C:\\l", "OS": "Windows_NT", "HOME": "/ignored-on-windows"}
               if windows else {"HOME": "/home/a", "PATH": "/bin", "USER": "a", "LANG": "C.UTF-8",
                                "LC_CTYPE": "C.UTF-8", "TMPDIR": "/tmp", "SYSTEMROOT": "nope"})
    ours = {"VOICEMODE_TTS_BASE_URLS": "http://127.0.0.1:28880/v1", "VOICEMODE_NATIVE_ENV_SETTINGS": "",
            "FASTMCP_CHECK_FOR_UPDATES": "off", "PYTHONUTF8": "1", "PYTHONNOUSERSITE": "1",
            "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}
    p = _profile(tmp_path, env={**os_keys, **ours, **SECRETS}, windows=windows)
    assert not set(SECRETS) & set(p.env)
    assert {k: v for k, v in ours.items()}.items() <= p.env.items()
    kept = set(os_keys) - ({"HOME"} if windows else {"SYSTEMROOT"})
    assert set(p.env) - set(ours) == kept


def test_the_host_profile_file_holds_no_secret(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from vml_supervisor import host
    p = _profile(tmp_path, env={"HOME": str(tmp_path), "PATH": "/bin", **SECRETS}, windows=False)
    p = __import__("dataclasses").replace(p, host_command=[str(p.python), "voicemode_native.py host"])
    monkeypatch.setattr(host.subprocess, "run", lambda argv, **kw: SimpleNamespace(stdout="0 5", stderr="", returncode=0))
    monkeypatch.setattr(host, "background_options", lambda: {})
    assert host.start_host(p).code == "host_started"
    text = (p.run_dir / host.HOST_PROFILE).read_text()
    assert not [value for value in SECRETS.values() if f'"{value}"' in text]
