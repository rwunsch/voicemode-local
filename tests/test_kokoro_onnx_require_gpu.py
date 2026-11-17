"""kokoro-onnx-server.py --require-gpu (and VOICEMODE_KOKORO_REQUIRE_GPU=1).

The default pip onnxruntime-gpu (1.32.0 on 2026-11-17) lists CUDAExecutionProvider but
fails to load it with the CUDA 12 pip libraries (it needs CUDA 13), and the session
silently falls back to the CPU: 10-20x slower, and nothing says so. With --require-gpu the
server checks the provider list of the session it actually got (kokoro_onnx.Kokoro.sess)
after loading the model, and exits 1 naming the cause when CUDA is not first. /health and
/v1/models report the active provider.

The server runs as a subprocess against a fake kokoro_onnx package (PYTHONPATH) whose
session reports the providers given in FAKE_PROVIDERS, so no model and no GPU is needed.
One optional test runs the real thing: VML_GPU_TESTS=1 and VML_KOKORO_GPU_VENV=<venv>.
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SERVER = REPO / "kokoro-onnx-server.py"
WRAPPER = REPO / "scripts" / "kokoro-gpu-start.sh"

FAKE_KOKORO_ONNX = '''
import os
import numpy as np

class _Session:
    def get_providers(self):
        return [p for p in os.environ.get("FAKE_PROVIDERS", "").split(",") if p]

class Kokoro:
    def __init__(self, model_path, voices_path):
        if os.environ.get("FAKE_NO_SESSION") != "1":
            self.sess = _Session()

    def get_voices(self):
        return ["af_sky"]

    def create(self, text, voice, speed, lang):
        return np.zeros(240, dtype=np.float32), 24000
'''


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _fake_env(tmp_path, providers, extra=None):
    pkg = tmp_path / "fake" / "kokoro_onnx"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(FAKE_KOKORO_ONNX)
    models = tmp_path / "models"
    models.mkdir()
    for name in ("kokoro-v1.0.onnx", "voices-v1.0.bin"):
        (models / name).write_bytes(b"x")
    env = {k: v for k, v in os.environ.items()
           if k not in ("VOICEMODE_KOKORO_REQUIRE_GPU", "ONNX_PROVIDER")}
    env.update({"PYTHONPATH": str(tmp_path / "fake"), "FAKE_PROVIDERS": providers})
    env.update(extra or {})
    return env, models


def _start(tmp_path, providers, args=("--require-gpu",), extra=None):
    env, models = _fake_env(tmp_path, providers, extra)
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, str(SERVER), "--host", "127.0.0.1", "--port", str(port),
         "--model-dir", str(models), "--no-download", *args],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return proc, port


def _get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as resp:
        return json.load(resp)


def _wait_up(proc, port, seconds=20):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if proc.poll() is not None:
            out, err = proc.communicate()
            raise AssertionError(f"server exited {proc.returncode}: {out}\n{err}")
        try:
            return _get(port, "/health")
        except OSError:
            time.sleep(0.1)
    raise AssertionError("server did not come up")


def _stop(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _exits(tmp_path, providers, args=("--require-gpu",), extra=None):
    proc, _ = _start(tmp_path, providers, args, extra)
    try:
        out, err = proc.communicate(timeout=30)
    finally:
        _stop(proc)
    return proc.returncode, out, err


CAUSE = ("onnxruntime-gpu loaded no CUDA provider; the default pip build needs CUDA 13: "
         "install onnxruntime-gpu==1.23.2")


def test_require_gpu_with_a_cpu_session_exits_1_naming_the_cause(tmp_path):
    code, _, err = _exits(tmp_path, "CPUExecutionProvider")
    assert code == 1
    assert CAUSE in err
    assert "CPUExecutionProvider" in err          # what it got instead


def test_require_gpu_with_cuda_listed_second_still_exits(tmp_path):
    code, _, err = _exits(tmp_path, "CPUExecutionProvider,CUDAExecutionProvider")
    assert code == 1 and CAUSE in err


def test_require_gpu_when_the_session_cannot_be_read_exits(tmp_path):
    code, _, err = _exits(tmp_path, "CUDAExecutionProvider", extra={"FAKE_NO_SESSION": "1"})
    assert code == 1
    assert "provider" in err.lower()


def test_the_env_switch_works_like_the_flag(tmp_path):
    code, _, err = _exits(tmp_path, "CPUExecutionProvider", args=(),
                          extra={"VOICEMODE_KOKORO_REQUIRE_GPU": "1"})
    assert code == 1 and CAUSE in err


def test_require_gpu_with_cuda_first_serves_and_health_shows_the_provider(tmp_path):
    proc, port = _start(tmp_path, "CUDAExecutionProvider,CPUExecutionProvider")
    try:
        health = _wait_up(proc, port)
        assert health["status"] == "ok"
        assert health["provider"] == "CUDAExecutionProvider"
        models = _get(port, "/v1/models")
        assert models["data"][0]["provider"] == "CUDAExecutionProvider"
    finally:
        _stop(proc)


def test_without_the_flag_a_cpu_session_still_serves(tmp_path):
    proc, port = _start(tmp_path, "CPUExecutionProvider", args=())
    try:
        health = _wait_up(proc, port)
        assert health["status"] == "ok"
        assert health["provider"] == "CPUExecutionProvider"
    finally:
        _stop(proc)


# --- the real thing (optional) ---------------------------------------------------------

@pytest.mark.skipif(os.environ.get("VML_GPU_TESTS") != "1" or not os.environ.get("VML_KOKORO_GPU_VENV"),
                    reason="set VML_GPU_TESTS=1 and VML_KOKORO_GPU_VENV=<venv> to run the real GPU path")
def test_real_gpu_server_synthesises_a_sentence(tmp_path):
    venv = os.environ["VML_KOKORO_GPU_VENV"]
    model_dir = os.environ.get("VML_KOKORO_MODEL_DIR", str(REPO / "models" / "kokoro"))
    port = _free_port()
    log = (tmp_path / "server.log").open("w")
    proc = subprocess.Popen([str(WRAPPER), venv, "--host", "127.0.0.1", "--port", str(port),
                             "--model-dir", model_dir, "--no-download", "--require-gpu"],
                            stdout=log, stderr=subprocess.STDOUT)
    try:
        health = _wait_up(proc, port, seconds=180)
        assert health["provider"] == "CUDAExecutionProvider"
        body = json.dumps({"input": "Hello from the GPU.", "voice": "af_sky"}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/audio/speech", data=body,
                                     headers={"Content-Type": "application/json"})
        start = time.monotonic()
        with urllib.request.urlopen(req, timeout=120) as resp:
            audio = resp.read()
        print(f"synthesised {len(audio)} bytes in {time.monotonic() - start:.2f}s")
        assert audio[:4] == b"RIFF" and len(audio) > 44 + 24000   # more than half a second
    finally:
        _stop(proc)
        log.close()
