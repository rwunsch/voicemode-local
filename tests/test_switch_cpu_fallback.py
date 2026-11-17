"""voicemode-switch: managed CPU fallbacks for the native GPU speech engines.

  KOKORO_CPU_FALLBACK=true (with KOKORO_ONNX=true + KOKORO_ONNX_GPU=true)
      `ensure` / `start` also run kokoro-onnx-server.py on the CPU on KOKORO_CPU_FALLBACK_PORT
      (default 8882), through scripts/kokoro-cpu-start.sh from the GPU venv, --no-download.
      The port is healthy only if /health says provider CPUExecutionProvider.
  STT_CPU_FALLBACK=true (with STT_ENGINE=faster-whisper)
      `ensure` / `start` also run faster-whisper-server.py --device cpu --compute-type int8 on
      STT_CPU_FALLBACK_PORT (default 2023) with STT_CPU_FALLBACK_MODEL, through
      scripts/faster-whisper-start.sh. Healthy only if /health says engine faster-whisper and
      device cpu.
  a wrong holder of a fallback port
      stopped only when its pid file / command line proves it is this install's own (exact pid);
      otherwise one line says who holds it, nothing is killed and ensure exits non-zero
  both unset / false
      nothing new starts (the other switch tests cover that behaviour)

Runs a copy of the script with /tmp redirected to a per-test scratch dir. curl is a stub that
answers for a port only while the holder process it names is alive. Every holder is a fake
python script inside the per-test tmp dir, started and stopped here by exact pid, so no
pattern in the switch can match a real process.
"""
import os
import signal
import stat
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SWITCH = REPO / "voicemode-switch"


def _stub(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/bash\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class Box:
    def __init__(self, tmp_path: Path, config: str, urls: str):
        self.tmp = tmp_path
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
        self.cpu_log = self.scratch / "cpu.log"
        self.docker_log = self.scratch / "docker.log"
        _stub(self.root / "scripts/faster-whisper-start.sh", f'echo "$@" >> "{self.fw_log}"; exit 0')
        _stub(self.root / "scripts/kokoro-gpu-start.sh", f'echo "$@" >> "{self.gpu_log}"; exit 0')
        _stub(self.root / "scripts/kokoro-cpu-start.sh", f'echo "$@" >> "{self.cpu_log}"; exit 0')
        _stub(self.root / ".venv/bin/python", "exit 0")
        _stub(self.bin / "docker", f'echo "$@" >> "{self.docker_log}"; exit 0')
        _stub(self.bin / "sleep", "exit 0")
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

    def fake(self, name: str, *args, where: str = "app") -> list:
        """A python process whose command line names `name` inside the per-test tmp dir."""
        d = self.root if where == "app" else self.tmp / where
        d.mkdir(parents=True, exist_ok=True)
        script = d / name
        script.write_text("import time\ntime.sleep(60)\n")
        return ["python3", str(script), *args]

    def holder(self, port: int, body: str, cmd: list, pidfile: str = None) -> int:
        out = subprocess.run(["bash", "-c", '"$@" >/dev/null 2>&1 </dev/null & echo $!', "x", *cmd],
                             capture_output=True, text=True, timeout=10)
        pid = int(out.stdout.strip())
        self.holders.append(pid)
        if port:
            (self.scratch / f"holder_{port}").write_text(str(pid))
            (self.scratch / f"body_{port}").write_text(body)
        if pidfile:
            (self.scratch / pidfile).write_text(str(pid))
        time.sleep(0.3)   # let exec finish so `ps` shows the final command line
        return pid

    def alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    def run(self, *args):
        env = {**os.environ, "HOME": str(self.home), "PATH": f"{self.bin}:{os.environ['PATH']}"}
        return subprocess.run([str(self.script), *args], env=env, capture_output=True, text=True, timeout=30)

    def read(self, path: Path) -> str:
        return path.read_text() if path.exists() else ""

    def wait_for(self, path: Path, needle: str = "", seconds=6.0) -> str:
        deadline = time.time() + seconds
        while time.time() < deadline:
            if path.exists() and path.read_text() and needle in path.read_text():
                time.sleep(0.2)
                return path.read_text()
            time.sleep(0.05)
        return self.read(path)

    def cleanup(self):
        for pid in self.holders:
            if self.alive(pid):
                os.kill(pid, signal.SIGKILL)


@pytest.fixture
def made(tmp_path):
    boxes = []

    def make(config, urls=""):
        b = Box(tmp_path, config, urls)
        boxes.append(b)
        return b

    yield make
    for b in boxes:
        b.cleanup()


GPU = "KOKORO_ONNX=true\nKOKORO_ONNX_GPU=true\n"
KFB = GPU + "KOKORO_CPU_FALLBACK=true\n"
FW = "STT_ENGINE=faster-whisper\n"
SFB = FW + "STT_CPU_FALLBACK=true\n"
TTS_URLS = "VOICEMODE_TTS_BASE_URLS=http://127.0.0.1:8880/v1\n"
STT_URLS = "VOICEMODE_STT_BASE_URLS=http://127.0.0.1:2022/v1\n"
CPU_OK = '{"status":"ok","engine":"kokoro-onnx","provider":"CPUExecutionProvider"}'
CUDA = '{"status":"ok","engine":"kokoro-onnx","provider":"CUDAExecutionProvider"}'
FW_CPU = '{"status":"ok","engine":"faster-whisper","model":"base","device":"cpu"}'
FW_CUDA = '{"status":"ok","engine":"faster-whisper","model":"small","device":"cuda"}'


# --- ensure: the Kokoro CPU fallback -------------------------------------------------

def test_ensure_starts_the_kokoro_cpu_fallback_from_the_gpu_venv(made):
    b = made(KFB + "KOKORO_ONNX_VENV=/opt/kgpu\n", TTS_URLS)
    done = b.run("ensure")
    assert done.returncode == 0, done.stderr
    args = b.wait_for(b.cpu_log).strip()
    assert args == f"/opt/kgpu --port 8882 --model-dir {b.root}/models/kokoro --no-download"
    assert (b.scratch / "kokoro-cpu-fallback.pid").exists()
    assert "ensured Kokoro CPU fallback (8882)" in done.stdout
    assert "--require-gpu" in b.wait_for(b.gpu_log)        # the GPU one as before


def test_the_kokoro_fallback_port_can_be_set(made):
    b = made(KFB + "KOKORO_CPU_FALLBACK_PORT=18882\n", TTS_URLS)
    b.run("ensure", "--quiet")
    assert "--port 18882 " in b.wait_for(b.cpu_log)


def test_no_kokoro_fallback_unless_enabled(made):
    b = made(GPU, TTS_URLS)
    done = b.run("ensure")
    assert done.returncode == 0, done.stderr
    b.wait_for(b.gpu_log)
    time.sleep(0.3)
    assert not b.cpu_log.exists()
    assert "CPU fallback" not in done.stdout


def test_no_kokoro_fallback_without_the_gpu_kokoro(made):
    b = made("KOKORO_ONNX=true\nKOKORO_CPU_FALLBACK=true\n", TTS_URLS)
    b.run("ensure", "--quiet")
    time.sleep(0.3)
    assert not b.cpu_log.exists()


def test_no_kokoro_fallback_when_no_local_kokoro_is_routed(made):
    b = made(KFB, "VOICEMODE_TTS_BASE_URLS=https://api.openai.com/v1\n"
                  "VOICEMODE_STT_BASE_URLS=https://api.openai.com/v1\n")
    b.run("ensure", "--quiet")
    time.sleep(0.3)
    assert not b.cpu_log.exists()


def test_a_cpu_kokoro_on_the_fallback_port_is_left_alone(made):
    b = made(KFB, TTS_URLS)
    pid = b.holder(8882, CPU_OK, b.fake("anything.py", where="elsewhere"))
    b.holder(8880, CUDA, b.fake("gpu-kokoro.py", where="elsewhere"))
    done = b.run("ensure")
    assert done.returncode == 0, done.stderr
    assert done.stderr == ""
    assert b.alive(pid)
    time.sleep(0.3)
    assert not b.cpu_log.exists()


def test_an_unprovable_holder_of_the_kokoro_fallback_port_is_not_killed(made):
    b = made(KFB, TTS_URLS)
    b.holder(8880, CUDA, b.fake("gpu-kokoro.py", where="elsewhere"))
    pid = b.holder(8882, CUDA, b.fake("kokoro-onnx-server.py", "--port", "8882", where="other-checkout"))
    done = b.run("ensure")
    assert done.returncode != 0
    assert ("8882 is held by an unidentified service, not the configured Kokoro CPU fallback "
            "(CPUExecutionProvider); not stopping it") in done.stderr
    assert b.alive(pid)
    time.sleep(0.3)
    assert not b.cpu_log.exists()


def test_our_own_kokoro_on_cuda_on_the_fallback_port_is_replaced_by_exact_pid(made):
    b = made(KFB, TTS_URLS)
    b.holder(8880, CUDA, b.fake("gpu-kokoro.py", where="elsewhere"))
    bystander = b.holder(0, "", b.fake("kokoro-onnx-server.py", "--port", "8882", where="other-checkout"))
    pid = b.holder(8882, CUDA, b.fake("kokoro-onnx-server.py", "--port", "8882", "--model-dir", "m"),
                   pidfile="kokoro-cpu-fallback.pid")
    done = b.run("ensure")
    assert done.returncode == 0, done.stderr
    assert f"8882 is held by kokoro-onnx-server on CUDAExecutionProvider (PID {pid})" in done.stderr
    assert not b.alive(pid)
    assert b.alive(bystander)
    assert "--port 8882" in b.wait_for(b.cpu_log)


def test_the_fallback_on_the_gpu_port_is_refused(made):
    b = made(KFB + "KOKORO_ONNX_PORT=8882\n", "VOICEMODE_TTS_BASE_URLS=http://127.0.0.1:8882/v1\n")
    done = b.run("ensure")
    assert "KOKORO_CPU_FALLBACK_PORT" in done.stderr
    time.sleep(0.3)
    assert not b.cpu_log.exists()
    assert "--port 8882" in b.wait_for(b.gpu_log)


# --- ensure: the speech recognition CPU fallback -----------------------------------------

def _fw_lines(b):
    return [l for l in b.wait_for(b.fw_log, "--device cpu").splitlines() if l]


def test_ensure_starts_the_stt_cpu_fallback(made):
    b = made(SFB + "FASTER_WHISPER_VENV=/opt/fw\n", STT_URLS)
    done = b.run("ensure")
    assert done.returncode == 0, done.stderr
    lines = _fw_lines(b)
    assert "/opt/fw --host 0.0.0.0 --port 2022 --model small" in lines
    assert "/opt/fw --host 0.0.0.0 --port 2023 --model base --device cpu --compute-type int8" in lines
    assert (b.scratch / "faster-whisper-cpu.pid").exists()
    assert "ensured faster-whisper CPU fallback (2023)" in done.stdout


def test_the_stt_fallback_port_model_and_host_can_be_set(made):
    b = made(SFB + "STT_CPU_FALLBACK_PORT=12023\nSTT_CPU_FALLBACK_MODEL=tiny\nFASTER_WHISPER_HOST=127.0.0.1\n",
             STT_URLS)
    b.run("ensure", "--quiet")
    assert any(l.endswith("--host 127.0.0.1 --port 12023 --model tiny --device cpu --compute-type int8")
               for l in _fw_lines(b))


def test_no_stt_fallback_unless_enabled(made):
    b = made(FW, STT_URLS)
    b.run("ensure", "--quiet")
    log = b.wait_for(b.fw_log)
    time.sleep(0.3)
    assert "--device cpu" not in b.read(b.fw_log) and "--port 2022" in log
    assert not (b.scratch / "faster-whisper-cpu.pid").exists()


def test_no_stt_fallback_with_the_docker_engine(made):
    b = made("STT_ENGINE=docker\nSTT_CPU_FALLBACK=true\n", STT_URLS)
    b.run("ensure", "--quiet")
    time.sleep(0.3)
    assert not b.fw_log.exists()


def test_a_cpu_faster_whisper_on_the_fallback_port_is_left_alone(made):
    b = made(SFB, STT_URLS)
    b.holder(2022, FW_CUDA, b.fake("gpu-stt.py", where="elsewhere"))
    pid = b.holder(2023, FW_CPU, b.fake("anything.py", where="elsewhere"))
    done = b.run("ensure")
    assert done.returncode == 0, done.stderr
    assert done.stderr == ""
    assert b.alive(pid)
    time.sleep(0.3)
    assert not b.fw_log.exists()


def test_our_own_cpu_fallback_still_loading_is_left_alone(made):
    b = made(SFB, STT_URLS)
    b.holder(2022, FW_CUDA, b.fake("gpu-stt.py", where="elsewhere"))
    pid = b.holder(2023, '{"status":"loading","error":null,"model":"base","device":"cpu"}',
                   b.fake("faster-whisper-server.py", "--port", "2023", "--device", "cpu"),
                   pidfile="faster-whisper-cpu.pid")
    done = b.run("ensure")
    assert done.returncode == 0, done.stderr
    assert done.stderr == ""
    assert b.alive(pid)


def test_an_unprovable_gpu_whisper_on_the_fallback_port_is_not_killed(made):
    b = made(SFB, STT_URLS)
    b.holder(2022, FW_CUDA, b.fake("gpu-stt.py", where="elsewhere"))
    pid = b.holder(2023, FW_CUDA, b.fake("faster-whisper-server.py", "--port", "2023", where="other-checkout"))
    done = b.run("ensure")
    assert done.returncode != 0
    assert ("2023 is held by an unidentified service, not the configured faster-whisper CPU fallback; "
            "not stopping it") in done.stderr
    assert b.alive(pid)
    time.sleep(0.3)
    assert not b.fw_log.exists()


def test_our_own_gpu_whisper_on_the_fallback_port_is_replaced_by_exact_pid(made):
    b = made(SFB, STT_URLS)
    b.holder(2022, FW_CUDA, b.fake("gpu-stt.py", where="elsewhere"))
    pid = b.holder(2023, FW_CUDA, b.fake("faster-whisper-server.py", "--port", "2023", "--model", "small"))
    done = b.run("ensure")
    assert done.returncode == 0, done.stderr
    assert f"2023 is held by faster-whisper on cuda (PID {pid})" in done.stderr
    assert not b.alive(pid)
    assert "--port 2023" in b.wait_for(b.fw_log)


# --- start ------------------------------------------------------------------------------

def test_start_starts_both_fallbacks(made):
    b = made(KFB + SFB, TTS_URLS + STT_URLS)
    done = b.run("start")
    assert done.returncode == 0, done.stderr
    assert "--port 8882" in b.wait_for(b.cpu_log)
    assert any("--port 2023" in l and "--device cpu" in l for l in _fw_lines(b))
    assert "Kokoro CPU fallback" in done.stdout and "faster-whisper CPU fallback" in done.stdout


def test_start_without_fallbacks_starts_none(made):
    b = made(GPU + FW, TTS_URLS + STT_URLS)
    done = b.run("start")
    assert done.returncode == 0, done.stderr
    time.sleep(0.3)
    assert not b.cpu_log.exists()
    assert "--device cpu" not in b.read(b.fw_log)


# --- stop / status ------------------------------------------------------------------------

def test_stop_stops_both_fallbacks_by_exact_pid(made):
    b = made(KFB + SFB)
    k = b.holder(0, "", b.fake("kokoro-onnx-server.py", "--port", "8882"), pidfile="kokoro-cpu-fallback.pid")
    s = b.holder(0, "", b.fake("faster-whisper-server.py", "--port", "2023", "--device", "cpu"),
                 pidfile="faster-whisper-cpu.pid")
    bystander = b.holder(0, "", b.fake("kokoro-onnx-server.py", "--port", "8882", where="other-checkout"))
    done = b.run("stop")
    assert "Kokoro CPU fallback stopped" in done.stdout
    assert "faster-whisper CPU fallback stopped" in done.stdout
    deadline = time.time() + 5
    while time.time() < deadline and (b.alive(k) or b.alive(s)):
        time.sleep(0.05)
    assert not b.alive(k) and not b.alive(s)
    assert b.alive(bystander)
    assert not (b.scratch / "kokoro-cpu-fallback.pid").exists()
    assert not (b.scratch / "faster-whisper-cpu.pid").exists()


def test_stop_does_not_kill_a_stale_pid_file_s_unrelated_process(made):
    b = made(KFB)
    other = b.holder(0, "", b.fake("unrelated.py", where="elsewhere"), pidfile="kokoro-cpu-fallback.pid")
    done = b.run("stop")
    assert b.alive(other)
    assert "Kokoro CPU fallback was not running" in done.stdout
    assert not (b.scratch / "kokoro-cpu-fallback.pid").exists()


def test_status_shows_both_fallbacks(made):
    b = made(KFB + SFB)
    k = b.holder(8882, CPU_OK, b.fake("kokoro-onnx-server.py", "--port", "8882"), pidfile="kokoro-cpu-fallback.pid")
    s = b.holder(2023, FW_CPU, b.fake("faster-whisper-server.py", "--port", "2023"), pidfile="faster-whisper-cpu.pid")
    done = b.run("status")
    out = done.stdout
    assert "Kokoro CPU fallback (port 8882):" in out
    assert f"Kokoro CPU fallback running (PID {k})" in out
    assert "Provider: CPUExecutionProvider" in out
    assert "Speech recognition CPU fallback (faster-whisper, port 2023):" in out
    assert f"faster-whisper CPU fallback running (PID {s})" in out
    assert "Device: cpu" in out


def test_status_says_when_a_fallback_is_down(made):
    b = made(KFB + SFB)
    out = b.run("status").stdout
    assert "Kokoro CPU fallback not running" in out
    assert "faster-whisper CPU fallback not running" in out


def test_status_without_fallbacks_mentions_none(made):
    b = made(GPU + FW)
    assert "CPU fallback" not in b.run("status").stdout


def test_health_names_the_fallback_ports(made):
    b = made(KFB + SFB, TTS_URLS + STT_URLS)
    b.holder(8882, CPU_OK, b.fake("k.py", where="elsewhere"))
    out = b.run("health").stdout
    assert "Kokoro CPU fallback (8882) up" in out
    assert "faster-whisper CPU fallback (2023) DOWN" in out
