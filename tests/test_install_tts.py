"""install.sh: the speech output (TTS) choice.

With an NVIDIA GPU (Linux/WSL, nvidia-smi lists one) the installer recommends native
GPU Kokoro: kokoro-onnx-server.py from its own venv on CUDA, on port 8880 in place of
the Docker Kokoro. It creates the venv with uv and writes KOKORO_ONNX=true,
KOKORO_ONNX_GPU=true, KOKORO_ONNX_VENV and KOKORO_ONNX_PORT=8880 to
~/.voicemode-local/config. Without a GPU nothing changes. --tts=docker (or
VOICEMODE_TTS=docker) forces the old Docker path.

kokoro-onnx requires the CPU `onnxruntime`, which shares its module directory with
`onnxruntime-gpu`: installed together, the CPU build won (validated 2026-11-17, one
venv: onnxruntime 1.32.0, no CUDA provider). So the setup removes `onnxruntime`,
reinstalls `onnxruntime-gpu==1.23.2` and checks that CUDAExecutionProvider is offered.

The helpers live in lib/tts.sh (sourced by install.sh after lib/stt.sh) and are tested
by sourcing them with stubbed nvidia-smi, uname, uv and venv python. The install.sh
wiring is tested by running its setup section (up to "Step 1: System packages") in a
fake HOME. Nothing is installed for real.
"""
import subprocess
from pathlib import Path

import pytest

from test_install_stt import Box as SttBox, _install_setup, _stub, REPO

TTS_SH = REPO / "lib" / "tts.sh"
INSTALL_SH = REPO / "install.sh"
STT_SH = REPO / "lib" / "stt.sh"


class Box(SttBox):
    def bash(self, code, stdin="", **extra):
        script = (f'set -euo pipefail\nVM_CONFIG_FILE="$HOME/.voicemode-local/config"\n'
                  f'. "{REPO}/lib/compute.sh"\n. "{STT_SH}"\n. "{TTS_SH}"\n{code}\n')
        return subprocess.run(["bash", "-c", script], input=stdin, capture_output=True, text=True,
                              env=self.env(**extra), timeout=30)

    def venv_python(self, venv="kg-venv"):
        """A venv python stub: logs its arguments, exits STUB_PY_EXIT (default 0)."""
        _stub(self.home / venv / "bin/python", f'echo "python $*" >> "{self.log}"; exit ${{STUB_PY_EXIT:-0}}')


# --- the choice --------------------------------------------------------------------

def test_with_a_gpu_native_gpu_is_the_default(tmp_path):
    done = Box(tmp_path).bash('vm_choose_tts_engine; echo "ENGINE=$TTS_ENGINE"', stdin="\n")
    assert done.returncode == 0, done.stderr
    assert "ENGINE=native-gpu" in done.stdout
    out = done.stdout.lower()
    assert "recommended" in out
    assert "8880" in done.stdout and "docker kokoro" in out     # says it replaces it
    assert "gb" in out                                           # and the disk it takes


def test_with_a_gpu_docker_can_still_be_chosen(tmp_path):
    done = Box(tmp_path).bash('vm_choose_tts_engine; echo "ENGINE=$TTS_ENGINE"', stdin="2\n")
    assert "ENGINE=docker" in done.stdout


def test_without_a_gpu_it_stays_docker_and_does_not_ask(tmp_path):
    done = Box(tmp_path, gpu="none-listed").bash('vm_choose_tts_engine < /dev/null; echo "ENGINE=$TTS_ENGINE"')
    assert done.returncode == 0, done.stderr
    assert "ENGINE=docker" in done.stdout


@pytest.mark.parametrize("how", ["flag", "env"])
def test_docker_can_be_forced_even_with_a_gpu(tmp_path, how):
    b = Box(tmp_path)
    if how == "flag":
        done = b.bash('TTS_CHOICE=docker; vm_choose_tts_engine < /dev/null; echo "ENGINE=$TTS_ENGINE"')
    else:
        done = b.bash('vm_choose_tts_engine < /dev/null; echo "ENGINE=$TTS_ENGINE"', VOICEMODE_TTS="docker")
    assert done.returncode == 0, done.stderr
    assert "ENGINE=docker" in done.stdout


def test_native_gpu_can_be_forced_without_asking(tmp_path):
    done = Box(tmp_path).bash('TTS_CHOICE=native-gpu; vm_choose_tts_engine < /dev/null; echo "ENGINE=$TTS_ENGINE"')
    assert "ENGINE=native-gpu" in done.stdout


def test_an_unknown_forced_choice_fails(tmp_path):
    done = Box(tmp_path).bash('TTS_CHOICE=cloud; vm_choose_tts_engine < /dev/null')
    assert done.returncode != 0
    assert "--tts" in done.stdout + done.stderr


# --- venv and config ---------------------------------------------------------------

PACKAGES = ("kokoro-onnx soundfile numpy onnxruntime-gpu==1.23.2 nvidia-cublas-cu12 nvidia-cudnn-cu12==9.* "
            "nvidia-cuda-runtime-cu12 nvidia-cufft-cu12 nvidia-curand-cu12 nvidia-cuda-nvrtc-cu12")


def test_setup_installs_then_replaces_the_cpu_onnxruntime_and_checks_cuda(tmp_path):
    b = Box(tmp_path)
    b.venv_python()
    done = b.bash('vm_setup_kokoro_gpu "$HOME/kg-venv"')
    assert done.returncode == 0, done.stderr
    py = f"{b.home}/kg-venv/bin/python"
    lines = b.calls().splitlines()
    assert lines[0] == f"uv pip install --python {py} {PACKAGES}"
    assert lines[1] == f"uv pip uninstall --python {py} onnxruntime"
    assert lines[2] == f"uv pip install --python {py} --reinstall-package onnxruntime-gpu onnxruntime-gpu==1.23.2"
    assert lines[3].startswith("python -c ") and "CUDAExecutionProvider" in lines[3]


def test_setup_creates_the_venv_when_missing(tmp_path):
    b = Box(tmp_path)
    b.bash('vm_setup_kokoro_gpu "$HOME/new-venv" || true')
    assert f"uv venv {b.home}/new-venv" in b.calls().splitlines()


def test_setup_reports_a_failed_install(tmp_path):
    b = Box(tmp_path)
    b.venv_python()
    done = b.bash('vm_setup_kokoro_gpu "$HOME/kg-venv" && echo OK || echo FAILED', STUB_UV_EXIT="1")
    assert "FAILED" in done.stdout


def test_setup_fails_when_cuda_is_not_offered(tmp_path):
    b = Box(tmp_path)
    b.venv_python()
    done = b.bash('vm_setup_kokoro_gpu "$HOME/kg-venv" && echo OK || echo FAILED', STUB_PY_EXIT="1")
    assert "FAILED" in done.stdout
    assert "CUDAExecutionProvider" in done.stdout + done.stderr


def test_write_tts_config_for_native_gpu(tmp_path):
    b = Box(tmp_path)
    cfg = b.home / ".voicemode-local/config"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("INSTALL_MODE=docker\nKOKORO_ONNX=false\n")
    done = b.bash('vm_write_tts_config native-gpu "$HOME/kg-venv"')
    assert done.returncode == 0, done.stderr
    text = cfg.read_text()
    assert "INSTALL_MODE=docker\n" in text
    assert text.count("KOKORO_ONNX=") == 1 and "KOKORO_ONNX=true\n" in text
    assert "KOKORO_ONNX_GPU=true\n" in text
    assert f"KOKORO_ONNX_VENV={b.home}/kg-venv\n" in text
    assert "KOKORO_ONNX_PORT=8880\n" in text


def test_write_tts_config_defaults_to_the_installer_venv(tmp_path):
    b = Box(tmp_path)
    b.bash('vm_write_tts_config native-gpu')
    text = (b.home / ".voicemode-local/config").read_text()
    assert f"KOKORO_ONNX_VENV={b.home}/.voicemode-local/kokoro-gpu-venv\n" in text


def test_write_tts_config_for_docker_turns_the_gpu_off_only(tmp_path):
    b = Box(tmp_path)
    cfg = b.home / ".voicemode-local/config"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("KOKORO_ONNX=true\nKOKORO_ONNX_GPU=true\n")
    b.bash('vm_write_tts_config docker')
    assert cfg.read_text() == "KOKORO_ONNX=true\nKOKORO_ONNX_GPU=false\n"


# --- install.sh wiring -------------------------------------------------------------

def test_install_with_a_gpu_writes_native_gpu_kokoro_by_default(tmp_path):
    done, cfg, b = _install_setup(tmp_path, stdin="1\nn\n\n\n")
    assert done.returncode == 0, done.stdout + done.stderr
    assert cfg.count("KOKORO_ONNX=") == 1 and "KOKORO_ONNX=true\n" in cfg
    assert "KOKORO_ONNX_GPU=true\n" in cfg
    assert f"KOKORO_ONNX_VENV={b.home}/.voicemode-local/kokoro-gpu-venv\n" in cfg
    assert "KOKORO_ONNX_PORT=8880\n" in cfg
    assert "INSTALL_MODE=docker\n" in cfg and "STT_ENGINE=faster-whisper\n" in cfg


def test_install_tts_docker_flag_forces_docker(tmp_path):
    done, cfg, _ = _install_setup(tmp_path, args=("--tts=docker",), stdin="1\nn\n\n")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "KOKORO_ONNX=false\n" in cfg
    assert "KOKORO_ONNX_GPU=true" not in cfg


def test_install_without_a_gpu_keeps_the_docker_kokoro(tmp_path):
    done, cfg, _ = _install_setup(tmp_path, gpu="none-listed", stdin="1\nn\n")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "KOKORO_ONNX=false\n" in cfg
    assert "KOKORO_ONNX_GPU=true" not in cfg


def test_install_sets_up_the_venv_starts_through_the_wrapper_and_can_fall_back():
    text = INSTALL_SH.read_text()
    tail = text[text.index("# ─── Step 1: System packages"):]
    assert "vm_setup_kokoro_gpu" in tail
    assert "scripts/kokoro-gpu-start.sh" in tail and "--require-gpu" in tail
    assert "/tmp/kokoro-onnx-server.pid" in tail
    assert "vm_write_tts_config docker" in tail                # the fallback
    assert "up -d kokoro" in tail                              # ... brings the Docker Kokoro back
    # The German model download stays as it is.
    assert '--download-german' in tail


# --- the CPU fallback (KOKORO_CPU_FALLBACK) ------------------------------------------

def test_write_tts_config_turns_the_cpu_fallback_on_and_says_what_it_costs(tmp_path):
    b = Box(tmp_path)
    done = b.bash('vm_write_tts_config native-gpu "$HOME/kg-venv"')
    assert done.returncode == 0, done.stderr
    assert "KOKORO_CPU_FALLBACK=true\n" in (b.home / ".voicemode-local/config").read_text()
    out = done.stdout
    assert "CPU fallback" in out and "8882" in out and "MB" in out
    assert "--no-cpu-fallback" in out


@pytest.mark.parametrize("how", ["flag", "env"])
def test_the_kokoro_cpu_fallback_can_be_opted_out(tmp_path, how):
    b = Box(tmp_path)
    if how == "flag":
        done = b.bash('CPU_FALLBACK=false; vm_write_tts_config native-gpu')
    else:
        done = b.bash('vm_write_tts_config native-gpu', VOICEMODE_CPU_FALLBACK="false")
    assert done.returncode == 0, done.stderr
    assert "KOKORO_CPU_FALLBACK=false\n" in (b.home / ".voicemode-local/config").read_text()


def test_install_with_a_gpu_turns_the_kokoro_cpu_fallback_on(tmp_path):
    done, cfg, _ = _install_setup(tmp_path, stdin="1\nn\n\n\n")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "KOKORO_CPU_FALLBACK=true\n" in cfg


def test_install_no_cpu_fallback_flag_turns_the_kokoro_fallback_off(tmp_path):
    done, cfg, _ = _install_setup(tmp_path, args=("--no-cpu-fallback",), stdin="1\nn\n\n\n")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "KOKORO_ONNX_GPU=true\n" in cfg
    assert "KOKORO_CPU_FALLBACK=false\n" in cfg


def test_install_tts_docker_writes_no_kokoro_fallback(tmp_path):
    _, cfg, _ = _install_setup(tmp_path, args=("--tts=docker",), stdin="1\nn\n\n")
    assert "KOKORO_CPU_FALLBACK" not in cfg
