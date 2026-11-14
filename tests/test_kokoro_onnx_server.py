"""Tests for the Kokoro ONNX server HTTP handler logic."""

import json
import struct
import io
import pytest
import numpy as np


def test_pcm_to_wav_produces_valid_wav():
    """Test that PCM to WAV conversion produces a valid WAV header."""
    # Import from the server module
    server = _server()

    samples = np.zeros(24000, dtype=np.float32)  # 1 second of silence
    wav_bytes = server.pcm_to_wav(samples, sample_rate=24000)

    # Check WAV header
    assert wav_bytes[:4] == b"RIFF"
    assert wav_bytes[8:12] == b"WAVE"
    assert wav_bytes[12:16] == b"fmt "

    # Parse format chunk
    fmt_size = struct.unpack_from("<I", wav_bytes, 16)[0]
    assert fmt_size == 16  # PCM format
    audio_format = struct.unpack_from("<H", wav_bytes, 20)[0]
    assert audio_format == 1  # PCM
    channels = struct.unpack_from("<H", wav_bytes, 22)[0]
    assert channels == 1
    sample_rate = struct.unpack_from("<I", wav_bytes, 24)[0]
    assert sample_rate == 24000


def test_pcm_to_wav_data_size():
    """Test that the WAV data chunk has correct size."""
    server = _server()

    n_samples = 12000
    samples = np.random.randn(n_samples).astype(np.float32)
    wav_bytes = server.pcm_to_wav(samples, sample_rate=24000)

    # data chunk size should be n_samples * 2 (int16)
    data_size = struct.unpack_from("<I", wav_bytes, 40)[0]
    assert data_size == n_samples * 2


def test_voice_map_has_all_default_voices():
    """Test that the voice map includes common Kokoro voices."""
    server = _server()

    expected = ["af_sky", "af_bella", "am_adam", "bf_emma", "bm_daniel", "ff_siwis"]
    for voice in expected:
        assert voice in server.VOICE_MAP, f"Missing voice: {voice}"


def test_voice_map_preserves_names():
    """Voice names should map to themselves (kokoro-onnx uses same names)."""
    server = _server()

    for key, value in server.VOICE_MAP.items():
        assert key == value, f"Voice {key} maps to {value} instead of itself"


def test_default_voice():
    """Default voice should be af_sky."""
    server = _server()

    assert server.DEFAULT_VOICE == "af_sky"


def _server():
    """The server next to these tests, by path: importing "kokoro-onnx-server" from "."
    loaded whatever checkout pytest was started in."""
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parent.parent / "kokoro-onnx-server.py"
    spec = importlib.util.spec_from_file_location("kokoro_onnx_server_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_pcm_request_gets_raw_samples_without_a_wav_header():
    """voice-mode's streaming path asks for response_format=pcm (streaming.py:960) and
    plays the bytes as int16. A WAV reply put its 44-byte RIFF header into the audio
    (reported by Stephan's team's benchmark; confirmed by reading, 2026-11-07)."""
    samples = np.full(480, 0.5, dtype=np.float32)
    body, ctype = _server().encode_audio(samples, 24000, "pcm")
    assert ctype == "audio/pcm"
    assert not body.startswith(b"RIFF")
    assert len(body) == 2 * len(samples)
    assert struct.unpack_from("<h", body, 0)[0] == int(0.5 * 32767)


@pytest.mark.parametrize("fmt", ["wav", None, "", "mp3", "opus"])
def test_other_formats_still_get_wav(fmt):
    body, ctype = _server().encode_audio(np.zeros(10, dtype=np.float32), 24000, fmt)
    assert ctype == "audio/wav" and body[:4] == b"RIFF"


# --- Concurrency (2026-11-14) ---------------------------------------------------------
# On a Windows laptop one 54 s reply took 134.7 s to generate. The server was a plain
# HTTPServer, so while it generated, its health checks timed out and voice-mode's second
# request queued behind the first. A health check must answer during a synthesis, and two
# syntheses must still run one at a time (one ONNX session, used under a lock).

import threading
import time
import urllib.request


class _SlowKokoro:
    """Stands in for kokoro_onnx.Kokoro: create() blocks until released, and records
    how many calls ran at the same time."""

    def __init__(self):
        self.release = threading.Event()
        self.started = threading.Event()
        self._lock = threading.Lock()
        self.running = 0
        self.most_at_once = 0
        self.calls = 0

    def create(self, text, voice, speed, lang):
        with self._lock:
            self.running += 1
            self.calls += 1
            self.most_at_once = max(self.most_at_once, self.running)
        self.started.set()
        try:
            self.release.wait(10)
            return np.zeros(240, dtype=np.float32), 24000
        finally:
            with self._lock:
                self.running -= 1


@pytest.fixture
def slow_server(monkeypatch):
    server = _server()
    fake = _SlowKokoro()
    monkeypatch.setattr(server, "get_kokoro", lambda: fake)
    httpd = server.make_server("127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", fake
    finally:
        fake.release.set()
        httpd.shutdown()
        httpd.server_close()


def _speak(base, results, key):
    body = json.dumps({"input": "hello", "voice": "af_sky", "response_format": "pcm"}).encode()
    req = urllib.request.Request(f"{base}/v1/audio/speech", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        results[key] = resp.status


def test_health_answers_while_a_synthesis_runs(slow_server):
    base, fake = slow_server
    results = {}
    speaker = threading.Thread(target=_speak, args=(base, results, "speech"), daemon=True)
    speaker.start()
    assert fake.started.wait(5), "the synthesis never started"
    start = time.monotonic()
    with urllib.request.urlopen(f"{base}/health", timeout=3) as resp:
        assert json.load(resp)["status"] == "ok"
    assert time.monotonic() - start < 2
    assert fake.running == 1, "the synthesis should still be running"
    fake.release.set()
    speaker.join(10)
    assert results == {"speech": 200}


def test_two_syntheses_run_one_at_a_time(slow_server):
    base, fake = slow_server
    results = {}
    speakers = [threading.Thread(target=_speak, args=(base, results, i), daemon=True) for i in (1, 2)]
    for t in speakers:
        t.start()
    assert fake.started.wait(5)
    time.sleep(0.5)  # time for the second request to reach the server
    assert fake.most_at_once == 1
    fake.release.set()
    for t in speakers:
        t.join(10)
    assert results == {1: 200, 2: 200}
    assert fake.calls == 2 and fake.most_at_once == 1
