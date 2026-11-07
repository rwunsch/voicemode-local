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
