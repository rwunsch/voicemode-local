"""piper-proxy.py warm-up: the default voice speaks once before the proxy serves.

The proxy already loaded the default voice's model before serving. Measured 2026-11-18 on
WSL (load 41-46, n=3 fresh starts): the first request after /health still took 13.4-19.4 s,
the second 3.7-6.1 s. In-process (load ~22, n=3) the synthesis itself was not slower the first
time (4.3-5.1 s vs 4.3-6.6 s); the cost was convert_wav's first call (3.8-7.6 s, then 0.0 s):
its lazy scipy import for the resampling.

So after loading the default voice the proxy synthesises "Hallo." with it and converts the
result as a request would, before it serves. Only the default voice: the others load on first
use, as before. One log line "warm-up: <seconds> s". Off with --no-warmup or
VOICEMODE_PIPER_WARMUP=0 (the model is then still pre-loaded, as before). A failing warm-up is
logged and the proxy serves anyway.

The proxy runs as a subprocess against a fake piper package (PYTHONPATH) that logs every
synthesis, so no model is needed.
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
PROXY = REPO / "piper-proxy.py"

FAKE_PIPER = '''
import json
import os
import time


class PiperVoice:
    @classmethod
    def load(cls, path):
        return cls()

    def synthesize_wav(self, text, wav_file):
        with open(os.environ["FAKE_LOG"], "a") as handle:
            handle.write(json.dumps({"text": text, "t": time.time()}) + "\\n")
        if os.environ.get("FAKE_SYNTH_FAIL") == "1":
            raise RuntimeError("fake synthesis failure")
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(22050)
        wav_file.writeframes(b"\\x00\\x00" * 2205)
'''


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Proxy:
    def __init__(self, tmp_path, extra=None, model_present=True):
        fake = tmp_path / "fake" / "piper"
        fake.mkdir(parents=True)
        (fake / "__init__.py").write_text(FAKE_PIPER)
        self.models = tmp_path / "models"
        self.models.mkdir()
        if model_present:
            (self.models / "de_DE-fake-high.onnx").write_bytes(b"x")
        self.voices = tmp_path / "voices.json"
        self.voices.write_text(json.dumps({
            "default_voice": "p_de_fake",
            "voices": [{"id": "p_de_fake", "piper_model": "de_DE-fake-high"},
                       {"id": "p_de_other", "piper_model": "de_DE-other-low"}]}))
        self.log = tmp_path / "synth.log"
        self.env = {k: v for k, v in os.environ.items() if k != "VOICEMODE_PIPER_WARMUP"}
        self.env.update({"PYTHONPATH": str(tmp_path / "fake"), "FAKE_LOG": str(self.log)})
        self.env.update(extra or {})
        self.proc = None

    def start(self, *args):
        self.port = _free_port()
        self.proc = subprocess.Popen(
            [sys.executable, str(PROXY), "--host", "127.0.0.1", "--port", str(self.port),
             "--voices-file", str(self.voices), "--models-dir", str(self.models), *args],
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        return self

    def wait_healthy(self, seconds=60):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise AssertionError(f"proxy exited {self.proc.returncode}: {self.proc.stdout.read()}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=2) as r:
                    if r.status == 200:
                        return time.time()
            except OSError:
                time.sleep(0.05)
        raise AssertionError("proxy did not come up")

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
def proxy(tmp_path):
    made = []

    def make(**kwargs):
        made.append(Proxy(tmp_path / str(len(made)), **kwargs))
        return made[-1]
    yield make
    for p in made:
        p.stop()


WARMUP_LINE = re.compile(r"warm-up: \d+\.\d+ s$", re.M)


def test_the_default_voice_speaks_once_before_the_proxy_answers(proxy):
    p = proxy().start()
    ready = p.wait_healthy()
    calls = p.calls()
    assert [c["text"] for c in calls] == ["Hallo."]
    assert calls[0]["t"] < ready
    assert WARMUP_LINE.search(p.output())


def test_the_warm_up_converts_the_audio_as_a_request_would(proxy, monkeypatch):
    """In-process: the warm-up passes the synthesised WAV through convert_wav (the lazy
    scipy import measured as the first-request cost)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("piper_proxy_warmup_under_test", PROXY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    converted = []

    class Voice:
        def synthesize_wav(self, text, wav_file):
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(22050)
            wav_file.writeframes(b"\x00\x00" * 10)

    monkeypatch.setattr(mod, "load_piper_voice", lambda path: Voice())
    monkeypatch.setattr(mod, "convert_wav", lambda wav, fmt: converted.append(fmt) or (b"", "audio/pcm"))
    seconds = mod.warm_up_voice(Path("/nonexistent/model.onnx"))
    assert converted == ["pcm"] and seconds >= 0


def test_a_failed_synthesis_keeps_its_own_error():
    """Found by the warm-up test: closing the half-written WAV raised '# channels not
    specified', which replaced the synthesis error (also in a request's 500 message)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("piper_proxy_synth_under_test", PROXY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    class Voice:
        def synthesize_wav(self, text, wav_file):
            raise RuntimeError("the real cause")

    with pytest.raises(RuntimeError, match="the real cause"):
        mod.synthesize_wav_bytes(Voice(), "Hallo.")


def test_no_warmup_flag_still_preloads_but_does_not_speak(proxy):
    p = proxy().start("--no-warmup")
    p.wait_healthy()
    assert p.calls() == []
    out = p.output()
    assert "Default voice warm." in out and not WARMUP_LINE.search(out)


def test_env_zero_skips_it(proxy):
    p = proxy(extra={"VOICEMODE_PIPER_WARMUP": "0"}).start()
    p.wait_healthy()
    assert p.calls() == []
    assert "warm-up" not in p.output()


def test_without_the_default_model_file_nothing_is_warmed(proxy):
    p = proxy(model_present=False).start()
    p.wait_healthy()
    assert p.calls() == []


def test_a_failed_warm_up_is_logged_and_the_proxy_still_serves(proxy):
    p = proxy(extra={"FAKE_SYNTH_FAIL": "1"}).start()
    p.wait_healthy()
    out = p.output()
    assert "warm-up failed" in out and "fake synthesis failure" in out
