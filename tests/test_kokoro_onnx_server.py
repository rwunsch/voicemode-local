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


# --- Voice list (2026-11-16) ----------------------------------------------------------
# The pill lists every local voice by language. FELIX runs only this server, so it must
# answer GET /v1/audio/voices in the Piper proxy's shape: {"object": "list", "data":
# [{"id", "language", "language_name"}]}. dm_martin only when the German files exist,
# and the German model is never loaded just to list it.

class _ListingKokoro:
    def get_voices(self):
        return ["af_sky", "bm_george", "ef_dora", "zf_xiaobei", "jm_kumo"]


@pytest.fixture
def listing_server(monkeypatch, tmp_path):
    server = _server()
    monkeypatch.setattr(server, "get_kokoro", lambda: _ListingKokoro())

    def never():
        raise AssertionError("the German model must not be loaded to list it")
    monkeypatch.setattr(server, "get_kokoro_de", never)
    monkeypatch.setattr(server, "_load_kokoro_de", never)
    monkeypatch.setattr(server, "_de_model_dir", tmp_path / "de")
    httpd = server.make_server("127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", tmp_path / "de"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _voices(base):
    with urllib.request.urlopen(f"{base}/v1/audio/voices", timeout=5) as resp:
        assert resp.status == 200
        return json.load(resp)


def test_voices_lists_the_english_model_with_languages(listing_server):
    base, _ = listing_server
    body = _voices(base)
    assert body["object"] == "list"
    server = _server()
    assert body["data"] == [
        {"id": "af_sky", "language": "en", "language_name": "English", "quality": "good"},
        {"id": "bm_george", "language": "en", "language_name": "English", "quality": "good"},
        {"id": "ef_dora", "language": "es", "language_name": "Spanish", "quality": "good"},
        {"id": "zf_xiaobei", "language": "zh", "language_name": "Chinese",
         "quality": "experimental", "quality_note": server.QUALITY_NOTES["cmn"]},
        {"id": "jm_kumo", "language": "ja", "language_name": "Japanese",
         "quality": "experimental", "quality_note": server.QUALITY_NOTES["ja"]},
    ]


def test_voices_marks_ja_and_zh_experimental_with_a_dated_note(listing_server):
    server = _server()
    for key in ("ja", "cmn"):
        note = server.QUALITY_NOTES[key]
        assert "2026-11-17" in note and len(note) < 120
    base, _ = listing_server
    by_id = {v["id"]: v for v in _voices(base)["data"]}
    assert {i for i, v in by_id.items() if v["quality"] == "experimental"} == {"zf_xiaobei", "jm_kumo"}
    assert all("quality_note" not in v for v in by_id.values() if v["quality"] == "good")


def test_voices_lists_dm_martin_only_when_the_german_files_exist(listing_server):
    base, de = listing_server
    assert "dm_martin" not in [v["id"] for v in _voices(base)["data"]]
    de.mkdir()
    (de / "kokoro-martin.onnx").write_text("")
    assert "dm_martin" not in [v["id"] for v in _voices(base)["data"]]   # one file is not enough
    (de / "voices-martin.npz").write_text("")
    data = _voices(base)["data"]
    assert data[-1] == {"id": "dm_martin", "language": "de", "language_name": "German",
                        "quality": "good"}


# --- An unknown voice is a 404 that names it (2026-11-17) --------------------------------
# kokoro_onnx looks a voice up with voices[name], so an id this model lacks raised KeyError
# and the server answered 500 {"error": "'xx'"}: FELIX's first reply failed with HTTP 500
# (Stephan, 2026-11-16). voice-mode's fallback recognises "Voice 'x' not found".

class _VoicedKokoro(_ListingKokoro):
    def __init__(self):
        self.created = []

    def create(self, text, voice, speed, lang):
        self.created.append(voice)
        return np.zeros(240, dtype=np.float32), 24000


@pytest.fixture
def voiced_server(monkeypatch):
    server = _server()
    fake = _VoicedKokoro()
    monkeypatch.setattr(server, "get_kokoro", lambda: fake)
    httpd = server.make_server("127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", fake
    finally:
        httpd.shutdown()
        httpd.server_close()


def _post_speech(base, voice):
    body = json.dumps({"input": "hello", "voice": voice, "response_format": "pcm"}).encode()
    req = urllib.request.Request(f"{base}/v1/audio/speech", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def test_an_unknown_voice_is_a_404_naming_it(voiced_server):
    import urllib.error  # noqa: F401
    base, fake = voiced_server
    code, body = _post_speech(base, "xf_nobody")
    assert code == 404
    assert json.loads(body) == {"error": "Voice 'xf_nobody' not found"}
    assert fake.created == []


def test_a_known_voice_still_speaks(voiced_server):
    base, fake = voiced_server
    code, _ = _post_speech(base, "bm_george")
    assert code == 200 and fake.created == ["bm_george"]


# --- --no-download (FELIX runs the server against files it installed itself) ---------

def _run_no_download(tmp_path, *present):
    import subprocess
    import sys
    from pathlib import Path
    for name in present:
        (tmp_path / name).write_bytes(b"x")
    script = Path(__file__).resolve().parent.parent / "kokoro-onnx-server.py"
    return subprocess.run(
        [sys.executable, str(script), "--host", "127.0.0.1", "--port", "0",
         "--model-dir", str(tmp_path), "--no-download"],
        capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("present,missing", [
    ((), "kokoro-v1.0.onnx"),
    (("voices-v1.0.bin",), "kokoro-v1.0.onnx"),
    (("kokoro-v1.0.onnx",), "voices-v1.0.bin"),
])
def test_no_download_exits_1_naming_the_missing_file(tmp_path, present, missing):
    result = _run_no_download(tmp_path, *present)
    assert result.returncode == 1, result.stderr
    err = result.stderr
    assert str(tmp_path / missing) in err
    assert "--no-download" in err
    # How to get it: the release URL of that file.
    server = _server()
    url = server.MODEL_URL if missing == "kokoro-v1.0.onnx" else server.VOICES_URL
    assert url in err
    # Nothing was fetched or created.
    assert not (tmp_path / missing).exists()


def test_no_download_never_calls_urlretrieve(tmp_path, monkeypatch):
    server = _server()
    calls = []
    monkeypatch.setattr(server, "urlretrieve", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(server, "_model_dir", tmp_path)
    monkeypatch.setattr(server, "_no_download", True)
    with pytest.raises(SystemExit) as exc:
        server.ensure_models()
    assert exc.value.code == 1
    assert calls == []


def test_without_no_download_missing_files_are_still_downloaded(tmp_path, monkeypatch):
    server = _server()
    calls = []

    def fake(url, path):
        calls.append(url)
        with open(path, "wb") as fh:
            fh.write(b"x")
    monkeypatch.setattr(server, "urlretrieve", fake)
    monkeypatch.setattr(server, "_model_dir", tmp_path)
    server.ensure_models()
    assert calls == [server.MODEL_URL, server.VOICES_URL]


# --- The espeak language follows the voice's prefix (Talos #1840, 2026-11-17) -------------
# Every non-German voice used to be phonemised as lang="en-us", so ff_siwis read French
# text with English rules.

class _LangKokoro(_VoicedKokoro):
    def __init__(self):
        super().__init__()
        self.langs = []

    def create(self, text, voice, speed, lang):
        self.langs.append(lang)
        return np.zeros(240, dtype=np.float32), 24000


@pytest.mark.parametrize("voice,lang", [
    ("af_sky", "en-us"), ("am_adam", "en-us"), ("bf_emma", "en-gb"), ("bm_george", "en-gb"),
    ("ef_dora", "es"), ("ff_siwis", "fr-fr"), ("hf_alpha", "hi"), ("if_sara", "it"),
    ("jf_alpha", "ja"), ("pf_dora", "pt-br"), ("zf_xiaobei", "cmn"),
    ("xf_nobody", "en-us"),
])
def test_the_espeak_language_follows_the_voice_prefix(monkeypatch, voice, lang):
    server = _server()
    fake = _LangKokoro()
    monkeypatch.setattr(server, "get_kokoro", lambda: fake)
    monkeypatch.setattr(server, "known_voices", lambda k: {voice})
    httpd = server.make_server("127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        code, _ = _post_speech(f"http://127.0.0.1:{httpd.server_address[1]}", voice)
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert code == 200 and fake.langs == [lang]
