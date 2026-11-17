"""voicemode-switch and native GPU Kokoro (~/.voicemode-local/config).

  KOKORO_ONNX=true + KOKORO_ONNX_GPU=true   kokoro-onnx-server.py runs from its own venv
                                            (KOKORO_ONNX_VENV, default
                                            ~/.voicemode-local/kokoro-gpu-venv) through
                                            scripts/kokoro-gpu-start.sh with --require-gpu,
                                            on KOKORO_ONNX_PORT (default 8880); on 8880 it
                                            replaces the Docker Kokoro, so `start` does not
                                            start that container
  KOKORO_ONNX_GPU unset / false             today's behaviour, unchanged (the CPU server
                                            from the repo venv)

Runs a copy of the script (with lib/compute.sh) in a throwaway app dir, /tmp
redirected to a scratch dir, curl/docker/sleep stubbed and the start wrapper and the
repo venv's python replaced by stubs that log their arguments, so nothing real starts
and no live PID file is touched.
"""
import os
import stat
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SWITCH = REPO / "voicemode-switch"


def _stub(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/bash\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class Box:
    def __init__(self, tmp_path: Path, config: str,
                 urls: str = "VOICEMODE_TTS_BASE_URLS=http://127.0.0.1:8880/v1\n"):
        self.root, self.home, self.bin, self.scratch = (tmp_path / n for n in ("app", "home", "bin", "scratch"))
        for d in (self.root / "lib", self.home / ".voicemode-local", self.home / ".voicemode", self.bin, self.scratch):
            d.mkdir(parents=True, exist_ok=True)
        self.script = self.root / "voicemode-switch"
        self.script.write_text(SWITCH.read_text().replace("/tmp/", f"{self.scratch}/"))
        self.script.chmod(0o755)
        (self.root / "lib/compute.sh").write_text((REPO / "lib/compute.sh").read_text())
        self.gpu_log = self.scratch / "gpu-wrapper.log"
        self.py_log = self.scratch / "python.log"
        self.docker_log = self.scratch / "docker.log"
        _stub(self.root / "scripts/kokoro-gpu-start.sh", f'echo "$@" >> "{self.gpu_log}"; exit 0')
        _stub(self.root / "scripts/faster-whisper-start.sh", "exit 0")
        _stub(self.root / ".venv/bin/python", f'echo "$@" >> "{self.py_log}"; exit 0')
        _stub(self.bin / "curl", "exit 1")   # every port reads as down
        _stub(self.bin / "docker", f'echo "$@" >> "{self.docker_log}"')
        _stub(self.bin / "sleep", "exit 0")
        (self.home / ".voicemode-local/config").write_text(config)
        (self.home / ".voicemode/voicemode.env").write_text(urls)

    def run(self, *args):
        env = {**os.environ, "HOME": str(self.home), "PATH": f"{self.bin}:{os.environ['PATH']}"}
        return subprocess.run([str(self.script), *args], env=env, capture_output=True, text=True, timeout=30)

    def wait_for(self, path: Path, seconds=6.0) -> str:
        deadline = time.time() + seconds
        while time.time() < deadline:
            if path.exists() and path.read_text():
                time.sleep(0.2)
                return path.read_text()
            time.sleep(0.05)
        return path.read_text() if path.exists() else ""

    def read(self, path: Path) -> str:
        return path.read_text() if path.exists() else ""


GPU = "KOKORO_ONNX=true\nKOKORO_ONNX_GPU=true\n"


# --- ensure ----------------------------------------------------------------------------

def test_gpu_ensure_starts_the_wrapper_with_venv_port_and_require_gpu(tmp_path):
    b = Box(tmp_path, GPU + "KOKORO_ONNX_VENV=/opt/kgpu\n")
    done = b.run("ensure")
    assert done.returncode == 0, done.stderr
    args = b.wait_for(b.gpu_log).strip()
    assert args == f"/opt/kgpu --port 8880 --model-dir {b.root}/models/kokoro --require-gpu"
    assert (b.scratch / "kokoro-onnx-server.pid").exists()
    assert not b.docker_log.exists()               # no Docker Kokoro needed
    assert "GPU" in done.stdout and "8880" in done.stdout
    time.sleep(0.3)
    assert "kokoro-onnx-server.py" not in b.read(b.py_log)   # not the CPU server


def test_gpu_ensure_defaults_to_the_installer_venv(tmp_path):
    b = Box(tmp_path, GPU)
    b.run("ensure", "--quiet")
    args = b.wait_for(b.gpu_log).strip()
    assert args.startswith(f"{b.home}/.voicemode-local/kokoro-gpu-venv --port 8880 ")


def test_gpu_ensure_uses_kokoro_onnx_port(tmp_path):
    b = Box(tmp_path, GPU + "KOKORO_ONNX_PORT=8882\n",
            urls="VOICEMODE_TTS_BASE_URLS=http://127.0.0.1:8882/v1\n")
    b.run("ensure", "--quiet")
    assert "--port 8882" in b.wait_for(b.gpu_log)


def test_cpu_onnx_ensure_is_unchanged(tmp_path):
    b = Box(tmp_path, "KOKORO_ONNX=true\n")
    done = b.run("ensure")
    assert "ensured Kokoro (8880)" in done.stdout
    args = b.wait_for(b.py_log)
    assert f"{b.root}/kokoro-onnx-server.py --port 8880 --model-dir {b.root}/models/kokoro" in args
    assert "--require-gpu" not in args
    assert not b.gpu_log.exists()


def test_gpu_flag_without_kokoro_onnx_changes_nothing(tmp_path):
    b = Box(tmp_path, "KOKORO_ONNX=false\nKOKORO_ONNX_GPU=true\n")
    b.run("ensure", "--quiet")
    time.sleep(0.3)
    assert not b.gpu_log.exists()
    assert "up -d" in b.read(b.docker_log)         # the Docker Kokoro, as before


# --- start -----------------------------------------------------------------------------

def test_gpu_start_does_not_start_the_docker_kokoro_and_says_so(tmp_path):
    b = Box(tmp_path, GPU + "STT_ENGINE=faster-whisper\n")
    done = b.run("start")
    assert done.returncode == 0, done.stderr
    assert "--require-gpu" in b.wait_for(b.gpu_log)
    assert "not starting the Docker kokoro" in done.stdout
    assert "kokoro" not in b.read(b.docker_log).replace("docker-compose", "")


def test_gpu_start_with_docker_stt_starts_only_the_docker_whisper(tmp_path):
    b = Box(tmp_path, GPU)
    done = b.run("start")
    assert done.returncode == 0, done.stderr
    first = b.read(b.docker_log).splitlines()[0]
    assert first == f"compose -f {b.root}/docker-compose.yml up -d whisper"
    assert "not starting the Docker kokoro" in done.stdout


def test_gpu_start_keeps_piper_when_enabled(tmp_path):
    b = Box(tmp_path, GPU + "PIPER_ENABLED=true\n")
    b.run("start")
    first = b.read(b.docker_log).splitlines()[0]
    assert first == f"compose -f {b.root}/docker-compose.yml --profile piper up -d whisper piper"


def test_gpu_start_with_native_stt_and_no_piper_needs_no_docker(tmp_path):
    b = Box(tmp_path, GPU + "STT_ENGINE=faster-whisper\n")
    done = b.run("start")
    assert "up -d" not in b.read(b.docker_log)
    assert "No Docker containers needed" in done.stdout


def test_cpu_onnx_start_is_unchanged(tmp_path):
    b = Box(tmp_path, "KOKORO_ONNX=true\n")
    _stub(b.bin / "lsof", "exit 1")
    done = b.run("start")
    assert done.returncode == 0, done.stderr
    assert "Starting Kokoro ONNX server (native, port 8880)..." in done.stdout
    assert "--port 8880" in b.wait_for(b.py_log)
    assert not b.gpu_log.exists()
    assert "up -d" not in b.read(b.docker_log)


# --- stop / status ---------------------------------------------------------------------

def test_stop_stops_the_gpu_server(tmp_path):
    b = Box(tmp_path, GPU)
    sleeper = subprocess.Popen(["sleep", "60"])
    try:
        (b.scratch / "kokoro-onnx-server.pid").write_text(str(sleeper.pid))
        done = b.run("stop")
        assert "Kokoro ONNX server stopped" in done.stdout
        assert sleeper.wait(timeout=5) is not None
        assert not (b.scratch / "kokoro-onnx-server.pid").exists()
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait()


def test_status_reports_the_gpu_server_and_its_provider(tmp_path):
    b = Box(tmp_path, GPU)
    _stub(b.bin / "curl", 'for a in "$@"; do case "$a" in *:8880/health) '
                          'echo \'{"status": "ok", "engine": "kokoro-onnx", "provider": "CUDAExecutionProvider"}\'; '
                          'exit 0;; esac; done; exit 1')
    sleeper = subprocess.Popen(["sleep", "60"])
    try:
        (b.scratch / "kokoro-onnx-server.pid").write_text(str(sleeper.pid))
        done = b.run("status")
        assert "Kokoro ONNX (native GPU, port 8880)" in done.stdout
        assert f"Kokoro ONNX server running (PID {sleeper.pid})" in done.stdout
        assert "Provider: CUDAExecutionProvider" in done.stdout
    finally:
        sleeper.kill()
        sleeper.wait()


def test_status_says_when_the_gpu_server_runs_on_the_cpu(tmp_path):
    b = Box(tmp_path, GPU)
    _stub(b.bin / "curl", 'for a in "$@"; do case "$a" in *:8880/health) '
                          'echo \'{"status": "ok", "provider": "CPUExecutionProvider"}\'; '
                          'exit 0;; esac; done; exit 1')
    done = b.run("status")
    assert "Provider: CPUExecutionProvider" in done.stdout
    assert "not on the GPU" in done.stdout


def test_cpu_onnx_status_is_unchanged(tmp_path):
    b = Box(tmp_path, "KOKORO_ONNX=true\n")
    done = b.run("status")
    assert "Kokoro ONNX (native, port 8880):" in done.stdout
    assert "Provider:" not in done.stdout
