"""scripts/faster-whisper-start.sh: put the venv's CUDA libraries on LD_LIBRARY_PATH,
then exec the server with the venv's python.

ctranslate2 dlopens cuBLAS/cuDNN at import, so the path must be set before the
process starts. The nvidia pip packages are namespace packages: their lib dirs are
nvidia.cublas.__path__[0]/lib and nvidia.cudnn.__path__[0]/lib.

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
WRAPPER = REPO / "scripts" / "faster-whisper-start.sh"


def _fake_venv(tmp_path, with_nvidia=True):
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    site = tmp_path / "site"
    site.mkdir()
    if with_nvidia:
        for pkg in ("cublas", "cudnn"):
            (site / "nvidia" / pkg / "lib").mkdir(parents=True)   # namespace packages: no __init__.py
    py = venv / "bin" / "python"
    py.write_text(
        "#!/bin/bash\n"
        'if [ "$1" = "-c" ]; then PYTHONPATH="' + str(site) + '" exec "' + sys.executable + '" "$@"; fi\n'
        'echo "LD=$LD_LIBRARY_PATH"\n'
        'echo "ARGS=$*"\n'
    )
    py.chmod(py.stat().st_mode | stat.S_IEXEC)
    return venv, site


def _run(args, env_extra=None):
    env = {k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"}
    env.update(env_extra or {})
    return subprocess.run([str(WRAPPER), *args], capture_output=True, text=True, env=env, timeout=30)


def test_wrapper_is_executable_bash():
    assert WRAPPER.read_text().startswith("#!/usr/bin/env bash\n")
    assert WRAPPER.stat().st_mode & stat.S_IXUSR
    assert subprocess.run(["bash", "-n", str(WRAPPER)]).returncode == 0


def test_puts_cublas_and_cudnn_lib_dirs_on_the_path_and_execs_the_server(tmp_path):
    venv, site = _fake_venv(tmp_path)
    done = _run([str(venv), "--port", "2022", "--model", "small"])
    assert done.returncode == 0, done.stderr
    ld = next(l for l in done.stdout.splitlines() if l.startswith("LD=")).split("=", 1)[1].split(":")
    assert str(site / "nvidia/cublas/lib") in ld
    assert str(site / "nvidia/cudnn/lib") in ld
    args = next(l for l in done.stdout.splitlines() if l.startswith("ARGS="))
    assert args == f"ARGS={REPO / 'faster-whisper-server.py'} --port 2022 --model small"


def test_keeps_an_existing_library_path_after_the_venv_dirs(tmp_path):
    venv, site = _fake_venv(tmp_path)
    done = _run([str(venv)], {"LD_LIBRARY_PATH": "/opt/other"})
    ld = next(l for l in done.stdout.splitlines() if l.startswith("LD=")).split("=", 1)[1].split(":")
    assert ld[-1] == "/opt/other"
    assert ld.index(str(site / "nvidia/cublas/lib")) < ld.index("/opt/other")


def test_without_nvidia_packages_it_still_starts_for_cpu(tmp_path):
    venv, _ = _fake_venv(tmp_path, with_nvidia=False)
    done = _run([str(venv), "--device", "cpu"])
    assert done.returncode == 0, done.stderr
    assert "LD=\n" in done.stdout or done.stdout.startswith("LD=\n")
    assert "--device cpu" in done.stdout


def test_a_missing_venv_fails_loud(tmp_path):
    done = _run([str(tmp_path / "nope")])
    assert done.returncode != 0
    assert "faster-whisper venv" in done.stderr and str(tmp_path / "nope") in done.stderr


def test_no_arguments_prints_usage():
    done = _run([])
    assert done.returncode != 0 and "usage" in done.stderr.lower()
