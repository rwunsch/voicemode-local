#!/usr/bin/env python3
"""Lightweight OpenAI-compatible TTS server using kokoro-onnx.

This is a minimal server that wraps the kokoro-onnx library to provide
an OpenAI-compatible /v1/audio/speech endpoint. Much lighter than the
full Kokoro-FastAPI Docker image (~92MB model vs multi-GB container).

Requirements:
    pip install kokoro-onnx soundfile numpy

Usage:
    python3 kokoro-onnx-server.py [--port 8880] [--model-dir ./models/kokoro]
"""

import argparse
import io
import json
import os
import struct
import sys
import tempfile
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.request import urlretrieve

MODEL_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx"
VOICES_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin"

# Lazy-loaded globals
_kokoro = None
_model_dir = None


def get_model_dir():
    return _model_dir or Path("./models/kokoro")


def ensure_models():
    """Download model files if not present."""
    model_dir = get_model_dir()
    model_dir.mkdir(parents=True, exist_ok=True)

    model_path = model_dir / "kokoro-v1.0.onnx"
    voices_path = model_dir / "voices-v1.0.bin"

    if not model_path.exists():
        print(f"Downloading Kokoro ONNX model to {model_path}...")
        urlretrieve(MODEL_URL, str(model_path))
        print(f"Downloaded ({model_path.stat().st_size / 1e6:.0f} MB)")

    if not voices_path.exists():
        print(f"Downloading voice data to {voices_path}...")
        urlretrieve(VOICES_URL, str(voices_path))
        print(f"Downloaded ({voices_path.stat().st_size / 1e6:.0f} MB)")

    return model_path, voices_path


def get_kokoro():
    """Lazy-load the kokoro-onnx model."""
    global _kokoro
    if _kokoro is None:
        try:
            import kokoro_onnx
        except ImportError:
            print("Error: kokoro-onnx not installed. Run: pip install kokoro-onnx")
            sys.exit(1)

        model_path, voices_path = ensure_models()
        print(f"Loading Kokoro ONNX model from {model_path}...")
        _kokoro = kokoro_onnx.Kokoro(str(model_path), str(voices_path))
        print("Model loaded.")
    return _kokoro


def pcm_to_wav(pcm_data, sample_rate=24000, channels=1, sample_width=2):
    """Convert raw PCM float32 samples to WAV bytes."""
    import numpy as np
    # Convert float32 to int16
    pcm_int16 = (pcm_data * 32767).astype(np.int16)
    raw_bytes = pcm_int16.tobytes()

    buf = io.BytesIO()
    # WAV header
    data_size = len(raw_bytes)
    buf.write(b"RIFF")
    buf.write(struct.pack("<I", 36 + data_size))
    buf.write(b"WAVE")
    buf.write(b"fmt ")
    buf.write(struct.pack("<I", 16))  # chunk size
    buf.write(struct.pack("<H", 1))   # PCM format
    buf.write(struct.pack("<H", channels))
    buf.write(struct.pack("<I", sample_rate))
    buf.write(struct.pack("<I", sample_rate * channels * sample_width))
    buf.write(struct.pack("<H", channels * sample_width))
    buf.write(struct.pack("<H", sample_width * 8))
    buf.write(b"data")
    buf.write(struct.pack("<I", data_size))
    buf.write(raw_bytes)
    return buf.getvalue()


# Map Kokoro voice names to kokoro-onnx voice IDs
# kokoro-onnx uses the same voice names as Kokoro
VOICE_MAP = {
    "af_sky": "af_sky", "af_bella": "af_bella", "af_heart": "af_heart",
    "af_jessica": "af_jessica", "af_nicole": "af_nicole", "af_nova": "af_nova",
    "af_sarah": "af_sarah", "af_alloy": "af_alloy",
    "am_adam": "am_adam", "am_echo": "am_echo", "am_eric": "am_eric",
    "am_michael": "am_michael", "am_liam": "am_liam", "am_puck": "am_puck",
    "am_fenrir": "am_fenrir",
    "bf_alice": "bf_alice", "bf_emma": "bf_emma", "bf_lily": "bf_lily",
    "bm_daniel": "bm_daniel", "bm_george": "bm_george", "bm_lewis": "bm_lewis",
    "ff_siwis": "ff_siwis",
    "if_sara": "if_sara", "im_nicola": "im_nicola",
    "ef_dora": "ef_dora", "em_alex": "em_alex",
    "hf_alpha": "hf_alpha", "hm_omega": "hm_omega",
    "jf_alpha": "jf_alpha", "jm_kumo": "jm_kumo",
    "pf_dora": "pf_dora", "pm_alex": "pm_alex",
    "zf_xiaobei": "zf_xiaobei", "zm_yunxi": "zm_yunxi",
}

DEFAULT_VOICE = "af_sky"


class TTSHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Suppress default request logging for cleaner output
        pass

    def _send_json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"status": "ok", "engine": "kokoro-onnx"})
        elif self.path == "/v1/models":
            self._send_json(200, {
                "object": "list",
                "data": [{"id": "kokoro-onnx", "object": "model", "owned_by": "local"}]
            })
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/v1/audio/speech":
            self._send_json(404, {"error": "not found"})
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid JSON"})
            return

        text = data.get("input", "")
        voice = data.get("voice", DEFAULT_VOICE)
        speed = float(data.get("speed", 1.0))

        if not text:
            self._send_json(400, {"error": "input text is required"})
            return

        # Resolve voice name
        voice_id = VOICE_MAP.get(voice, voice)

        try:
            import numpy as np
            kokoro = get_kokoro()
            start = time.time()
            samples, sample_rate = kokoro.create(text, voice=voice_id, speed=speed)
            elapsed = time.time() - start
            print(f"[kokoro-onnx] Generated {len(samples)/sample_rate:.1f}s audio "
                  f"in {elapsed:.1f}s (voice={voice_id})")

            wav_bytes = pcm_to_wav(samples, sample_rate=sample_rate)

            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(wav_bytes)))
            self.end_headers()
            self.wfile.write(wav_bytes)

        except Exception as e:
            print(f"[kokoro-onnx] Error: {e}")
            self._send_json(500, {"error": str(e)})


def main():
    parser = argparse.ArgumentParser(description="Kokoro ONNX TTS Server")
    parser.add_argument("--port", type=int, default=8880, help="Port to listen on")
    parser.add_argument("--model-dir", type=str, default="./models/kokoro",
                        help="Directory for model files")
    args = parser.parse_args()

    global _model_dir
    _model_dir = Path(args.model_dir)

    # Pre-load model
    get_kokoro()

    server = HTTPServer(("0.0.0.0", args.port), TTSHandler)
    print(f"Kokoro ONNX TTS server listening on port {args.port}")
    print(f"  POST /v1/audio/speech  - Generate speech")
    print(f"  GET  /v1/models        - List models")
    print(f"  GET  /health           - Health check")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
