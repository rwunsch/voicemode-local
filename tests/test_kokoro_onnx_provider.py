"""kokoro-onnx-server.py --provider auto|cuda|cpu|directml|coreml and --threads N.

Robert, 2026-11-17: the provider and the thread count are options, with the GPU as the
automatic choice when a supported GPU exists. Before this the server set no provider and no
session options: the provider came from kokoro-onnx's ONNX_PROVIDER, the threads from
onnxruntime's default.

  --provider (env VOICEMODE_KOKORO_PROVIDER, the flag wins)
    auto      CUDA when onnxruntime offers it AND the session really runs on it; else the
              CPU, with one visible log line saying why, and /health reporting
              provider_requested "auto" and fallback_reason. Never DirectML or CoreML.
    cuda / directml / coreml   not offered, or not what the session runs on: exit 1 naming
              the cause (the --require-gpu rule).
    cpu       CPUExecutionProvider only.
    unset     byte-identical to before: kokoro_onnx.Kokoro(model, voices).
  --threads N (env VOICEMODE_KOKORO_THREADS): SessionOptions.intra_op_num_threads; 0 or
    unset = onnxruntime's default. /health shows "threads" when set.

With a provider or threads the server builds the onnxruntime session itself and hands it
to kokoro_onnx.Kokoro.from_session (present in 0.4.9 and 0.6.1). The server runs as a
subprocess against a fake onnxruntime and a fake kokoro_onnx (PYTHONPATH) that log every
session they create to FAKE_LOG, so no model and no GPU is needed.
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

CUDA, CPU, DML, COREML = ("CUDAExecutionProvider", "CPUExecutionProvider",
                          "DmlExecutionProvider", "CoreMLExecutionProvider")

FAKE_ORT = '''
import json
import os

__version__ = "9.9.9"


def _log(entry):
    path = os.environ.get("FAKE_LOG")
    if path:
        with open(path, "a") as handle:
            handle.write(json.dumps(entry) + "\\n")


def get_available_providers():
    return [p for p in os.environ.get("FAKE_AVAILABLE", "CPUExecutionProvider").split(",") if p]


class SessionOptions:
    def __init__(self):
        self.intra_op_num_threads = 0


class InferenceSession:
    def __init__(self, path, sess_options=None, providers=None, **kwargs):
        self._model_path = path
        self.requested = list(providers or [])
        _log({"via": os.environ.get("FAKE_VIA", "server"), "model": os.path.basename(path),
              "providers": self.requested,
              "threads": getattr(sess_options, "intra_op_num_threads", None)})

    def get_providers(self):
        forced = os.environ.get("FAKE_SESSION_PROVIDERS")
        if forced:
            return forced.split(",")
        offered = get_available_providers()
        return [p for p in self.requested if p in offered] or ["CPUExecutionProvider"]
'''

FAKE_KOKORO_ONNX = '''
import os
import numpy as np
import onnxruntime as rt


class Kokoro:
    def __init__(self, model_path, voices_path):
        os.environ["FAKE_VIA"] = "constructor"
        try:
            self.sess = rt.InferenceSession(
                model_path, providers=[os.environ.get("ONNX_PROVIDER") or "CPUExecutionProvider"])
        finally:
            os.environ.pop("FAKE_VIA", None)
        self.voices_path = voices_path

    @classmethod
    def from_session(cls, session, voices_path):
        instance = cls.__new__(cls)
        instance.sess = session
        instance.voices_path = voices_path
        return instance

    def get_voices(self):
        return ["dm_martin"] if "martin" in str(self.voices_path) else ["af_sky"]

    def create(self, text, voice, speed, lang):
        return np.zeros(240, dtype=np.float32), 24000
'''


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Box:
    def __init__(self, tmp_path, available=CPU, session=None, from_session=True, extra=None):
        fake = tmp_path / "fake"
        (fake / "onnxruntime").mkdir(parents=True)
        (fake / "onnxruntime" / "__init__.py").write_text(FAKE_ORT)
        (fake / "kokoro_onnx").mkdir()
        code = FAKE_KOKORO_ONNX
        if not from_session:
            code = code.replace("def from_session", "def _no_from_session")
        (fake / "kokoro_onnx" / "__init__.py").write_text(code)
        self.models = tmp_path / "models"
        self.models.mkdir()
        for name in ("kokoro-v1.0.onnx", "voices-v1.0.bin"):
            (self.models / name).write_bytes(b"x")
        self.log = tmp_path / "sessions.log"
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("VOICEMODE_KOKORO_REQUIRE_GPU", "ONNX_PROVIDER",
                                 "VOICEMODE_KOKORO_PROVIDER", "VOICEMODE_KOKORO_THREADS")}
        self.env.update({"PYTHONPATH": str(fake), "FAKE_AVAILABLE": available,
                         "FAKE_LOG": str(self.log)})
        if session:
            self.env["FAKE_SESSION_PROVIDERS"] = session
        self.env.update(extra or {})
        self.proc = None

    def start(self, *args):
        self.port = _free_port()
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVER), "--host", "127.0.0.1", "--port", str(self.port),
             "--model-dir", str(self.models), "--no-download", *args],
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return self

    def health(self, seconds=20):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self.proc.poll() is not None:
                out, err = self.proc.communicate()
                raise AssertionError(f"server exited {self.proc.returncode}: {out}\n{err}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=3) as r:
                    return json.load(r)
            except OSError:
                time.sleep(0.1)
        raise AssertionError("server did not come up")

    def exits(self, *args):
        self.start(*args)
        try:
            out, err = self.proc.communicate(timeout=30)
        finally:
            self.stop()
        return self.proc.returncode, out, err

    def output(self):
        """Stop the server and return what it printed."""
        self.stop()
        out, err = self.proc.communicate()
        return out + err

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()

    def sessions(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line]


@pytest.fixture
def box(tmp_path):
    made = []

    def make(**kwargs):
        made.append(Box(tmp_path / str(len(made)), **kwargs))
        return made[-1]
    yield make
    for b in made:
        b.stop()


# --- auto --------------------------------------------------------------------------------

def test_auto_with_cuda_offered_and_used_runs_on_cuda(box):
    b = box(available=f"{CUDA},{CPU}").start("--provider", "auto")
    health = b.health()
    assert health["provider"] == CUDA
    assert health["provider_requested"] == "auto"
    assert health["fallback_reason"] is None
    assert b.sessions() == [{"via": "server", "model": "kokoro-v1.0.onnx",
                             "providers": [CUDA, CPU], "threads": 0}]


def test_auto_without_cuda_runs_on_the_cpu_and_says_why(box):
    b = box(available=CPU).start("--provider", "auto")
    health = b.health()
    assert health["provider"] == CPU
    assert health["provider_requested"] == "auto"
    assert "offers no CUDAExecutionProvider" in health["fallback_reason"]
    assert b.sessions()[0]["providers"] == [CPU]
    out = b.output()
    assert "--provider auto: running on the CPU" in out
    assert "offers no CUDAExecutionProvider" in out


def test_auto_when_cuda_is_listed_but_the_session_runs_on_the_cpu(box):
    b = box(available=f"{CUDA},{CPU}", session=CPU).start("--provider", "auto")
    health = b.health()
    assert health["provider"] == CPU
    assert "runs on CPUExecutionProvider" in health["fallback_reason"]
    assert "--provider auto: running on the CPU" in b.output()


def test_auto_never_picks_directml_or_coreml(box):
    b = box(available=f"{DML},{COREML},{CPU}").start("--provider", "auto")
    assert b.health()["provider"] == CPU
    assert b.sessions()[0]["providers"] == [CPU]


# --- explicit providers -------------------------------------------------------------------

@pytest.mark.parametrize("name,ep", [("cuda", CUDA), ("directml", DML), ("coreml", COREML)])
def test_an_explicit_provider_that_is_not_offered_exits_1_naming_it(box, name, ep):
    code, _, err = box(available=CPU).exits("--provider", name)
    assert code == 1
    assert f"--provider {name}" in err and ep in err
    assert "not offered" in err and "['CPUExecutionProvider']" in err


def test_explicit_cuda_offered_but_not_used_exits_1(box):
    code, _, err = box(available=f"{CUDA},{CPU}", session=CPU).exits("--provider", "cuda")
    assert code == 1
    assert "--provider cuda" in err and "runs on CPUExecutionProvider" in err


def test_explicit_cuda_offered_and_used_serves(box):
    b = box(available=f"{CUDA},{CPU}").start("--provider", "cuda")
    health = b.health()
    assert health["provider"] == CUDA and health["provider_requested"] == "cuda"
    assert "fallback_reason" not in health
    assert b.sessions()[0]["providers"] == [CUDA, CPU]


def test_explicit_directml_offered_and_used_serves(box):
    b = box(available=f"{DML},{CPU}").start("--provider", "directml")
    assert b.health()["provider"] == DML
    assert b.sessions()[0]["providers"] == [DML, CPU]


def test_cpu_forces_the_cpu_even_with_cuda_offered(box):
    b = box(available=f"{CUDA},{CPU}").start("--provider", "cpu")
    health = b.health()
    assert health["provider"] == CPU and health["provider_requested"] == "cpu"
    assert b.sessions()[0]["providers"] == [CPU]


def test_the_env_sets_the_provider(box):
    b = box(available=f"{CUDA},{CPU}", extra={"VOICEMODE_KOKORO_PROVIDER": "cpu"}).start()
    assert b.health()["provider_requested"] == "cpu"
    assert b.sessions()[0]["providers"] == [CPU]


def test_the_flag_wins_over_the_env(box):
    b = box(available=CPU, extra={"VOICEMODE_KOKORO_PROVIDER": "cuda"}).start("--provider", "cpu")
    assert b.health()["provider_requested"] == "cpu"


def test_an_unknown_provider_in_the_env_exits_2(box):
    code, _, err = box(extra={"VOICEMODE_KOKORO_PROVIDER": "tpu"}).exits()
    assert code == 2
    assert "VOICEMODE_KOKORO_PROVIDER" in err and "tpu" in err


def test_require_gpu_with_another_provider_is_refused(box):
    code, _, err = box(available=f"{CUDA},{CPU}").exits("--provider", "cpu", "--require-gpu")
    assert code == 2
    assert "--require-gpu" in err


def test_require_gpu_with_provider_cuda_is_allowed(box):
    b = box(available=f"{CUDA},{CPU}").start("--provider", "cuda", "--require-gpu")
    assert b.health()["provider"] == CUDA


def test_a_kokoro_onnx_without_from_session_exits_1_naming_it(box):
    code, _, err = box(from_session=False).exits("--provider", "cpu")
    assert code == 1
    assert "from_session" in err


def test_the_german_model_gets_the_same_provider(box, tmp_path):
    b = box(available=f"{CUDA},{CPU}")
    de = b.models.parent / "models-de-martin"
    de.mkdir()
    for name in ("kokoro-martin.onnx", "voices-martin.npz"):
        (de / name).write_bytes(b"x")
    b.start("--provider", "auto")
    b.health()
    assert [s["model"] for s in b.sessions()] == ["kokoro-v1.0.onnx", "kokoro-martin.onnx"]
    assert all(s["providers"] == [CUDA, CPU] for s in b.sessions())


# --- default: unchanged -------------------------------------------------------------------

def test_without_provider_or_threads_the_constructor_is_used_as_before(box):
    b = box(available=f"{CUDA},{CPU}").start()
    health = b.health()
    assert set(health) == {"status", "engine", "provider"}
    assert b.sessions() == [{"via": "constructor", "model": "kokoro-v1.0.onnx",
                             "providers": [CPU], "threads": None}]


def test_threads_zero_is_the_default(box):
    b = box().start("--threads", "0")
    assert "threads" not in b.health()
    assert b.sessions()[0]["via"] == "constructor"


# --- threads ------------------------------------------------------------------------------

def test_threads_set_the_intra_op_thread_count(box):
    b = box().start("--provider", "cpu", "--threads", "4")
    assert b.health()["threads"] == 4
    assert b.sessions()[0]["threads"] == 4


def test_threads_from_the_env(box):
    b = box(extra={"VOICEMODE_KOKORO_THREADS": "2"}).start("--provider", "cpu")
    assert b.health()["threads"] == 2
    assert b.sessions()[0]["threads"] == 2


def test_threads_without_a_provider_keep_the_onnx_provider_choice(box):
    b = box(available=f"{CUDA},{CPU}", extra={"ONNX_PROVIDER": CUDA}).start("--threads", "3")
    health = b.health()
    assert "provider_requested" not in health and health["threads"] == 3
    assert b.sessions()[0] == {"via": "server", "model": "kokoro-v1.0.onnx",
                               "providers": [CUDA], "threads": 3}


def test_env_only_provider_and_threads(box):
    """FELIX-voice: both from the environment alone, no flags."""
    b = box(available=f"{CUDA},{CPU}", extra={"VOICEMODE_KOKORO_PROVIDER": "auto",
                                              "VOICEMODE_KOKORO_THREADS": "6"}).start()
    health = b.health()
    assert health["provider"] == CUDA and health["provider_requested"] == "auto"
    assert health["threads"] == 6
    assert b.sessions()[0]["providers"] == [CUDA, CPU] and b.sessions()[0]["threads"] == 6


def test_the_flag_wins_over_the_env_for_threads(box):
    b = box(extra={"VOICEMODE_KOKORO_THREADS": "6"}).start("--provider", "cpu", "--threads", "2")
    assert b.health()["threads"] == 2


# --- the start log (FELIX reads it for its start notes) -----------------------------------

def _lines(text):
    return [line for line in text.splitlines() if line.strip()]


def test_the_first_log_line_names_the_requested_provider_and_threads(box):
    b = box(available=f"{CUDA},{CPU}").start("--provider", "auto", "--threads", "4")
    b.health()
    first = _lines(b.output())[0]
    assert first == "Kokoro ONNX options: provider requested auto, threads 4"


def test_the_provider_line_names_requested_and_threads(box):
    b = box(available=f"{CUDA},{CPU}").start("--provider", "auto", "--threads", "4")
    b.health()
    assert ("Kokoro ONNX provider: CUDAExecutionProvider (requested: auto), threads: 4"
            in _lines(b.output()))


def test_the_provider_line_names_the_fallback_reason_and_default_threads(box):
    b = box(available=CPU).start("--provider", "auto")
    b.health()
    line = next(l for l in _lines(b.output()) if l.startswith("Kokoro ONNX provider:"))
    assert line.startswith("Kokoro ONNX provider: CPUExecutionProvider (requested: auto, reason: "
                           "onnxruntime 9.9.9 offers no CUDAExecutionProvider")
    assert line.endswith("), threads: default")


def test_the_default_log_is_unchanged(box):
    b = box().start()
    b.health()
    lines = _lines(b.output())
    assert not any(l.startswith("Kokoro ONNX options:") for l in lines)
    assert "Kokoro ONNX provider: CPUExecutionProvider" in lines


@pytest.mark.parametrize("value", ["-1", "many"])
def test_bad_threads_exit_2(box, value):
    code, _, err = box(extra={"VOICEMODE_KOKORO_THREADS": value}).exits()
    assert code == 2 and "VOICEMODE_KOKORO_THREADS" in err
