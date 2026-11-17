"""scripts/kokoro-cpu-start.sh: start kokoro-onnx-server.py on the CPU, as the CPU fallback
for the native GPU Kokoro.

It runs from the SAME venv as the GPU server (onnxruntime-gpu also offers
CPUExecutionProvider, so installer users need no second venv), with
ONNX_PROVIDER=CPUExecutionProvider and without --require-gpu. When that venv is absent it
uses the repo's .venv python. Unlike kokoro-gpu-start.sh it puts no CUDA libraries on the
library path: the CPU session needs none, and a broken CUDA install must not take the
fallback down with it.

The fake venv's python prints what it was exec'd with, so no server starts. The wrapper is
copied into a per-test app dir, so "the repo's .venv" is a fake too.
"""
import os
import shutil
import stat
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WRAPPER = REPO / "scripts" / "kokoro-cpu-start.sh"

FAKE_PY = ('#!/bin/bash\n'
           'echo "PY=$0"\n'
           'echo "LD=$LD_LIBRARY_PATH"\n'
           'echo "PROVIDER=$ONNX_PROVIDER"\n'
           'echo "ARGS=$*"\n')


def _py(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(FAKE_PY)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _app(tmp_path: Path) -> Path:
    app = tmp_path / "app"
    (app / "scripts").mkdir(parents=True)
    shutil.copy2(WRAPPER, app / "scripts" / WRAPPER.name)
    return app


def _run(app: Path, args, env_extra=None):
    env = {k: v for k, v in os.environ.items() if k not in ("LD_LIBRARY_PATH", "ONNX_PROVIDER")}
    env.update(env_extra or {})
    return subprocess.run([str(app / "scripts" / WRAPPER.name), *args],
                          capture_output=True, text=True, env=env, timeout=30)


def _value(out, key):
    return next(l for l in out.splitlines() if l.startswith(key + "=")).split("=", 1)[1]


def test_wrapper_is_executable_bash():
    assert WRAPPER.read_text().startswith("#!/usr/bin/env bash\n")
    assert WRAPPER.stat().st_mode & stat.S_IXUSR
    assert subprocess.run(["bash", "-n", str(WRAPPER)]).returncode == 0


def test_runs_the_server_from_the_gpu_venv_on_the_cpu_provider(tmp_path):
    app = _app(tmp_path)
    py = _py(tmp_path / "kgpu" / "bin" / "python")
    done = _run(app, [str(tmp_path / "kgpu"), "--port", "8882", "--no-download"])
    assert done.returncode == 0, done.stderr
    assert _value(done.stdout, "PY") == str(py)
    assert _value(done.stdout, "PROVIDER") == "CPUExecutionProvider"
    assert _value(done.stdout, "ARGS") == f"{app / 'kokoro-onnx-server.py'} --port 8882 --no-download"


def test_the_provider_is_cpu_even_if_the_caller_set_cuda(tmp_path):
    app = _app(tmp_path)
    _py(tmp_path / "kgpu" / "bin" / "python")
    done = _run(app, [str(tmp_path / "kgpu")], {"ONNX_PROVIDER": "CUDAExecutionProvider"})
    assert _value(done.stdout, "PROVIDER") == "CPUExecutionProvider"


def test_no_cuda_libraries_are_added_to_the_library_path(tmp_path):
    app = _app(tmp_path)
    _py(tmp_path / "kgpu" / "bin" / "python")
    done = _run(app, [str(tmp_path / "kgpu")], {"LD_LIBRARY_PATH": "/opt/other"})
    assert _value(done.stdout, "LD") == "/opt/other"


def test_require_gpu_is_never_passed_on(tmp_path):
    app = _app(tmp_path)
    _py(tmp_path / "kgpu" / "bin" / "python")
    done = _run(app, [str(tmp_path / "kgpu"), "--port", "8882", "--require-gpu"])
    assert done.returncode == 0, done.stderr
    assert "--require-gpu" not in _value(done.stdout, "ARGS")
    assert _value(done.stdout, "ARGS").endswith("--port 8882")


def test_the_require_gpu_environment_switch_is_cleared(tmp_path):
    app = _app(tmp_path)
    py = tmp_path / "kgpu" / "bin" / "python"
    _py(py)
    py.write_text(FAKE_PY + 'echo "REQ=${VOICEMODE_KOKORO_REQUIRE_GPU:-unset}"\n')
    done = _run(app, [str(tmp_path / "kgpu")], {"VOICEMODE_KOKORO_REQUIRE_GPU": "1"})
    assert _value(done.stdout, "REQ") == "unset"


def test_without_the_gpu_venv_it_uses_the_repo_venv(tmp_path):
    app = _app(tmp_path)
    repo_py = _py(app / ".venv" / "bin" / "python")
    done = _run(app, [str(tmp_path / "absent"), "--port", "8882"])
    assert done.returncode == 0, done.stderr
    assert _value(done.stdout, "PY") == str(repo_py)
    assert _value(done.stdout, "PROVIDER") == "CPUExecutionProvider"


def test_with_neither_venv_it_fails_loud_naming_both(tmp_path):
    app = _app(tmp_path)
    done = _run(app, [str(tmp_path / "absent")])
    assert done.returncode != 0
    assert str(tmp_path / "absent") in done.stderr
    assert str(app / ".venv") in done.stderr


def test_no_arguments_prints_usage(tmp_path):
    done = _run(_app(tmp_path), [])
    assert done.returncode != 0 and "usage" in done.stderr.lower()


# --- --provider / --threads (2026-11-17) ----------------------------------------------
# The fallback exists for the times CUDA failed, so it never takes a provider from the
# caller: --provider (both spellings) and VOICEMODE_KOKORO_PROVIDER are dropped, and
# ONNX_PROVIDER stays CPUExecutionProvider. --threads is passed on.

def test_a_provider_is_never_passed_on_but_threads_are(tmp_path):
    app = _app(tmp_path)
    _py(tmp_path / "kgpu" / "bin" / "python")
    done = _run(app, [str(tmp_path / "kgpu"), "--port", "8882", "--provider", "cuda",
                      "--provider=auto", "--threads", "4"])
    assert done.returncode == 0, done.stderr
    assert _value(done.stdout, "ARGS") == f"{app / 'kokoro-onnx-server.py'} --port 8882 --threads 4"


def test_the_provider_environment_switch_is_cleared(tmp_path):
    app = _app(tmp_path)
    py = tmp_path / "kgpu" / "bin" / "python"
    _py(py)
    py.write_text(FAKE_PY + 'echo "VKP=${VOICEMODE_KOKORO_PROVIDER:-unset}"\n')
    done = _run(app, [str(tmp_path / "kgpu")], {"VOICEMODE_KOKORO_PROVIDER": "cuda"})
    assert _value(done.stdout, "VKP") == "unset"
