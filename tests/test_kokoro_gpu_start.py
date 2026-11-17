"""scripts/kokoro-gpu-start.sh: start kokoro-onnx-server.py from the GPU venv, with the
venv's nvidia libraries on LD_LIBRARY_PATH and ONNX_PROVIDER=CUDAExecutionProvider.

onnxruntime-gpu dlopens the CUDA libraries when the session is created, from the
library path the process started with. The nvidia pip packages (cublas, cudnn,
cuda_runtime, cufft, curand, cuda_nvrtc, nvjitlink, ...) are namespace packages: each
lib dir is nvidia.<pkg>.__path__[0]/lib. kokoro_onnx takes the provider from
ONNX_PROVIDER (the server passes none).

The fake venv's python runs the real discovery snippet (-c) under this interpreter
with a fake nvidia namespace on PYTHONPATH, and for anything else prints what it was
exec'd with, so no server starts.
"""
import os
import stat
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WRAPPER = REPO / "scripts" / "kokoro-gpu-start.sh"
PKGS = ("cublas", "cudnn", "cuda_runtime", "cufft", "curand", "cuda_nvrtc", "nvjitlink")


def _fake_venv(tmp_path, pkgs=PKGS):
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    site = tmp_path / "site"
    site.mkdir()
    for pkg in pkgs:
        (site / "nvidia" / pkg / "lib").mkdir(parents=True)   # namespace packages: no __init__.py
    if pkgs:
        (site / "nvidia" / "no_lib_dir").mkdir()              # a subpackage without lib/ is skipped
    py = venv / "bin" / "python"
    py.write_text(
        "#!/bin/bash\n"
        'if [ "$1" = "-c" ]; then PYTHONPATH="' + str(site) + '" exec "' + sys.executable + '" "$@"; fi\n'
        'echo "LD=$LD_LIBRARY_PATH"\n'
        'echo "PROVIDER=$ONNX_PROVIDER"\n'
        'echo "ARGS=$*"\n'
    )
    py.chmod(py.stat().st_mode | stat.S_IEXEC)
    return venv, site


def _run(args, env_extra=None):
    env = {k: v for k, v in os.environ.items() if k not in ("LD_LIBRARY_PATH", "ONNX_PROVIDER")}
    env.update(env_extra or {})
    return subprocess.run([str(WRAPPER), *args], capture_output=True, text=True, env=env, timeout=30)


def _value(out, key):
    return next(l for l in out.splitlines() if l.startswith(key + "=")).split("=", 1)[1]


def test_wrapper_is_executable_bash():
    assert WRAPPER.read_text().startswith("#!/usr/bin/env bash\n")
    assert WRAPPER.stat().st_mode & stat.S_IXUSR
    assert subprocess.run(["bash", "-n", str(WRAPPER)]).returncode == 0


def test_puts_every_nvidia_lib_dir_on_the_path_sets_the_provider_and_execs_the_server(tmp_path):
    venv, site = _fake_venv(tmp_path)
    done = _run([str(venv), "--port", "8880", "--require-gpu"])
    assert done.returncode == 0, done.stderr
    ld = _value(done.stdout, "LD").split(":")
    for pkg in PKGS:
        assert str(site / "nvidia" / pkg / "lib") in ld
    assert not any("no_lib_dir" in d for d in ld)
    assert _value(done.stdout, "PROVIDER") == "CUDAExecutionProvider"
    assert _value(done.stdout, "ARGS") == f"{REPO / 'kokoro-onnx-server.py'} --port 8880 --require-gpu"


def test_keeps_an_existing_library_path_after_the_venv_dirs(tmp_path):
    venv, site = _fake_venv(tmp_path)
    done = _run([str(venv)], {"LD_LIBRARY_PATH": "/opt/other"})
    ld = _value(done.stdout, "LD").split(":")
    assert ld[-1] == "/opt/other"
    assert ld.index(str(site / "nvidia/cublas/lib")) < ld.index("/opt/other")


def test_the_provider_is_cuda_even_if_the_caller_set_another(tmp_path):
    venv, _ = _fake_venv(tmp_path)
    done = _run([str(venv)], {"ONNX_PROVIDER": "CPUExecutionProvider"})
    assert _value(done.stdout, "PROVIDER") == "CUDAExecutionProvider"


def test_without_nvidia_packages_the_path_is_left_alone(tmp_path):
    venv, _ = _fake_venv(tmp_path, pkgs=())
    done = _run([str(venv)])
    assert done.returncode == 0, done.stderr
    assert _value(done.stdout, "LD") == ""


def test_a_missing_venv_fails_loud(tmp_path):
    done = _run([str(tmp_path / "nope")])
    assert done.returncode != 0
    assert "Kokoro GPU venv" in done.stderr and str(tmp_path / "nope") in done.stderr
    assert "KOKORO_ONNX_VENV" in done.stderr


def test_no_arguments_prints_usage():
    done = _run([])
    assert done.returncode != 0 and "usage" in done.stderr.lower()
