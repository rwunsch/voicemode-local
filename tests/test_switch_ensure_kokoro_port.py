"""`voicemode-switch ensure` must respect KOKORO_ONNX_PORT.

Regression: with KOKORO_ONNX_PORT=8882 and Docker Kokoro not up yet (a session
starting during boot), `ensure` started the CPU ONNX server on a hardcoded 8880.
It kept that port after Docker came up, so all TTS silently ran on CPU, and the
shared PID file stopped the real 8882 last resort from ever starting.

Runs a copy of the script with /tmp redirected and curl, docker and the venv
python stubbed, so it cannot touch the live services or their PID files.
"""
import os
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

    # start_detached backgrounds the server, so give the stub a moment to log.
    deadline = time.time() + 3
    while time.time() < deadline and not py_log.exists():
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
