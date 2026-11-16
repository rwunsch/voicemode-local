"""faster-whisper-server.py: native speech recognition, OpenAI-compatible.

The model loader is injectable, so these tests run the real HTTP server in-process
on a free port with a fake model: no GPU, no faster-whisper, no download. One
optional test (VML_GPU_TESTS=1) runs the real GPU path through the start wrapper.
"""
import importlib.util
import io
import json
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

REPO = Path(__file__).resolve().parent.parent
SERVER = REPO / "faster-whisper-server.py"
WRAPPER = REPO / "scripts" / "faster-whisper-start.sh"


def _server_module():
    spec = importlib.util.spec_from_file_location("faster_whisper_server_under_test", SERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fws = _server_module()


# --- fakes -------------------------------------------------------------------------

class FakeModel:
    """Stands in for faster_whisper.WhisperModel: transcribe(array, ...) -> (segments, info).

    Like the real one, the segments are a lazy generator, so the work happens while the
    caller iterates them. Records every call and whether two ever overlapped."""

    def __init__(self, text="hello world", delay=0.0, fail_warmup=False):
        self.text, self.delay, self.fail_warmup = text, delay, fail_warmup
        self.calls = []
        self.active = 0
        self.max_active = 0
        self._guard = threading.Lock()

    def transcribe(self, audio, **kwargs):
        self.calls.append((audio, kwargs))
        if self.fail_warmup and len(self.calls) == 1:
            raise RuntimeError("CUDA failed to initialise")

        def segments():
            with self._guard:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            try:
                time.sleep(self.delay)
                yield SimpleNamespace(id=0, seek=0, start=0.0, end=1.0, text=" " + self.text,
                                      tokens=[1], temperature=0.0, avg_logprob=-0.1,
                                      compression_ratio=1.0, no_speech_prob=0.01)
            finally:
                with self._guard:
                    self.active -= 1

        info = SimpleNamespace(language=kwargs.get("language") or "en",
                               duration=len(audio) / fws.SAMPLE_RATE)
        return segments(), info


def _wav(seconds=1.0, rate=16000, channels=1, width=2, freq=440.0):
    n = int(seconds * rate)
    t = np.arange(n) / rate
    tone = 0.3 * np.sin(2 * np.pi * freq * t)
    if width == 2:
        frames = (tone * 32767).astype("<i2")
    elif width == 1:
        frames = ((tone * 127) + 128).astype(np.uint8)
    elif width == 4:
        frames = (tone * 2147483647).astype("<i4")
    else:
        raise ValueError(width)
    if channels == 2:
        frames = np.repeat(frames, 2)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(frames.tobytes())
    return buf.getvalue()


def _multipart(file_bytes, filename="audio.wav", **fields):
    boundary = "----TestBoundary7MA4YWxkTrZu0gW"
    out = io.BytesIO()
    for name, value in fields.items():
        out.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
    if file_bytes is not None:
        out.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
                  "Content-Type: application/octet-stream\r\n\r\n".encode())
        out.write(file_bytes)
        out.write(b"\r\n")
    out.write(f"--{boundary}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={boundary}"


class Running:
    """The real server on a free port, in this process, with a fake model."""

    def __init__(self, model, load=True):
        self.model = model
        self.transcriber = fws.Transcriber(loader=lambda: model, name="small", device="cpu")
        self.server = fws.make_server("127.0.0.1", 0, self.transcriber)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        if load:
            self.transcriber.load()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def get(self, path):
        try:
            with urllib.request.urlopen(self.url(path), timeout=10) as r:
                return r.status, r.headers.get("Content-Type", ""), r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type", ""), e.read()

    def post(self, body, content_type):
        req = urllib.request.Request(self.url("/v1/audio/transcriptions"), data=body,
                                     headers={"Content-Type": content_type}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.headers.get("Content-Type", ""), r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type", ""), e.read()

    def transcribe(self, wav_bytes=None, **fields):
        body, ctype = _multipart(_wav() if wav_bytes is None else wav_bytes, **fields)
        return self.post(body, ctype)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def running():
    started = []

    def start(model=None, load=True):
        r = Running(model or FakeModel(), load=load)
        started.append(r)
        return r

    yield start
    for r in started:
        r.close()


# --- WAV decoding ------------------------------------------------------------------

def test_decode_wav_returns_16k_mono_float32():
    audio = fws.decode_wav(_wav(seconds=1.0, rate=16000))
    assert audio.dtype == np.float32 and audio.ndim == 1
    assert len(audio) == 16000
    assert 0.25 < float(np.max(np.abs(audio))) <= 1.0


def test_decode_wav_resamples_to_16k():
    audio = fws.decode_wav(_wav(seconds=2.0, rate=24000))
    assert abs(len(audio) - 32000) <= 1


def test_decode_wav_mixes_stereo_down_to_mono():
    audio = fws.decode_wav(_wav(seconds=0.5, rate=16000, channels=2))
    assert audio.ndim == 1 and len(audio) == 8000


@pytest.mark.parametrize("width", [1, 4])
def test_decode_wav_handles_8_and_32_bit(width):
    audio = fws.decode_wav(_wav(seconds=0.5, rate=16000, width=width))
    assert len(audio) == 8000
    assert 0.2 < float(np.max(np.abs(audio))) <= 1.0


@pytest.mark.parametrize("data,name", [
    (b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 64, "mp3"),
    (b"\xff\xfb\x90\x00" + b"\x00" * 64, "mp3"),
    (b"OggS" + b"\x00" * 64, "ogg"),
    (b"fLaC" + b"\x00" * 64, "flac"),
    (b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 64, "m4a"),
    (b"\x1a\x45\xdf\xa3" + b"\x00" * 64, "webm"),
])
def test_decode_wav_names_a_non_wav_format(data, name):
    with pytest.raises(fws.UnsupportedAudio) as err:
        fws.decode_wav(data)
    assert name in str(err.value)


# --- request handling ----------------------------------------------------------------

def test_text_format_returns_plain_text(running):
    r = running(FakeModel(text="hello world"))
    status, ctype, body = r.transcribe(response_format="text", model="whisper-1", language="auto")
    assert status == 200
    assert ctype.startswith("text/plain")
    assert body.decode() == "hello world"


def test_json_format_is_the_default(running):
    r = running(FakeModel(text="hello world"))
    status, ctype, body = r.transcribe()
    assert status == 200 and ctype.startswith("application/json")
    assert json.loads(body) == {"text": "hello world"}


def test_verbose_json_has_language_duration_and_segments(running):
    r = running(FakeModel(text="hello world"))
    status, _, body = r.transcribe(response_format="verbose_json", language="de")
    assert status == 200
    data = json.loads(body)
    assert data["text"] == "hello world"
    assert data["language"] == "de"
    assert abs(data["duration"] - 1.0) < 0.01
    assert data["task"] == "transcribe"
    assert data["segments"][0]["text"] == " hello world"
    assert data["segments"][0]["start"] == 0.0 and data["segments"][0]["end"] == 1.0


def test_language_auto_means_detect_and_prompt_is_passed(running):
    model = FakeModel()
    r = running(model)
    r.transcribe(language="auto", prompt="Kokoro, Piper")
    audio, kwargs = model.calls[-1]
    assert kwargs.get("language") is None
    assert kwargs.get("initial_prompt") == "Kokoro, Piper"
    assert audio.dtype == np.float32 and len(audio) == 16000


def test_an_explicit_language_is_passed(running):
    model = FakeModel()
    r = running(model)
    r.transcribe(language="de")
    assert model.calls[-1][1].get("language") == "de"


def test_a_non_wav_upload_gets_415_naming_the_format(running, no_ffmpeg):
    r = running()
    status, _, body = r.transcribe(wav_bytes=b"ID3\x04\x00" + b"\x00" * 200, filename="audio.mp3")
    assert status == 415
    assert "mp3" in json.loads(body)["error"]["message"]


def test_an_unknown_upload_gets_415_not_a_crash(running, no_ffmpeg):
    r = running()
    status, _, body = r.transcribe(wav_bytes=b"garbage bytes that are no audio at all")
    assert status == 415
    assert "WAV" in json.loads(body)["error"]["message"]
    status, _, _ = r.get("/health")
    assert status == 200   # the server is still up


def test_missing_file_is_a_400(running):
    r = running()
    body, ctype = _multipart(None, response_format="text")
    status, _, _ = r.post(body, ctype)
    assert status == 400


def test_not_multipart_is_a_400(running):
    r = running()
    status, _, _ = r.post(b"{}", "application/json")
    assert status == 400


def test_an_unsupported_response_format_is_a_400(running):
    r = running()
    status, _, body = r.transcribe(response_format="srt")
    assert status == 400 and "srt" in json.loads(body)["error"]["message"]


def test_unknown_paths_are_404(running):
    r = running()
    assert r.get("/nope")[0] == 404


# --- non-WAV uploads through ffmpeg ----------------------------------------------------
# hybrid mode (OpenAI first, local second) sends mp3 to the local fallback; with an
# ffmpeg on PATH the server decodes it, without one it keeps the 415.

FFMPEG_ARGS = "-i pipe:0 -f s16le -ac 1 -ar 16000 pipe:1"


@pytest.fixture
def no_ffmpeg(tmp_path, monkeypatch):
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))


def _fake_ffmpeg(tmp_path, monkeypatch, body):
    bin_dir = tmp_path / "ffmpeg-bin"
    bin_dir.mkdir()
    stub = bin_dir / "ffmpeg"
    stub.write_text("#!/bin/bash\n" + body + "\n")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    return bin_dir


MP3 = b"ID3\x04\x00" + b"\x00" * 200


def test_a_non_wav_upload_is_decoded_by_ffmpeg(running, tmp_path, monkeypatch):
    known = (np.arange(8000, dtype=np.int16) - 4000).astype("<i2")
    pcm = tmp_path / "known.pcm"
    pcm.write_bytes(known.tobytes())
    log, stdin_copy = tmp_path / "ffmpeg.log", tmp_path / "stdin.bin"
    _fake_ffmpeg(tmp_path, monkeypatch,
                 f'echo "$*" > "{log}"; cat > "{stdin_copy}"; cat "{pcm}"')
    model = FakeModel(text="from mp3")
    r = running(model)
    status, _, body = r.transcribe(wav_bytes=MP3, filename="audio.mp3", response_format="text")
    assert status == 200, body
    assert body.decode() == "from mp3"
    assert FFMPEG_ARGS in log.read_text()
    assert stdin_copy.read_bytes() == MP3
    audio = model.calls[-1][0]
    assert audio.dtype == np.float32 and len(audio) == 8000
    assert np.allclose(audio, known.astype(np.float32) / 32768.0)


def test_ffmpeg_failing_is_a_clear_error_not_a_crash(running, tmp_path, monkeypatch):
    _fake_ffmpeg(tmp_path, monkeypatch, 'cat > /dev/null; echo "Invalid data found when processing input" >&2; exit 1')
    r = running()
    status, _, body = r.transcribe(wav_bytes=MP3, filename="audio.mp3")
    assert 400 <= status < 600 and status != 200
    message = json.loads(body)["error"]["message"]
    assert "ffmpeg" in message and "mp3" in message
    assert "Invalid data found" in message
    assert r.get("/health")[0] == 200   # still up


def test_ffmpeg_producing_nothing_is_a_clear_error(running, tmp_path, monkeypatch):
    _fake_ffmpeg(tmp_path, monkeypatch, 'cat > /dev/null; exit 0')
    r = running()
    status, _, body = r.transcribe(wav_bytes=MP3, filename="audio.mp3")
    assert 400 <= status < 600 and status != 200
    assert "ffmpeg" in json.loads(body)["error"]["message"]


def test_ffmpeg_is_run_without_a_shell_and_with_a_timeout(tmp_path, monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"], seen["kwargs"] = cmd, kwargs
        return SimpleNamespace(returncode=0, stdout=b"\x00\x00" * 160, stderr=b"")

    monkeypatch.setattr(fws.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(fws.subprocess, "run", fake_run)
    audio = fws.decode_audio(MP3)
    assert len(audio) == 160
    assert isinstance(seen["cmd"], list) and seen["cmd"][0] == "/usr/bin/ffmpeg"
    assert " ".join(seen["cmd"]).endswith(FFMPEG_ARGS)
    assert seen["kwargs"].get("shell") in (None, False)
    assert 0 < seen["kwargs"]["timeout"] <= 60
    assert seen["kwargs"]["input"] == MP3


def test_ffmpeg_timing_out_is_a_clear_error(monkeypatch):
    def slow(cmd, **kwargs):
        raise fws.subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(fws.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(fws.subprocess, "run", slow)
    with pytest.raises(fws.AudioDecodeError) as err:
        fws.decode_audio(MP3)
    assert "timed out" in str(err.value)


def test_wav_does_not_need_ffmpeg(no_ffmpeg):
    assert len(fws.decode_audio(_wav(seconds=0.5))) == 8000


# --- health and warm-up --------------------------------------------------------------

def test_health_is_503_before_the_model_is_loaded_and_200_after(running):
    r = running(load=False)
    assert r.get("/health")[0] == 503
    assert r.get("/v1/models")[0] == 503
    status, _, _ = r.transcribe()
    assert status == 503
    r.transcriber.load()
    status, _, body = r.get("/health")
    assert status == 200 and json.loads(body)["status"] == "ok"
    status, _, body = r.get("/v1/models")
    assert status == 200 and json.loads(body)["data"]


def test_load_runs_a_warm_up_transcription(running):
    model = FakeModel()
    running(model)
    assert len(model.calls) == 1
    assert len(model.calls[0][0]) > 0


def test_a_failed_warm_up_keeps_health_503(running):
    r = running(FakeModel(fail_warmup=True), load=False)
    with pytest.raises(RuntimeError):
        r.transcriber.load()
    status, _, body = r.get("/health")
    assert status == 503
    assert "CUDA failed" in json.loads(body)["error"]


# --- serialization -------------------------------------------------------------------

def test_concurrent_requests_are_all_answered_and_never_overlap(running):
    model = FakeModel(text="ok", delay=0.3)
    r = running(model)
    results = []

    def one():
        results.append(r.transcribe(response_format="text"))

    threads = [threading.Thread(target=one) for _ in range(3)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    elapsed = time.perf_counter() - t0
    assert [s for s, _, _ in results] == [200, 200, 200]
    assert model.max_active == 1
    assert elapsed >= 0.85   # three 0.3 s jobs, one after another


def test_each_request_logs_audio_and_transcription_seconds(running, capsys):
    r = running(FakeModel(text="hi"))
    r.transcribe(wav_bytes=_wav(seconds=2.0))
    out = capsys.readouterr().out
    assert "audio=2.00s" in out
    assert "transcribe=" in out


# --- command line --------------------------------------------------------------------

def test_compute_type_defaults_follow_the_device():
    assert fws.default_compute_type("cuda") == "float16"
    assert fws.default_compute_type("cpu") == "int8"


def test_help_lists_the_arguments():
    done = subprocess.run([os.sys.executable, str(SERVER), "--help"], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0
    for flag in ("--port", "--model", "--device", "--compute-type", "--model-dir"):
        assert flag in done.stdout


# --- the real GPU path (optional) ----------------------------------------------------

GPU_VENV = Path(os.environ.get("VML_GPU_VENV", Path.home() / ".cache/voicemode-local/stt-gpu-venv"))


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _spoken_wav(text):
    """Speech made in-process by kokoro-onnx (24 kHz, so the resampler is exercised too)."""
    kokoro_onnx = pytest.importorskip("kokoro_onnx")
    model_dir = REPO / "models" / "kokoro"
    if not (model_dir / "kokoro-v1.0.onnx").exists():
        pytest.skip("Kokoro model not downloaded")
    kokoro = kokoro_onnx.Kokoro(str(model_dir / "kokoro-v1.0.onnx"), str(model_dir / "voices-v1.0.bin"))
    samples, rate = kokoro.create(text, voice="af_sky", speed=1.0, lang="en-us")
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue(), len(samples) / rate


@pytest.mark.skipif(os.environ.get("VML_GPU_TESTS") != "1", reason="set VML_GPU_TESTS=1 to run the real GPU path")
def test_real_gpu_transcription_through_the_wrapper(tmp_path):
    if not (GPU_VENV / "bin/python").exists():
        pytest.skip(f"no faster-whisper venv at {GPU_VENV}")
    wav_bytes, seconds = _spoken_wav("The quick brown fox jumps over the lazy dog.")
    port = _free_port()
    log = tmp_path / "server.log"
    model = os.environ.get("VML_GPU_MODEL", "small")
    t0 = time.perf_counter()
    with open(log, "w") as fh:
        proc = subprocess.Popen([str(WRAPPER), str(GPU_VENV), "--port", str(port), "--model", model,
                                 "--device", "cuda"], stdout=fh, stderr=subprocess.STDOUT)
    try:
        deadline = time.time() + 300
        while time.time() < deadline:
            assert proc.poll() is None, log.read_text()
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as resp:
                    if resp.status == 200:
                        break
            except (urllib.error.URLError, ConnectionError, OSError):
                pass
            time.sleep(0.5)
        else:
            pytest.fail("server did not become healthy:\n" + log.read_text())
        ready = time.perf_counter() - t0
        body, ctype = _multipart(wav_bytes, response_format="text", language="auto", model="whisper-1")
        timings = []
        for _ in range(3):
            req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/audio/transcriptions", data=body,
                                         headers={"Content-Type": ctype}, method="POST")
            t1 = time.perf_counter()
            with urllib.request.urlopen(req, timeout=120) as resp:
                text = resp.read().decode()
            timings.append(time.perf_counter() - t1)
        print(f"\nGPU test: model={model} ready={ready:.1f}s audio={seconds:.1f}s "
              f"requests={[round(t, 2) for t in timings]} text={text!r}")
        assert "fox" in text.lower() and "dog" in text.lower()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
