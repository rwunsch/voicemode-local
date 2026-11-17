"""`voicemode-switch ensure`: a port that answers is not enough for a native engine.

2026-11-17: STT_ENGINE=faster-whisper was configured, but the old whisper-proxy (started by the
~/.bashrc autostart) held port 2022 and answered /health, so `ensure` never started the engine.

  STT_ENGINE=faster-whisper  2022 is healthy only if GET /health says "engine": "faster-whisper"
  KOKORO_ONNX_GPU=true       8880 is healthy only if /health says provider CUDAExecutionProvider
  a wrong holder             stopped ONLY when its pid file / command line proves it is
                             voicemode's own (by exact pid); otherwise nothing is killed, one
                             line says who holds the port, and ensure exits non-zero
  settings unset             behaviour unchanged (see test_switch_stt_engine.py)

Runs a copy of the script with /tmp redirected to a scratch dir. curl is a stub that answers
for a port only while the holder process it names is alive, so nothing real is touched; the
holders are throwaway processes started here and stopped here by exact pid.
"""
import os
import signal
import stat
import subprocess
import textwrap
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SWITCH = REPO / "voicemode-switch"


def _stub(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/bash\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class Box:
    def __init__(self, tmp_path: Path, config: str, urls: str):
        self.root, self.home, self.bin, self.scratch = (tmp_path / n for n in ("app", "home", "bin", "scratch"))
        self.holders = []
        for d in (self.root / "lib", self.home / ".voicemode-local", self.home / ".voicemode", self.bin, self.scratch):
            d.mkdir(parents=True, exist_ok=True)
        self.script = self.root / "voicemode-switch"
        self.script.write_text(SWITCH.read_text().replace("/tmp/", f"{self.scratch}/"))
        self.script.chmod(0o755)
        (self.root / "lib/compute.sh").write_text((REPO / "lib/compute.sh").read_text())
        self.fw_log = self.scratch / "fw.log"
        self.gpu_log = self.scratch / "gpu.log"
        _stub(self.root / "scripts/faster-whisper-start.sh", f'echo "$@" >> "{self.fw_log}"; exit 0')
        _stub(self.root / "scripts/kokoro-gpu-start.sh", f'echo "$@" >> "{self.gpu_log}"; exit 0')
        _stub(self.root / ".venv/bin/python", "exit 0")
        _stub(self.bin / "docker", "exit 0")
        # A port answers only while the pid in holder_<port> is alive; the body is body_<port>.
        _stub(self.bin / "curl", textwrap.dedent(f'''
            url="${{@: -1}}"; port="${{url#http://127.0.0.1:}}"; port="${{port%%/*}}"
            pidf="{self.scratch}/holder_$port"
            [ -f "$pidf" ] && kill -0 "$(cat "$pidf")" 2>/dev/null || exit 7
            case "$url" in
                */health) cat "{self.scratch}/body_$port" 2>/dev/null; exit 0 ;;
                *) exit 22 ;;
            esac
        '''))
        (self.home / ".voicemode-local/config").write_text(config)
        (self.home / ".voicemode/voicemode.env").write_text(urls)

    def holder(self, port: int, body: str, cmd: list, pidfile: str = None) -> int:
        """Start a detached holder process for `port` (orphaned, so a kill leaves no zombie)."""
        out = subprocess.run(["bash", "-c", '"$@" >/dev/null 2>&1 </dev/null & echo $!', "x", *cmd],
                             capture_output=True, text=True, timeout=10)
        pid = int(out.stdout.strip())
        self.holders.append(pid)
        (self.scratch / f"holder_{port}").write_text(str(pid))
        (self.scratch / f"body_{port}").write_text(body)
        if pidfile:
            (self.scratch / pidfile).write_text(str(pid))
        time.sleep(0.3)   # let exec finish so `ps` shows the final command line
        return pid

    def fake_script_cmd(self, name: str, *args) -> list:
        script = self.root / name
        script.write_text("import time\ntime.sleep(60)\n")
        return ["python3", str(script), *args]

    def alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

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

    def cleanup(self):
        for pid in self.holders:
            if self.alive(pid):
                os.kill(pid, signal.SIGKILL)


import pytest


@pytest.fixture
def made(tmp_path):
    boxes = []

    def make(config, urls):
        b = Box(tmp_path, config, urls)
        boxes.append(b)
        return b

    yield make
    for b in boxes:
        b.cleanup()


STT_URLS = "VOICEMODE_STT_BASE_URLS=http://127.0.0.1:2022/v1\n"
TTS_URLS = "VOICEMODE_TTS_BASE_URLS=http://127.0.0.1:8880/v1\n"
FW = "STT_ENGINE=faster-whisper\n"
GPU = "KOKORO_ONNX=true\nKOKORO_ONNX_GPU=true\n"


# --- 2022 / faster-whisper --------------------------------------------------------

def test_the_right_engine_on_2022_is_left_alone(made):
    b = made(FW, STT_URLS)
    pid = b.holder(2022, '{"status":"ok","engine":"faster-whisper","model":"small","device":"cuda"}', ["sleep", "60"])
    done = b.run("ensure")
    assert done.returncode == 0, done.stderr
    assert done.stderr == ""
    assert b.alive(pid)
    time.sleep(0.3)
    assert not b.fw_log.exists()


def test_a_whisper_proxy_on_2022_is_stopped_by_pid_file_and_the_engine_started(made):
    b = made(FW, STT_URLS)
    cmd = b.fake_script_cmd("whisper-proxy.py", "--port", "2022", "--whisper-url", "http://127.0.0.1:9000")
    pid = b.holder(2022, '{"status":"ok"}', cmd, pidfile="whisper-proxy.pid")
    bystander = b.holder(9999, "", ["sleep", "60"])
    done = b.run("ensure")
    assert done.returncode == 0, done.stderr
    assert f"2022 is held by whisper-proxy (PID {pid}), not the configured faster-whisper engine" in done.stderr
    assert not b.alive(pid)
    assert b.alive(bystander)   # exact pid only
    assert "--port 2022" in b.wait_for(b.fw_log)


def test_a_whisper_proxy_with_no_pid_file_is_proven_by_its_command_line(made):
    b = made(FW, STT_URLS)
    cmd = b.fake_script_cmd("whisper-proxy.py", "--port", "2022")
    pid = b.holder(2022, '{"status":"ok"}', cmd)   # no pid file
    done = b.run("ensure", "--quiet")
    assert done.returncode == 0, done.stderr
    assert not b.alive(pid)
    assert "--port 2022" in b.wait_for(b.fw_log)


def test_an_unprovable_holder_on_2022_is_not_killed_and_ensure_fails(made):
    b = made(FW, STT_URLS)
    pid = b.holder(2022, '{"status":"ok"}', ["sleep", "60"])
    (b.scratch / "whisper-proxy.pid").write_text(str(pid))   # a pid file alone is not proof
    done = b.run("ensure")
    assert done.returncode != 0
    assert "2022 is held by an unidentified service, not the configured faster-whisper engine" in done.stderr
    assert b.alive(pid)
    time.sleep(0.3)
    assert not b.fw_log.exists()


def test_a_whisper_proxy_for_another_port_is_not_proof(made):
    b = made(FW, STT_URLS)
    cmd = b.fake_script_cmd("whisper-proxy.py", "--port", "2023")
    pid = b.holder(2022, '{"status":"ok"}', cmd, pidfile="whisper-proxy.pid")
    done = b.run("ensure", "--quiet")
    assert done.returncode != 0
    assert b.alive(pid)


def test_without_the_engine_a_whisper_proxy_on_2022_is_untouched(made):
    b = made("STT_ENGINE=docker\n", STT_URLS)
    cmd = b.fake_script_cmd("whisper-proxy.py", "--port", "2022")
    pid = b.holder(2022, '{"status":"ok"}', cmd, pidfile="whisper-proxy.pid")
    done = b.run("ensure")
    assert done.returncode == 0
    assert done.stderr == ""
    assert b.alive(pid)


# --- 8880 / native GPU Kokoro ------------------------------------------------------

def test_cuda_kokoro_on_8880_is_left_alone(made):
    b = made(GPU, TTS_URLS)
    pid = b.holder(8880, '{"status":"ok","provider":"CUDAExecutionProvider"}', ["sleep", "60"])
    done = b.run("ensure")
    assert done.returncode == 0, done.stderr
    assert done.stderr == ""
    assert b.alive(pid)
    time.sleep(0.3)
    assert not b.gpu_log.exists()


def test_our_cpu_kokoro_on_8880_is_replaced_by_the_gpu_one(made):
    b = made(GPU, TTS_URLS)
    cmd = b.fake_script_cmd("kokoro-onnx-server.py", "--port", "8880", "--model-dir", "m")
    pid = b.holder(8880, '{"status":"ok","provider":"CPUExecutionProvider"}', cmd, pidfile="kokoro-onnx-server.pid")
    done = b.run("ensure")
    assert done.returncode == 0, done.stderr
    assert f"8880 is held by kokoro-onnx-server on CPUExecutionProvider (PID {pid})" in done.stderr
    assert not b.alive(pid)
    assert "--port 8880" in b.wait_for(b.gpu_log)


def test_our_gpu_kokoro_still_loading_reports_no_provider_and_is_left_alone(made):
    b = made(GPU, TTS_URLS)
    cmd = b.fake_script_cmd("kokoro-onnx-server.py", "--port", "8880")
    pid = b.holder(8880, '{"status":"ok","provider":null}', cmd, pidfile="kokoro-onnx-server.pid")
    done = b.run("ensure")
    assert done.returncode == 0, done.stderr
    assert done.stderr == ""
    assert b.alive(pid)


def test_a_docker_kokoro_on_8880_is_not_killed_and_ensure_fails(made):
    b = made(GPU, TTS_URLS)
    pid = b.holder(8880, '{"status":"healthy"}', ["sleep", "60"])
    done = b.run("ensure")
    assert done.returncode != 0
    assert "8880 is held by an unidentified service, not the configured GPU Kokoro" in done.stderr
    assert b.alive(pid)
    time.sleep(0.3)
    assert not b.gpu_log.exists()


def test_cpu_kokoro_configuration_does_not_check_the_provider(made):
    b = made("KOKORO_ONNX=true\n", TTS_URLS)
    pid = b.holder(8880, '{"status":"ok","provider":"CPUExecutionProvider"}', ["sleep", "60"])
    done = b.run("ensure")
    assert done.returncode == 0
    assert done.stderr == ""
    assert b.alive(pid)
