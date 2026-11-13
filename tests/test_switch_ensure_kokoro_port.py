"""`voicemode-switch ensure` must respect KOKORO_ONNX_PORT.

Regression: with KOKORO_ONNX_PORT=8882 and Docker Kokoro not up yet (a session
starting during boot), `ensure` started the CPU ONNX server on a hardcoded 8880.
It kept that port after Docker came up, so all TTS silently ran on CPU, and the
shared PID file stopped the real 8882 last resort from ever starting.

Runs a copy of the script with /tmp redirected and curl, docker and the venv
python stubbed, so it cannot touch the live services or their PID files.
"""
import os
import re
import stat
import subprocess
import time
from pathlib import Path

SWITCH = Path(__file__).resolve().parent.parent / "voicemode-switch"


def _stub(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/bash\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _run_ensure(tmp_path: Path, config: str, urls: str) -> tuple[str, str]:
    """Run `ensure` with every local service down; return (python_args, docker_args)."""
    root, home, bin_dir, scratch = (tmp_path / n for n in ("app", "home", "bin", "scratch"))
    for d in (root, home / ".voicemode-local", home / ".voicemode", bin_dir, scratch):
        d.mkdir(parents=True, exist_ok=True)
    py_log, docker_log = scratch / "python.log", scratch / "docker.log"

    script = root / "voicemode-switch"
    script.write_text(SWITCH.read_text().replace("/tmp/", f"{scratch}/"))
    script.chmod(0o755)
    _stub(root / ".venv/bin/python", f'echo "$@" >> "{py_log}"')
    _stub(bin_dir / "curl", "exit 1")  # every port reads as unhealthy
    _stub(bin_dir / "docker", f'echo "$@" >> "{docker_log}"')

    (home / ".voicemode-local/config").write_text(config)
    (home / ".voicemode/voicemode.env").write_text(f"VOICEMODE_TTS_BASE_URLS={urls}\n")

    env = {**os.environ, "HOME": str(home), "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    subprocess.run([str(script), "ensure", "--quiet"], env=env, check=True, timeout=30)

    # start_detached backgrounds several starts (a lock wait, the server, the barge trigger), so
    # wait until the stub's log stops growing, not merely until it exists: under full-suite load
    # the first line alone was read and the trigger's line came later (2026-11-13).
    deadline, size, quiet_since = time.time() + 6, -1, time.time()
    while time.time() < deadline:
        now = py_log.stat().st_size if py_log.exists() else -1
        if now != size:
            size, quiet_since = now, time.time()
        elif size >= 0 and time.time() - quiet_since >= 0.5:
            break
        time.sleep(0.05)
    read = lambda p: p.read_text() if p.exists() else ""
    return read(py_log), read(docker_log)


def test_onnx_on_8882_does_not_take_8880_when_docker_is_down(tmp_path):
    python_args, docker_args = _run_ensure(
        tmp_path,
        "KOKORO_ONNX=true\nKOKORO_ONNX_PORT=8882\n",
        "http://127.0.0.1:8880/v1,http://127.0.0.1:8882/v1",
    )
    assert "--port 8880" not in python_args
    assert "--port 8882" in python_args
    assert "up -d" in docker_args


def test_onnx_still_replaces_docker_when_configured_for_8880(tmp_path):
    python_args, docker_args = _run_ensure(
        tmp_path,
        "KOKORO_ONNX=true\nKOKORO_ONNX_PORT=8880\n",
        "http://127.0.0.1:8880/v1",
    )
    assert "--port 8880" in python_args
    assert "up -d" not in docker_args


def test_docker_mode_starts_docker_and_no_onnx(tmp_path):
    python_args, docker_args = _run_ensure(
        tmp_path, "KOKORO_ONNX=false\n", "http://127.0.0.1:8880/v1"
    )
    assert "kokoro-onnx-server" not in python_args
    assert "up -d" in docker_args


def _prepare_model(tmp_path: Path) -> None:
    model = tmp_path / "app/models/silero/silero_vad.onnx"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"stub")


def _run_ensure_with_model(tmp_path: Path, env_extra: str) -> str:
    _prepare_model(tmp_path)
    python_args, _ = _run_ensure(
        tmp_path,
        "KOKORO_ONNX=false\n",
        f"http://127.0.0.1:8880/v1\n{env_extra}",
    )
    return python_args


def test_ensure_starts_the_voice_barge_trigger_when_enabled(tmp_path):
    args = _run_ensure_with_model(tmp_path, "VOICEMODE_VOICE_BARGE=true")
    assert "-m voice_mode.voice_barge" in args
    assert "models/silero/silero_vad.onnx" in args


def test_ensure_leaves_the_trigger_off_by_default(tmp_path):
    args = _run_ensure_with_model(tmp_path, "")
    assert "voice_barge" not in args


def test_ensure_leaves_the_trigger_off_when_disabled(tmp_path):
    args = _run_ensure_with_model(tmp_path, "VOICEMODE_VOICE_BARGE=false")
    assert "voice_barge" not in args


def _env_after_ensure(tmp_path: Path, env_extra: str) -> str:
    _run_ensure(tmp_path, "KOKORO_ONNX=false\n", f"http://127.0.0.1:8880/v1\n{env_extra}")
    return (tmp_path / "home/.voicemode/voicemode.env").read_text()


def test_ensure_adds_the_barge_tool_to_an_existing_whitelist(tmp_path):
    env = _env_after_ensure(tmp_path, "VOICEMODE_TOOLS_ENABLED=converse,service\n")
    assert "VOICEMODE_TOOLS_ENABLED=converse,service,voice_barge_tool\n" in env


def test_ensure_does_not_duplicate_the_barge_tool(tmp_path):
    env = _env_after_ensure(tmp_path, "VOICEMODE_TOOLS_ENABLED=converse,voice_barge_tool\n")
    assert env.count("voice_barge_tool") == 1


def test_ensure_creates_no_whitelist_when_there_is_none(tmp_path):
    assert "VOICEMODE_TOOLS_ENABLED" not in _env_after_ensure(tmp_path, "")


def _run_pill(tmp_path: Path, action: str, env_text: str = "", wsl: bool = True, window: str = "running"):
    """Run `pill <action>` against a fake HOME, with the window launcher and pkill stubbed."""
    root, home, bin_dir, scratch = (tmp_path / n for n in ("app", "home", "bin", "scratch"))
    for d in (root, home / ".voicemode", bin_dir, scratch):
        d.mkdir(parents=True, exist_ok=True)
    script = root / "voicemode-switch"
    script.write_text(SWITCH.read_text().replace("/tmp/", f"{scratch}/"))
    script.chmod(0o755)
    calls = scratch / "calls.log"
    _stub(bin_dir / "pkill", f'echo "pkill $*" >> "{calls}"')
    # `window` is what the launcher prints: "running", or the wrapper's exit code.
    _stub(bin_dir / "powershell.exe", f'echo "powershell.exe $*" >> "{calls}"; printf "{window}\\r\\n"')
    _stub(bin_dir / "pgrep", "exit 1")
    _stub(bin_dir / "uname", 'echo Linux')   # these tests are Linux/WSL; on a Mac "Darwin" took the Mac path
    _stub(bin_dir / "wslpath", 'echo "\\\\\\\\wsl.localhost\\\\Ubuntu\\\\app\\\\pill-windows.ps1"')
    env_file = home / ".voicemode/voicemode.env"
    env_file.write_text(env_text)
    env = {k: v for k, v in os.environ.items() if k != "WSL_DISTRO_NAME"}
    env.update(HOME=str(home), PATH=f"{bin_dir}:{os.environ['PATH']}")
    if wsl:
        env["WSL_DISTRO_NAME"] = "Ubuntu"
    done = subprocess.run([str(script), "pill", action], env=env, capture_output=True, text=True, timeout=30)
    return done, env_file.read_text(), calls.read_text() if calls.exists() else ""


def test_pill_on_sets_the_flag_and_the_control_channel(tmp_path):
    done, env, _ = _run_pill(tmp_path, "on")
    assert done.returncode == 0
    assert "VOICEMODE_PILL=true\n" in env
    assert "VOICEMODE_CONTROL_CHANNEL_ENABLED=true\n" in env


def test_pill_on_replaces_old_values_without_duplicates(tmp_path):
    _, env, _ = _run_pill(tmp_path, "on", "KEEP=1\nVOICEMODE_PILL=false\nVOICEMODE_CONTROL_CHANNEL_ENABLED=false\n")
    assert env.count("VOICEMODE_PILL=") == 1 and "VOICEMODE_PILL=true\n" in env
    assert env.count("VOICEMODE_CONTROL_CHANNEL_ENABLED=") == 1
    assert "VOICEMODE_CONTROL_CHANNEL_ENABLED=true\n" in env
    assert "KEEP=1\n" in env


def test_pill_on_opens_the_window_only_under_wsl(tmp_path):
    _, _, calls = _run_pill(tmp_path / "wsl", "on")
    assert "powershell.exe" in calls and "pill-windows.ps1" in calls
    _, _, calls = _run_pill(tmp_path / "other", "on", wsl=False)
    assert "powershell.exe" not in calls


def test_pill_off_clears_the_flag_stops_the_backend_and_keeps_the_control_channel(tmp_path):
    # Push-to-talk needs the control channel too, so `off` must leave it alone.
    done, env, calls = _run_pill(tmp_path, "off", "VOICEMODE_PILL=true\nVOICEMODE_CONTROL_CHANNEL_ENABLED=true\n")
    assert done.returncode == 0
    assert "VOICEMODE_PILL=false\n" in env and env.count("VOICEMODE_PILL=") == 1
    assert "VOICEMODE_CONTROL_CHANNEL_ENABLED=true\n" in env
    assert "pkill" in calls and "voice_pill" in calls
    assert "Start-Process" not in calls


def test_pill_off_signals_the_window_so_it_stops_without_a_backend(tmp_path):
    # A window waiting to retry, or with a refused backend, has nothing for pkill to find.
    _, _, calls = _run_pill(tmp_path / "wsl", "off", "VOICEMODE_PILL=true\n")
    assert "powershell.exe" in calls and "Local\\VoiceModePillStop" in calls and ".Set()" in calls
    _, _, calls = _run_pill(tmp_path / "other", "off", "VOICEMODE_PILL=true\n", wsl=False)
    assert "powershell.exe" not in calls


def test_pill_on_says_so_when_the_window_does_not_stay_up(tmp_path):
    done, env, _ = _run_pill(tmp_path, "on", window="1")
    assert done.returncode != 0
    assert "did not stay up" in done.stdout and "Voice pill on" not in done.stdout
    assert "%LOCALAPPDATA%\\voicemode-local\\pill-window.log" in done.stdout  # fail() uses echo -e
    assert "VOICEMODE_PILL=true\n" in env


def test_pill_on_reports_a_window_already_running(tmp_path):
    # The second instance sees the first one's mutex and exits 0.
    done, _, _ = _run_pill(tmp_path, "on", window="0")
    assert done.returncode == 0 and "already running" in done.stdout


def test_pill_on_says_when_it_cannot_tell(tmp_path):
    done, _, _ = _run_pill(tmp_path, "on", window="")
    assert done.returncode == 0 and "Could not confirm" in done.stdout


def test_pill_status_reports_and_changes_nothing(tmp_path):
    text = "VOICEMODE_PILL=true\nVOICEMODE_CONTROL_CHANNEL_ENABLED=false\n"
    done, env, _ = _run_pill(tmp_path / "a", "status", text)
    assert done.returncode == 0 and "Voice pill: enabled" in done.stdout
    assert "Control channel: disabled" in done.stdout
    assert env == text
    done, _, _ = _run_pill(tmp_path / "b", "status")
    assert "Voice pill: disabled" in done.stdout


def test_pill_rejects_an_unknown_action(tmp_path):
    done, env, _ = _run_pill(tmp_path, "sideways", "KEEP=1\n")
    assert done.returncode != 0 and "usage" in done.stdout
    assert env == "KEEP=1\n"


def test_pill_backend_pattern_matches_only_the_backend():
    pattern = re.search(r"^PILL_BACKEND_RE='(.*)'$", SWITCH.read_text(), re.M).group(1)
    for line in ("/repo/.venv/bin/python /repo/patches/voice_pill.py --stdio",
                 "/usr/bin/python3 /repo/patches/voice_pill.py",
                 "python3 -m voice_mode.voice_pill --stdio"):
        assert re.search(pattern, line), line
    for line in ("vim /repo/patches/voice_pill.py",
                 "/bin/bash -c python3 /repo/patches/voice_pill.py",
                 "/repo/.venv/bin/python -m pytest /repo/tests/test_voice_pill.py"):
        assert not re.search(pattern, line), line


# --- barge on/off/status and the control channel -------------------------------

def _run_barge(tmp_path: Path, action: str, env_text: str = ""):
    """Run `barge <action>` against a fake HOME with a stub Silero model and stubbed services."""
    import hashlib
    root, home, bin_dir, scratch = (tmp_path / n for n in ("app", "home", "bin", "scratch"))
    for d in (root, home / ".voicemode-local", home / ".voicemode", bin_dir, scratch):
        d.mkdir(parents=True, exist_ok=True)
    model = root / "models/silero/silero_vad.onnx"
    model.parent.mkdir(parents=True, exist_ok=True)
    model.write_bytes(b"stub")
    sha = hashlib.sha256(b"stub").hexdigest()
    text = SWITCH.read_text().replace("/tmp/", f"{scratch}/")
    text = re.sub(r'SILERO_SHA256="[0-9a-f]+"', f'SILERO_SHA256="{sha}"', text)
    script = root / "voicemode-switch"
    script.write_text(text)
    script.chmod(0o755)
    _stub(root / ".venv/bin/python", "exit 0")
    _stub(bin_dir / "curl", "exit 1")
    _stub(bin_dir / "docker", "exit 0")
    (home / ".voicemode-local/config").write_text("KOKORO_ONNX=false\n")
    env_file = home / ".voicemode/voicemode.env"
    env_file.write_text("VOICEMODE_TTS_BASE_URLS=http://127.0.0.1:8880/v1\n" + env_text)
    env = {**os.environ, "HOME": str(home), "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    done = subprocess.run([str(script), "barge", action], env=env, capture_output=True, text=True, timeout=30)
    return done, env_file.read_text()


def test_barge_on_also_turns_the_control_channel_on(tmp_path):
    # Without the channel the trigger's barge_in never reaches the server: barge is inert.
    done, env = _run_barge(tmp_path, "on")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "VOICEMODE_VOICE_BARGE=true\n" in env
    assert "VOICEMODE_CONTROL_CHANNEL_ENABLED=true\n" in env


def test_barge_on_replaces_a_false_channel_without_duplicates(tmp_path):
    _, env = _run_barge(tmp_path, "on", "VOICEMODE_CONTROL_CHANNEL_ENABLED=false\n")
    assert env.count("VOICEMODE_CONTROL_CHANNEL_ENABLED=") == 1
    assert "VOICEMODE_CONTROL_CHANNEL_ENABLED=true\n" in env


def test_barge_off_leaves_the_control_channel_on(tmp_path):
    # The pill and push-to-talk use the channel too.
    done, env = _run_barge(tmp_path, "off", "VOICEMODE_VOICE_BARGE=true\nVOICEMODE_CONTROL_CHANNEL_ENABLED=true\n")
    assert done.returncode == 0
    assert "VOICEMODE_VOICE_BARGE=false\n" in env
    assert "VOICEMODE_CONTROL_CHANNEL_ENABLED=true\n" in env


def test_barge_status_prints_the_channel_state(tmp_path):
    done, _ = _run_barge(tmp_path, "status", "VOICEMODE_VOICE_BARGE=true\nVOICEMODE_CONTROL_CHANNEL_ENABLED=true\n")
    assert "Control channel: enabled" in done.stdout
    done, _ = _run_barge(tmp_path / "b", "status", "VOICEMODE_VOICE_BARGE=true\n")
    assert "Control channel: disabled" in done.stdout


def test_pill_ensure_opens_the_window_when_enabled_without_touching_settings(tmp_path):
    """Per-session start (voicemode-mcp on WSL): opens the window if the pill is on, writes
    nothing (sessions start often; settings stay the user's), and the window's own
    single-instance lock makes a second call harmless."""
    original = "KEEP=1\n# comment\nVOICEMODE_PILL=true\n"
    done, env, calls = _run_pill(tmp_path, "ensure", original)
    assert done.returncode == 0
    assert "pill-windows.ps1" in calls
    assert env == original


def test_pill_ensure_does_nothing_when_disabled(tmp_path):
    for i, text in enumerate(("VOICEMODE_PILL=false\n", "")):
        done, env, calls = _run_pill(tmp_path / str(i), "ensure", text)
        assert done.returncode == 0
        assert "powershell.exe" not in calls and env == text
