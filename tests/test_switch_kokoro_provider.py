"""voicemode-switch: KOKORO_PROVIDER and KOKORO_THREADS in ~/.voicemode-local/config.

  KOKORO_PROVIDER=auto|cuda|cpu|directml|coreml   passed to kokoro-onnx-server.py as
                     --provider; with the GPU engine (KOKORO_ONNX_GPU=true) it REPLACES
                     --require-gpu (cuda is the same fail-loud rule, auto falls back to the
                     CPU and says why). Unset: exactly as before.
  KOKORO_THREADS=N   passed as --threads N (also to the CPU fallback); 0 or unset: not passed.

`status` shows the active values, what /health reports, and how to change them.
Reuses the sandbox of test_switch_kokoro_gpu.py: nothing real starts.
"""
import subprocess
import time

from test_switch_kokoro_gpu import Box, GPU, _stub


def _args(b, log):
    return b.wait_for(log).strip()


# --- ensure / start pass-through -------------------------------------------------------

def test_gpu_with_provider_auto_passes_it_instead_of_require_gpu(tmp_path):
    b = Box(tmp_path, GPU + "KOKORO_ONNX_VENV=/opt/kgpu\nKOKORO_PROVIDER=auto\n")
    done = b.run("ensure")
    assert done.returncode == 0, done.stderr
    assert _args(b, b.gpu_log) == f"/opt/kgpu --port 8880 --model-dir {b.root}/models/kokoro --provider auto"


def test_gpu_with_provider_cuda(tmp_path):
    b = Box(tmp_path, GPU + "KOKORO_PROVIDER=cuda\n")
    b.run("ensure", "--quiet")
    args = _args(b, b.gpu_log)
    assert args.endswith("--provider cuda") and "--require-gpu" not in args


def test_gpu_without_provider_keeps_require_gpu_and_adds_threads(tmp_path):
    b = Box(tmp_path, GPU + "KOKORO_THREADS=4\n")
    b.run("ensure", "--quiet")
    assert _args(b, b.gpu_log).endswith("--require-gpu --threads 4")


def test_threads_zero_is_not_passed(tmp_path):
    b = Box(tmp_path, GPU + "KOKORO_PROVIDER=auto\nKOKORO_THREADS=0\n")
    b.run("ensure", "--quiet")
    assert _args(b, b.gpu_log).endswith("--provider auto")


def test_cpu_onnx_ensure_passes_provider_and_threads(tmp_path):
    b = Box(tmp_path, "KOKORO_ONNX=true\nKOKORO_PROVIDER=cpu\nKOKORO_THREADS=2\n")
    b.run("ensure", "--quiet")
    args = b.wait_for(b.py_log)
    assert f"kokoro-onnx-server.py --port 8880 --model-dir {b.root}/models/kokoro --provider cpu --threads 2" in args


def test_cpu_onnx_start_passes_provider_and_threads(tmp_path):
    b = Box(tmp_path, "KOKORO_ONNX=true\nKOKORO_PROVIDER=auto\nKOKORO_THREADS=3\n")
    _stub(b.bin / "lsof", "exit 1")
    done = b.run("start")
    assert done.returncode == 0, done.stderr
    assert "--port 8880 --model-dir" in b.wait_for(b.py_log)
    assert "--provider auto --threads 3" in b.read(b.py_log)


def test_gpu_start_passes_the_provider(tmp_path):
    b = Box(tmp_path, GPU + "STT_ENGINE=faster-whisper\nKOKORO_PROVIDER=auto\n")
    done = b.run("start")
    assert done.returncode == 0, done.stderr
    assert _args(b, b.gpu_log).endswith("--provider auto")


def test_the_cpu_fallback_gets_threads_but_never_the_provider(tmp_path):
    b = Box(tmp_path, GPU + "KOKORO_CPU_FALLBACK=true\nKOKORO_PROVIDER=auto\nKOKORO_THREADS=4\n",
            urls="VOICEMODE_TTS_BASE_URLS=http://127.0.0.1:8880/v1,http://127.0.0.1:8882/v1\n")
    cpu_log = b.scratch / "cpu-wrapper.log"
    _stub(b.root / "scripts/kokoro-cpu-start.sh", f'echo "$@" >> "{cpu_log}"; exit 0')
    b.run("ensure", "--quiet")
    args = _args(b, cpu_log)
    assert args.endswith("--no-download --threads 4") and "--provider" not in args


def test_without_the_keys_nothing_changes(tmp_path):
    b = Box(tmp_path, GPU)
    b.run("ensure", "--quiet")
    args = _args(b, b.gpu_log)
    assert args.endswith("--require-gpu")
    assert "--provider" not in args and "--threads" not in args


# --- status ----------------------------------------------------------------------------

def _health(b, body):
    _stub(b.bin / "curl", 'for a in "$@"; do case "$a" in *:8880/health) '
                          f"echo '{body}'; exit 0;; esac; done; exit 1")


def test_status_shows_the_settings_and_how_to_change_them(tmp_path):
    b = Box(tmp_path, GPU + "KOKORO_PROVIDER=auto\nKOKORO_THREADS=4\n")
    _health(b, '{"status": "ok", "provider": "CUDAExecutionProvider", '
               '"provider_requested": "auto", "fallback_reason": null, "threads": 4}')
    out = b.run("status").stdout
    assert "KOKORO_PROVIDER=auto" in out and "KOKORO_THREADS=4" in out
    assert "Provider: CUDAExecutionProvider" in out
    assert "~/.voicemode-local/config" in out and "voicemode-switch stop" in out


def test_status_with_auto_on_the_cpu_names_the_reason(tmp_path):
    b = Box(tmp_path, GPU + "KOKORO_PROVIDER=auto\n")
    _health(b, '{"status": "ok", "provider": "CPUExecutionProvider", '
               '"provider_requested": "auto", "fallback_reason": "onnxruntime offers no CUDA"}')
    out = b.run("status").stdout
    assert "Provider: CPUExecutionProvider" in out
    assert "onnxruntime offers no CUDA" in out
    assert "should have exited" not in out


def test_status_with_provider_cpu_on_the_gpu_engine_is_not_an_error(tmp_path):
    b = Box(tmp_path, GPU + "KOKORO_PROVIDER=cpu\n")
    _health(b, '{"status": "ok", "provider": "CPUExecutionProvider", "provider_requested": "cpu"}')
    out = b.run("status").stdout
    assert "Provider: CPUExecutionProvider" in out and "should have exited" not in out


def test_status_shows_unset_settings(tmp_path):
    b = Box(tmp_path, GPU)
    out = b.run("status").stdout
    assert "KOKORO_PROVIDER unset" in out and "KOKORO_THREADS unset" in out


def test_cpu_onnx_status_shows_the_settings(tmp_path):
    b = Box(tmp_path, "KOKORO_ONNX=true\nKOKORO_PROVIDER=cpu\n")
    out = b.run("status").stdout
    assert "Kokoro ONNX (native, port 8880):" in out
    assert "KOKORO_PROVIDER=cpu" in out
