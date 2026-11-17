"""install.sh: the speech recognition (STT) choice.

With an NVIDIA GPU (Linux/WSL, nvidia-smi lists one) the installer recommends the
native GPU engine: it creates a faster-whisper venv with uv and writes
STT_ENGINE=faster-whisper, FASTER_WHISPER_VENV and FASTER_WHISPER_MODEL=small to
~/.voicemode-local/config. Without a GPU nothing changes. --stt=docker (or
VOICEMODE_STT=docker) forces the old Docker path.

The helpers live in lib/stt.sh (sourced by install.sh, like lib/compute.sh) and are
tested by sourcing them with stubbed nvidia-smi, uname and uv. The install.sh
wiring is tested by running its setup section (up to "Step 1: System packages") in
a fake HOME. Nothing is installed for real.
"""
import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
STT_SH = REPO / "lib" / "stt.sh"
INSTALL_SH = REPO / "install.sh"
GPU_LINE = "GPU 0: NVIDIA GeForce RTX 4080 Laptop GPU (UUID: GPU-f69cada7-0000)"


def _stub(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/bash\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class Box:
    def __init__(self, tmp_path: Path, gpu=True, uname="Linux"):
        self.tmp = tmp_path
        self.bin, self.home = tmp_path / "bin", tmp_path / "home"
        self.home.mkdir(parents=True)
        self.log = tmp_path / "calls.log"
        _stub(self.bin / "uname", f'echo {uname}')
        if gpu is True:
            _stub(self.bin / "nvidia-smi", f'echo "nvidia-smi $*" >> "{self.log}"; echo "{GPU_LINE}"')
        elif gpu == "none-listed":
            _stub(self.bin / "nvidia-smi", 'echo "No devices were found"; exit 6')
        self.uv_exit = 0
        _stub(self.bin / "uv", f'echo "uv $*" >> "{self.log}"; exit ${{STUB_UV_EXIT:-0}}')

    def env(self, **extra):
        env = {"HOME": str(self.home), "PATH": f"{self.bin}:/usr/bin:/bin", **extra}
        return env

    def bash(self, code, stdin="", **extra):
        script = f'set -euo pipefail\nVM_CONFIG_FILE="$HOME/.voicemode-local/config"\n. "{REPO}/lib/compute.sh"\n. "{STT_SH}"\n{code}\n'
        return subprocess.run(["bash", "-c", script], input=stdin, capture_output=True, text=True,
                               env=self.env(**extra), timeout=30)

    def calls(self):
        return self.log.read_text() if self.log.exists() else ""


# --- GPU detection -----------------------------------------------------------------

def test_detects_a_gpu_that_nvidia_smi_lists(tmp_path):
    done = Box(tmp_path).bash('vm_has_nvidia_gpu && echo "YES $VM_STT_GPU_NAME"')
    assert done.returncode == 0, done.stderr
    assert "YES NVIDIA GeForce RTX 4080 Laptop GPU" in done.stdout


def test_no_gpu_when_nvidia_smi_lists_none(tmp_path):
    done = Box(tmp_path, gpu="none-listed").bash('vm_has_nvidia_gpu && echo YES || echo NO')
    assert done.stdout.strip() == "NO"


def test_no_gpu_without_nvidia_smi(tmp_path):
    if Path("/usr/bin/nvidia-smi").exists() or Path("/bin/nvidia-smi").exists():
        pytest.skip("this machine has nvidia-smi on the base PATH")
    done = Box(tmp_path, gpu=False).bash('vm_has_nvidia_gpu && echo YES || echo NO')
    assert done.stdout.strip() == "NO"


def test_no_gpu_off_linux(tmp_path):
    done = Box(tmp_path, uname="Darwin").bash('vm_has_nvidia_gpu && echo YES || echo NO')
    assert done.stdout.strip() == "NO"


# --- the choice --------------------------------------------------------------------

def test_with_a_gpu_native_gpu_is_the_default(tmp_path):
    done = Box(tmp_path).bash('vm_choose_stt_engine; echo "ENGINE=$STT_ENGINE"', stdin="\n")
    assert "ENGINE=faster-whisper" in done.stdout
    assert "recommended" in done.stdout.lower()


def test_with_a_gpu_docker_can_still_be_chosen(tmp_path):
    done = Box(tmp_path).bash('vm_choose_stt_engine; echo "ENGINE=$STT_ENGINE"', stdin="2\n")
    assert "ENGINE=docker" in done.stdout


def test_without_a_gpu_it_stays_docker_and_does_not_ask(tmp_path):
    done = Box(tmp_path, gpu="none-listed").bash('vm_choose_stt_engine < /dev/null; echo "ENGINE=$STT_ENGINE"')
    assert done.returncode == 0, done.stderr
    assert "ENGINE=docker" in done.stdout


@pytest.mark.parametrize("how", ["flag", "env"])
def test_docker_can_be_forced_even_with_a_gpu(tmp_path, how):
    b = Box(tmp_path)
    if how == "flag":
        done = b.bash('STT_CHOICE=docker; vm_choose_stt_engine < /dev/null; echo "ENGINE=$STT_ENGINE"')
    else:
        done = b.bash('vm_choose_stt_engine < /dev/null; echo "ENGINE=$STT_ENGINE"', VOICEMODE_STT="docker")
    assert done.returncode == 0, done.stderr
    assert "ENGINE=docker" in done.stdout


def test_native_gpu_can_be_forced_without_asking(tmp_path):
    done = Box(tmp_path).bash('STT_CHOICE=native-gpu; vm_choose_stt_engine < /dev/null; echo "ENGINE=$STT_ENGINE"')
    assert "ENGINE=faster-whisper" in done.stdout


def test_an_unknown_forced_choice_fails(tmp_path):
    done = Box(tmp_path).bash('STT_CHOICE=cloud; vm_choose_stt_engine < /dev/null')
    assert done.returncode != 0
    assert "--stt" in done.stdout + done.stderr


# --- venv and config ---------------------------------------------------------------

def test_setup_creates_the_venv_and_installs_the_cuda_packages(tmp_path):
    b = Box(tmp_path)
    done = b.bash('vm_setup_faster_whisper "$HOME/fw-venv"')
    assert done.returncode == 0, done.stderr
    venv = f"{b.home}/fw-venv"
    lines = b.calls().splitlines()
    assert f"uv venv {venv}" in lines
    assert (f"uv pip install --python {venv}/bin/python faster-whisper nvidia-cublas-cu12 "
            "nvidia-cudnn-cu12==9.*") in lines


def test_setup_reports_a_failed_install(tmp_path):
    done = Box(tmp_path).bash('vm_setup_faster_whisper "$HOME/fw-venv" && echo OK || echo FAILED',
                              STUB_UV_EXIT="1")
    assert "FAILED" in done.stdout


def test_write_stt_config_keeps_the_other_keys(tmp_path):
    b = Box(tmp_path)
    cfg = b.home / ".voicemode-local/config"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("INSTALL_MODE=docker\nSTT_ENGINE=docker\n")
    done = b.bash('vm_write_stt_config faster-whisper "$HOME/fw-venv" small')
    assert done.returncode == 0, done.stderr
    text = cfg.read_text()
    assert "INSTALL_MODE=docker\n" in text
    assert text.count("STT_ENGINE=") == 1 and "STT_ENGINE=faster-whisper\n" in text
    assert f"FASTER_WHISPER_VENV={b.home}/fw-venv\n" in text
    assert "FASTER_WHISPER_MODEL=small\n" in text


def test_write_stt_config_for_docker_writes_only_the_engine(tmp_path):
    b = Box(tmp_path)
    b.bash('vm_write_stt_config docker')
    text = (b.home / ".voicemode-local/config").read_text()
    assert text == "STT_ENGINE=docker\n"


# --- install.sh wiring -------------------------------------------------------------

def _install_setup(tmp_path: Path, gpu=True, args=(), stdin="1\nn\n\n"):
    """install.sh up to 'Step 1: System packages', in a fake HOME. docker is a stub
    with no nvidia runtime, so the Docker compute mode is CPU and asks nothing."""
    b = Box(tmp_path, gpu=gpu)
    text = INSTALL_SH.read_text()
    cut = text.index("# ─── Step 1: System packages")
    repo = tmp_path / "repo"
    (repo / "lib").mkdir(parents=True)
    (repo / "install.sh").write_text(text[:cut] + "exit 0\n")
    (repo / "install.sh").chmod(0o755)
    for name in ("compute.sh", "stt.sh", "tts.sh"):
        (repo / "lib" / name).write_text((REPO / "lib" / name).read_text())
    _stub(b.bin / "docker", 'exit 0')
    done = subprocess.run([str(repo / "install.sh"), *args], input=stdin, capture_output=True, text=True,
                          env=b.env(), timeout=30)
    cfg = b.home / ".voicemode-local/config"
    return done, cfg.read_text() if cfg.exists() else "", b


def test_install_with_a_gpu_writes_the_native_engine_by_default(tmp_path):
    done, cfg, b = _install_setup(tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "STT_ENGINE=faster-whisper\n" in cfg
    assert f"FASTER_WHISPER_VENV={b.home}/.voicemode-local/faster-whisper-venv\n" in cfg
    assert "FASTER_WHISPER_MODEL=small\n" in cfg
    assert "COMPUTE_MODE=cpu\n" in cfg and "INSTALL_MODE=docker\n" in cfg   # the rest as before


def test_install_without_a_gpu_keeps_docker_stt(tmp_path):
    done, cfg, _ = _install_setup(tmp_path, gpu="none-listed", stdin="1\nn\n")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "STT_ENGINE=docker\n" in cfg
    assert "FASTER_WHISPER" not in cfg


def test_install_stt_docker_flag_forces_docker(tmp_path):
    done, cfg, _ = _install_setup(tmp_path, args=("--stt=docker",), stdin="1\nn\n")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "STT_ENGINE=docker\n" in cfg
    assert "FASTER_WHISPER" not in cfg


def test_install_sets_up_the_venv_and_starts_through_the_wrapper():
    text = INSTALL_SH.read_text()
    tail = text[text.index("# ─── Step 1: System packages"):]
    assert "vm_setup_faster_whisper" in tail
    assert "scripts/faster-whisper-start.sh" in tail
    assert "/tmp/faster-whisper-server.pid" in tail
    assert '--host "$FW_HOST"' in tail and "vm_read_config FASTER_WHISPER_HOST 0.0.0.0" in tail


# --- the CPU fallback (STT_CPU_FALLBACK) ---------------------------------------------

def test_write_stt_config_turns_the_cpu_fallback_on_and_says_what_it_costs(tmp_path):
    b = Box(tmp_path)
    done = b.bash('vm_write_stt_config faster-whisper "$HOME/fw-venv" small')
    assert done.returncode == 0, done.stderr
    assert "STT_CPU_FALLBACK=true\n" in (b.home / ".voicemode-local/config").read_text()
    out = done.stdout
    assert "CPU fallback" in out and "2023" in out and "MB" in out
    assert "--no-cpu-fallback" in out                       # and how to opt out


@pytest.mark.parametrize("how", ["flag", "env"])
def test_the_stt_cpu_fallback_can_be_opted_out(tmp_path, how):
    b = Box(tmp_path)
    if how == "flag":
        done = b.bash('CPU_FALLBACK=false; vm_write_stt_config faster-whisper')
    else:
        done = b.bash('vm_write_stt_config faster-whisper', VOICEMODE_CPU_FALLBACK="false")
    assert done.returncode == 0, done.stderr
    assert "STT_CPU_FALLBACK=false\n" in (b.home / ".voicemode-local/config").read_text()


def test_install_with_a_gpu_turns_the_stt_cpu_fallback_on(tmp_path):
    done, cfg, _ = _install_setup(tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "STT_CPU_FALLBACK=true\n" in cfg


def test_install_no_cpu_fallback_flag_turns_it_off(tmp_path):
    done, cfg, _ = _install_setup(tmp_path, args=("--no-cpu-fallback",))
    assert done.returncode == 0, done.stdout + done.stderr
    assert "STT_ENGINE=faster-whisper\n" in cfg
    assert "STT_CPU_FALLBACK=false\n" in cfg


def test_install_without_a_gpu_writes_no_stt_fallback(tmp_path):
    _, cfg, _ = _install_setup(tmp_path, gpu="none-listed", stdin="1\nn\n")
    assert "STT_CPU_FALLBACK" not in cfg
