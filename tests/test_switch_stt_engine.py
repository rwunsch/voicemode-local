"""voicemode-switch and STT_ENGINE (~/.voicemode-local/config).

  unset / docker / whisper-cpp  today's behaviour, unchanged: whisper-proxy on 2022
                                (or whisper.cpp when WHISPER_CPP_MODEL is set)
  faster-whisper                `ensure` starts faster-whisper-server.py on 2022
                                through scripts/faster-whisper-start.sh, venv from
                                FASTER_WHISPER_VENV, model from FASTER_WHISPER_MODEL
                                (default small); no proxy, no Docker whisper

Runs a copy of the script (with lib/compute.sh) in a throwaway app dir, /tmp
redirected to a scratch dir, curl/docker stubbed and the start wrapper replaced by a
stub that logs its arguments, so nothing real starts and no live PID file is touched.
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
    def __init__(self, tmp_path: Path, config: str, urls: str = "http://127.0.0.1:2022/v1"):
        self.root, self.home, self.bin, self.scratch = (tmp_path / n for n in ("app", "home", "bin", "scratch"))
        for d in (self.root / "lib", self.home / ".voicemode-local", self.home / ".voicemode", self.bin, self.scratch):
            d.mkdir(parents=True, exist_ok=True)
        self.script = self.root / "voicemode-switch"
        self.script.write_text(SWITCH.read_text().replace("/tmp/", f"{self.scratch}/"))
        self.script.chmod(0o755)
        (self.root / "lib/compute.sh").write_text((REPO / "lib/compute.sh").read_text())
        self.wrapper_log = self.scratch / "wrapper.log"
        self.docker_log = self.scratch / "docker.log"
        _stub(self.root / "scripts/faster-whisper-start.sh", f'echo "$@" >> "{self.wrapper_log}"; exit 0')
        _stub(self.root / ".venv/bin/python", "exit 0")
        _stub(self.bin / "curl", "exit 1")   # every port reads as down
        _stub(self.bin / "docker", f'echo "$@" >> "{self.docker_log}"')
        (self.home / ".voicemode-local/config").write_text(config)
        (self.home / ".voicemode/voicemode.env").write_text(f"VOICEMODE_STT_BASE_URLS={urls}\n")

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


def test_faster_whisper_ensure_starts_the_wrapper_with_venv_and_model(tmp_path):
    b = Box(tmp_path, "STT_ENGINE=faster-whisper\nFASTER_WHISPER_VENV=/opt/fw-venv\nFASTER_WHISPER_MODEL=base\n")
    done = b.run("ensure")
    assert done.returncode == 0, done.stderr
    args = b.wait_for(b.wrapper_log)
    assert args.strip() == "/opt/fw-venv --host 0.0.0.0 --port 2022 --model base"
    assert (b.scratch / "faster-whisper-server.pid").exists()
    assert not (b.scratch / "whisper-proxy.pid").exists()
    assert not b.docker_log.exists()      # no Docker whisper needed
    assert "faster-whisper" in done.stdout


def test_faster_whisper_defaults_to_small_and_the_installer_venv(tmp_path):
    b = Box(tmp_path, "STT_ENGINE=faster-whisper\n")
    b.run("ensure", "--quiet")
    args = b.wait_for(b.wrapper_log).strip()
    assert args == f"{b.home}/.voicemode-local/faster-whisper-venv --host 0.0.0.0 --port 2022 --model small"


def test_faster_whisper_host_can_be_set(tmp_path):
    b = Box(tmp_path, "STT_ENGINE=faster-whisper\nFASTER_WHISPER_HOST=127.0.0.1\n")
    b.run("ensure", "--quiet")
    assert "--host 127.0.0.1 --port 2022" in b.wait_for(b.wrapper_log)


def test_the_default_host_is_the_one_whisper_proxy_binds_today():
    """Replacing the proxy must keep every reachability it had (Windows reaching WSL's
    2022): the proxy is started without --host, so it binds its own default."""
    import re
    proxy = (REPO / "whisper-proxy.py").read_text()
    proxy_default = re.search(r'add_argument\("--host", default="([^"]+)"', proxy).group(1)
    switch = SWITCH.read_text()
    ensure_proxy = switch[switch.index("start_detached whisper-proxy"):]
    ensure_proxy = ensure_proxy[:ensure_proxy.index(";;")]
    assert "--host" not in ensure_proxy
    m = re.search(r'vm_read_config FASTER_WHISPER_HOST ([^ );"]+)', switch)
    assert m and m.group(1) == proxy_default == "0.0.0.0"


def test_unset_engine_keeps_starting_the_whisper_proxy(tmp_path):
    b = Box(tmp_path, "KOKORO_ONNX=false\n")
    done = b.run("ensure")
    assert done.returncode == 0, done.stderr
    assert (b.scratch / "whisper-proxy.pid").exists()
    assert "ensured Whisper proxy (2022)" in done.stdout
    time.sleep(0.3)
    assert not b.wrapper_log.exists()


def test_docker_engine_keeps_starting_the_whisper_proxy(tmp_path):
    b = Box(tmp_path, "STT_ENGINE=docker\n")
    done = b.run("ensure")
    assert "ensured Whisper proxy (2022)" in done.stdout
    time.sleep(0.3)
    assert not b.wrapper_log.exists()


def test_stop_stops_the_faster_whisper_server(tmp_path):
    b = Box(tmp_path, "STT_ENGINE=faster-whisper\n")
    sleeper = subprocess.Popen(["sleep", "60"])
    try:
        (b.scratch / "faster-whisper-server.pid").write_text(str(sleeper.pid))
        done = b.run("stop")
        assert "faster-whisper server stopped" in done.stdout
        assert sleeper.wait(timeout=5) is not None
        assert not (b.scratch / "faster-whisper-server.pid").exists()
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait()


def test_status_reports_the_faster_whisper_server(tmp_path):
    b = Box(tmp_path, "STT_ENGINE=faster-whisper\n")
    sleeper = subprocess.Popen(["sleep", "60"])
    try:
        (b.scratch / "faster-whisper-server.pid").write_text(str(sleeper.pid))
        done = b.run("status")
        assert f"faster-whisper server running (PID {sleeper.pid})" in done.stdout
        assert "http://127.0.0.1:2022/health" in done.stdout
        assert "Whisper proxy is not running" not in done.stdout   # no proxy in this setup
    finally:
        sleeper.kill()
        sleeper.wait()


def test_status_without_the_engine_is_unchanged(tmp_path):
    b = Box(tmp_path, "KOKORO_ONNX=false\n")
    done = b.run("status")
    assert "faster-whisper" not in done.stdout
    assert "Whisper proxy is not running" in done.stdout



# --- `start` brings up only the containers that are used ------------------------------

def _start(tmp_path, config):
    b = Box(tmp_path, config)
    _stub(b.bin / "sleep", "exit 0")   # the docker path waits up to 30 s for whisper on 9000
    _stub(b.bin / "whisper-server", f'echo "whisper-server $*" >> "{b.scratch}/wcpp.log"')
    done = b.run("start")
    docker = b.docker_log.read_text() if b.docker_log.exists() else ""
    return b, done, docker


def test_start_with_docker_stt_is_unchanged(tmp_path):
    b, done, docker = _start(tmp_path, "KOKORO_ONNX=false\n")
    assert done.returncode == 0, done.stderr
    assert docker.splitlines()[0] == f"compose -f {b.root}/docker-compose.yml up -d"
    assert "Starting Whisper proxy..." in done.stdout
    assert "Docker whisper" not in done.stdout


def test_start_with_faster_whisper_starts_only_kokoro(tmp_path):
    b, done, docker = _start(tmp_path, "KOKORO_ONNX=false\nSTT_ENGINE=faster-whisper\n")
    assert done.returncode == 0, done.stderr
    assert docker.splitlines()[0] == f"compose -f {b.root}/docker-compose.yml up -d kokoro"
    assert "not starting the Docker whisper" in done.stdout
    assert "Starting Whisper proxy..." not in done.stdout
    assert "--port 2022" in b.wait_for(b.wrapper_log)


def test_start_with_native_stt_keeps_piper_when_enabled(tmp_path):
    b, _, docker = _start(tmp_path, "KOKORO_ONNX=false\nPIPER_ENABLED=true\nSTT_ENGINE=faster-whisper\n")
    assert docker.splitlines()[0] == f"compose -f {b.root}/docker-compose.yml --profile piper up -d kokoro piper"


def test_start_with_whisper_cpp_starts_whisper_cpp_and_only_kokoro(tmp_path):
    b, done, docker = _start(tmp_path, "KOKORO_ONNX=false\nSTT_ENGINE=whisper-cpp\nWHISPER_CPP_MODEL=/m/ggml.bin\n")
    assert done.returncode == 0, done.stderr
    assert docker.splitlines()[0] == f"compose -f {b.root}/docker-compose.yml up -d kokoro"
    assert "Starting Whisper proxy..." not in done.stdout
    assert "-m /m/ggml.bin" in b.wait_for(b.scratch / "wcpp.log")
