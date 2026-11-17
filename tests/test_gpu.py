"""vml_supervisor.gpu: one read-only NVIDIA GPU check, shared by the Windows native setup and
FELIX (which vendors vml_supervisor byte-identical). No test runs this machine's nvidia-smi:
every case fakes the lookup and the run."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from vml_supervisor import gpu  # noqa: E402

SMI = "/fake/bin/nvidia-smi"
LINE = "NVIDIA GeForce RTX 4080 Laptop GPU, 566.14\n"


def _world(stdout=LINE, code=0, error=None, found=True, stderr=""):
    calls = []

    def run(args, **kwargs):
        calls.append((list(args), kwargs))
        if error is not None:
            raise error
        return subprocess.CompletedProcess(args, code, stdout, stderr)

    which = (lambda name: SMI if found and name == "nvidia-smi" else None)
    return calls, run, which


def _probe(platform="linux", **kwargs):
    calls, run, which = _world(**kwargs)
    return gpu.probe(run=run, which=which, platform=platform), calls


def test_a_listed_gpu_with_a_new_enough_driver_is_available():
    result, calls = _probe()
    assert result.code == "ok"
    assert result.data == {"gpu": "NVIDIA GeForce RTX 4080 Laptop GPU", "driver": "566.14"}
    (args, kwargs), = calls
    assert args == [SMI, "--query-gpu=name,driver_version", "--format=csv,noheader"]
    assert kwargs["timeout"] == 5
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert "creationflags" not in kwargs


def test_on_windows_nvidia_smi_runs_without_a_window(monkeypatch):
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", no_window, raising=False)
    result, calls = _probe(platform="win32")
    assert result.code == "ok"
    assert calls[0][1]["creationflags"] == no_window


def test_the_first_gpu_is_reported_when_several_are_listed():
    result, _ = _probe(stdout="NVIDIA A, 535.10\nNVIDIA B, 535.10\n")
    assert result.code == "ok" and result.data["gpu"] == "NVIDIA A"


def test_macos_is_not_applicable_and_runs_nothing():
    result, calls = _probe(platform="darwin")
    assert result.code == "not_applicable" and calls == []
    assert result.data == {"gpu": None, "driver": None}


def test_no_nvidia_smi_on_path():
    result, calls = _probe(found=False)
    assert result.code == "no_nvidia_smi" and calls == []


def test_an_empty_list_is_no_gpu():
    result, _ = _probe(stdout="\n")
    assert result.code == "no_gpu_listed"


@pytest.mark.parametrize("driver,code", [("524.99", "driver_too_old"), ("470.82.01", "driver_too_old"),
                                         ("525.60.13", "ok"), ("525", "ok")])
def test_the_cuda_12_4_build_needs_driver_525(driver, code):
    result, _ = _probe(stdout=f"NVIDIA T4, {driver}\n")
    assert result.code == code
    assert result.data == {"gpu": "NVIDIA T4", "driver": driver}
    if code == "driver_too_old":
        assert driver in result.detail and "525" in result.detail


def test_a_failing_nvidia_smi_says_its_exit_code_and_first_line():
    result, _ = _probe(stdout="", code=9, stderr="NVIDIA-SMI has failed because it couldn't communicate\n")
    assert result.code == "nvidia_smi_failed"
    assert "9" in result.detail and "couldn't communicate" in result.detail


def test_an_oserror_is_a_failure():
    result, _ = _probe(error=OSError("access denied"))
    assert result.code == "nvidia_smi_failed" and "access denied" in result.detail


def test_an_unreadable_line_is_a_failure_not_a_guess():
    result, _ = _probe(stdout="something else entirely\n")
    assert result.code == "nvidia_smi_failed"


def test_a_timeout():
    result, _ = _probe(error=subprocess.TimeoutExpired(["nvidia-smi"], 5))
    assert result.code == "nvidia_smi_timeout" and "5" in result.detail


@pytest.mark.parametrize("code,available", [("ok", True), ("no_nvidia_smi", False), ("driver_too_old", False)])
def test_summary_is_the_one_json_shape(code, available):
    summary = gpu.summary(gpu.Result(code, "d", {"gpu": "G" if available else None, "driver": None}))
    assert summary == {"available": available, "reason": code, "detail": "d",
                       "gpu": "G" if available else None, "driver": None}


def test_main_prints_one_json_line(monkeypatch, capsys):
    calls, run, which = _world()
    real = gpu.probe
    monkeypatch.setattr(gpu, "probe", lambda: real(run=run, which=which, platform="linux"))
    assert gpu.main() == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    data = json.loads(out)
    assert data == {"available": True, "reason": "ok", "detail": data["detail"],
                    "gpu": "NVIDIA GeForce RTX 4080 Laptop GPU", "driver": "566.14"}


def test_python_dash_m_runs_it(tmp_path):
    """`python -m vml_supervisor.gpu` with no nvidia-smi on PATH: one JSON line, nothing started."""
    import os
    env = {**os.environ, "PATH": str(tmp_path)}
    done = subprocess.run([sys.executable, "-m", "vml_supervisor.gpu"], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    data = json.loads(done.stdout)
    assert set(data) == {"available", "reason", "detail", "gpu", "driver"}
    assert data["available"] is False
    assert data["reason"] in ("no_nvidia_smi", "not_applicable")


def test_the_module_is_vendorable():
    """Stdlib only, and no provider key names: FELIX vendors it byte-identical."""
    text = (REPO / "vml_supervisor" / "gpu.py").read_text(encoding="utf-8")
    assert "OPENAI_API_KEY" not in text and "CARTESIA_API_KEY" not in text
    imports = {line.split()[1].split(".")[0] for line in text.splitlines()
               if line.startswith(("import ", "from ")) and not line.startswith("from __future__")}
    assert imports <= {"json", "re", "shutil", "subprocess", "sys", "vml_supervisor", "result", "os", ""}
