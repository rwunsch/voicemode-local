"""kokoro-onnx-server.py warm-up: one tiny synthesis after loading, before it serves.

Measured 2026-11-18 on WSL (RTX 4080 laptop, load 34-55, n=3 fresh starts each): on the GPU
the first request after /health turned ok took 5.3-11.8 s, the second 0.9-1.2 s. In-process
the first create() took 3.8-4.4 s and the second 0.85-1.06 s; after a "Hello." warm-up the
first real request took 1.5-2.0 s. The cost is a first-inference initialisation, not the model
load (which is done before the server listens) and not espeak (first phonemize 0.07-0.23 s).

So the server runs one synthesis of "Hello." before it binds its port: /health does not
answer until the warm-up is done (as during the model load), so nothing reports it healthy
early, and ensure's pid file keeps it from being started twice. One log line
"warm-up: <seconds> s". Off with --no-warmup or VOICEMODE_KOKORO_WARMUP=0. A failing warm-up
is logged and the server serves anyway (as before the warm-up existed).

The server runs as a subprocess against a fake kokoro_onnx (PYTHONPATH) that logs every
create() call, so no model is needed.
"""
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SERVER = REPO / "kokoro-onnx-server.py"

FAKE_KOKORO_ONNX = '''
import json
import os
import time
import numpy as np


class _Session:
    def get_providers(self):
        return ["CPUExecutionProvider"]


class Kokoro:
    def __init__(self, model_path, voices_path):
        self.sess = _Session()

    def get_voices(self):
        return ["af_sky", "bm_george"]

    def create(self, text, voice, speed=1.0, lang="en-us"):
        with open(os.environ["FAKE_LOG"], "a") as handle:
            handle.write(json.dumps({"text": text, "voice": voice, "lang": lang,
                                     "t": time.time()}) + "\\n")
        if os.environ.get("FAKE_CREATE_FAIL") == "1":
            raise RuntimeError("fake synthesis failure")
        time.sleep(float(os.environ.get("FAKE_CREATE_SLEEP", "0")))
        return np.zeros(240, dtype=np.float32), 24000
'''


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    def __init__(self, tmp_path, extra=None):
        fake = tmp_path / "fake" / "kokoro_onnx"
        fake.mkdir(parents=True)
        (fake / "__init__.py").write_text(FAKE_KOKORO_ONNX)
        self.models = tmp_path / "models"
        self.models.mkdir()
        for name in ("kokoro-v1.0.onnx", "voices-v1.0.bin"):
            (self.models / name).write_bytes(b"x")
        self.log = tmp_path / "create.log"
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("VOICEMODE_KOKORO_REQUIRE_GPU", "ONNX_PROVIDER",
                                 "VOICEMODE_KOKORO_PROVIDER", "VOICEMODE_KOKORO_THREADS",
                                 "VOICEMODE_KOKORO_WARMUP")}
        self.env.update({"PYTHONPATH": str(tmp_path / "fake"), "FAKE_LOG": str(self.log)})
        self.env.update(extra or {})
        self.proc = None

    def start(self, *args):
        self.port = _free_port()
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVER), "--host", "127.0.0.1", "--port", str(self.port),
             "--model-dir", str(self.models), "--no-download", *args],
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        return self

    def try_health(self):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=2) as r:
                return r.status
        except OSError:
            return None

    def wait_healthy(self, seconds=30):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise AssertionError(f"server exited {self.proc.returncode}: {self.proc.stdout.read()}")
            if self.try_health() == 200:
                return time.time()
            time.sleep(0.05)
        raise AssertionError("server did not come up")

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line]

    def output(self):
        self.stop()
        return self.proc.stdout.read()

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()


@pytest.fixture
def server(tmp_path):
    made = []

    def make(**kwargs):
        made.append(Server(tmp_path / str(len(made)), **kwargs))
        return made[-1]
    yield make
    for s in made:
        s.stop()


WARMUP_LINE = re.compile(r"^warm-up: \d+\.\d+ s$", re.M)


def test_the_server_synthesises_hello_once_before_it_is_healthy(server):
    s = server().start()
    ready = s.wait_healthy()
    calls = s.calls()
    assert [c["text"] for c in calls] == ["Hello."]
    assert calls[0]["voice"] == "af_sky" and calls[0]["lang"] == "en-us"
    assert calls[0]["t"] < ready
    assert WARMUP_LINE.search(s.output())


def test_health_does_not_answer_while_the_warm_up_runs(server):
    s = server(extra={"FAKE_CREATE_SLEEP": "3"}).start()
    deadline = time.time() + 20
    while not s.calls() and time.time() < deadline:
        time.sleep(0.05)
    assert s.calls(), "the warm-up never started"
    assert s.try_health() is None, "/health answered before the warm-up finished"
    s.wait_healthy()


def test_no_warmup_flag_skips_it(server):
    s = server().start("--no-warmup")
    s.wait_healthy()
    assert s.calls() == []
    assert not WARMUP_LINE.search(s.output())


def test_env_zero_skips_it(server):
    s = server(extra={"VOICEMODE_KOKORO_WARMUP": "0"}).start()
    s.wait_healthy()
    assert s.calls() == []
    assert "warm-up" not in s.output()


def test_env_one_keeps_it_on(server):
    s = server(extra={"VOICEMODE_KOKORO_WARMUP": "1"}).start()
    s.wait_healthy()
    assert [c["text"] for c in s.calls()] == ["Hello."]


def test_a_failed_warm_up_is_logged_and_the_server_still_serves(server):
    s = server(extra={"FAKE_CREATE_FAIL": "1"}).start()
    s.wait_healthy()
    out = s.output()
    assert "warm-up failed" in out and "fake synthesis failure" in out
