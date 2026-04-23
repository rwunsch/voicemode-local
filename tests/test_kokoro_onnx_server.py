"""Tests for the Kokoro ONNX server HTTP handler logic."""

import json
import struct
import io
import pytest
import numpy as np


def test_pcm_to_wav_produces_valid_wav():
    """Test that PCM to WAV conversion produces a valid WAV header."""
    # Import from the server module
    import sys
    sys.path.insert(0, ".")
    from importlib import import_module
    server = import_module("kokoro-onnx-server")

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
    import sys
    sys.path.insert(0, ".")
    from importlib import import_module
    server = import_module("kokoro-onnx-server")

    n_samples = 12000
    samples = np.random.randn(n_samples).astype(np.float32)
    wav_bytes = server.pcm_to_wav(samples, sample_rate=24000)

    # data chunk size should be n_samples * 2 (int16)
    data_size = struct.unpack_from("<I", wav_bytes, 40)[0]
    assert data_size == n_samples * 2


def test_voice_map_has_all_default_voices():
    """Test that the voice map includes common Kokoro voices."""
    import sys
    sys.path.insert(0, ".")
    from importlib import import_module
    server = import_module("kokoro-onnx-server")

    expected = ["af_sky", "af_bella", "am_adam", "bf_emma", "bm_daniel", "ff_siwis"]
    for voice in expected:
        assert voice in server.VOICE_MAP, f"Missing voice: {voice}"


def test_voice_map_preserves_names():
    """Voice names should map to themselves (kokoro-onnx uses same names)."""
    import sys
    sys.path.insert(0, ".")
    from importlib import import_module
    server = import_module("kokoro-onnx-server")

    for key, value in server.VOICE_MAP.items():
        assert key == value, f"Voice {key} maps to {value} instead of itself"


def test_default_voice():
    """Default voice should be af_sky."""
    import sys
    sys.path.insert(0, ".")
    from importlib import import_module
    server = import_module("kokoro-onnx-server")

    assert server.DEFAULT_VOICE == "af_sky"
