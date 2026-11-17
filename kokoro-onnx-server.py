#!/usr/bin/env python3
"""Lightweight OpenAI-compatible TTS server using kokoro-onnx.

This is a minimal server that wraps the kokoro-onnx library to provide
an OpenAI-compatible /v1/audio/speech endpoint. Much lighter than the
full Kokoro-FastAPI Docker image (~92MB model vs multi-GB container).

Requirements:
    pip install kokoro-onnx soundfile numpy

Usage:
    python3 kokoro-onnx-server.py [--host 0.0.0.0] [--port 8880] [--model-dir ./models/kokoro]
                                  [--no-download] [--require-gpu]

--require-gpu (or env VOICEMODE_KOKORO_REQUIRE_GPU=1): after loading the model, exit 1
unless its onnxruntime session runs on CUDAExecutionProvider first. Without it a GPU
venv whose CUDA provider failed to load silently runs on the CPU. Run the GPU venv
through scripts/kokoro-gpu-start.sh. See docs/native-gpu-tts/README.md.
"""

import argparse
import io
import json
import os
import struct
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlretrieve

MODEL_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx"
VOICES_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin"

# Lazy-loaded globals
_kokoro = None
_model_dir = None
# --no-download: a missing model file is an error, never a download (FELIX installs the
# files itself and runs the server offline).
_no_download = False
# --require-gpu: a session that is not on CUDA ends the server (exit 1), never a silent
# CPU fallback.
_require_gpu = False
GPU_PROVIDER = "CUDAExecutionProvider"
NO_CUDA_CAUSE = ("onnxruntime-gpu loaded no CUDA provider; the default pip build needs CUDA 13: "
                 "install onnxruntime-gpu==1.23.2")

# Requests are served on their own threads, so /health answers while a long synthesis
# runs (2026-11-14: a 54 s reply took 134.7 s and the health checks timed out). One
# ONNX session serves every request, so speech is generated one request at a time.
_LOAD_LOCK = threading.Lock()
_INFERENCE_LOCK = threading.Lock()

# Optional German model: Kokoro-82M fine-tuned on one German speaker ("Martin").
# Upstream Kokoro v1.0 has no German voices, and a voicepack only works with the
# weights it was trained with, so German needs its own model file. Same ONNX
# graph as v1.0, so kokoro-onnx loads it unchanged. See docs/german-kokoro/.
DE_MODEL_URL = ("https://huggingface.co/Godelaune/Kokoro-82M-ONNX-German-Martin"
                "/resolve/main/kokoro-martin.onnx")
DE_VOICES_URL = ("https://huggingface.co/Godelaune/Kokoro-82M-ONNX-German-Martin"
                 "/resolve/main/voices-martin.npz")
DE_VOICE = "dm_martin"
_kokoro_de = None
_de_model_dir = None


def get_model_dir():
    return _model_dir or Path("./models/kokoro")


def missing_model_files():
    """(path, url) of every English model file not present in the model directory."""
    model_dir = get_model_dir()
    return [(model_dir / name, url)
            for name, url in (("kokoro-v1.0.onnx", MODEL_URL), ("voices-v1.0.bin", VOICES_URL))
            if not (model_dir / name).exists()]


def exit_if_missing_without_download():
    """With --no-download, a missing model file ends the server: exit 1, naming the file
    and where to get it, so the caller can fetch it rather than have us download it."""
    missing = missing_model_files()
    if not missing:
        return
    for path, url in missing:
        print(f"Error: Kokoro model file missing: {path}\n"
              f"  --no-download is set, so it is not downloaded. Get it from:\n"
              f"  {url}\n"
              f"  and save it as {path} (or run without --no-download).",
              file=sys.stderr, flush=True)
    sys.exit(1)


def ensure_models():
    """Download model files if not present (with --no-download: exit 1 instead)."""
    if _no_download:
        exit_if_missing_without_download()
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
    with _LOAD_LOCK:
        return _load_kokoro()


def _load_kokoro():
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


def get_kokoro_de():
    """Lazy-load the German model, or None if its files are not downloaded.

    Fetch them with: python3 kokoro-onnx-server.py --download-german
    """
    with _LOAD_LOCK:
        return _load_kokoro_de()


def _load_kokoro_de():
    global _kokoro_de
    if _kokoro_de is None:
        model_dir = _de_model_dir or Path("./models/kokoro-de-martin")
        model_path = model_dir / "kokoro-martin.onnx"
        voices_path = model_dir / "voices-martin.npz"
        if not (model_path.exists() and voices_path.exists()):
            return None
        import kokoro_onnx
        print(f"Loading German Kokoro ONNX model from {model_path}...")
        _kokoro_de = kokoro_onnx.Kokoro(str(model_path), str(voices_path))
        print(f"German model loaded (voices: {_kokoro_de.get_voices()}).")
    return _kokoro_de


def download_german():
    model_dir = _de_model_dir or Path("./models/kokoro-de-martin")
    model_dir.mkdir(parents=True, exist_ok=True)
    for url, name in ((DE_MODEL_URL, "kokoro-martin.onnx"),
                      (DE_VOICES_URL, "voices-martin.npz")):
        path = model_dir / name
        if not path.exists():
            print(f"Downloading {name} to {path}...")
            urlretrieve(url, str(path))


def encode_audio(samples, sample_rate, response_format):
    """(body, content type) for an OpenAI-style response_format. "pcm" is raw 16-bit
    little-endian mono, as voice-mode's streaming player expects (it plays the bytes as
    int16, so a WAV header would be heard). Anything else gets WAV, as before."""
    import numpy as np
    if (response_format or "").lower() == "pcm":
        return (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes(), "audio/pcm"
    return pcm_to_wav(samples, sample_rate=sample_rate), "audio/wav"


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

# A Kokoro voice's language is its first letter (a American, b British English).
LANGUAGE_BY_PREFIX = {
    "a": ("en", "English"), "b": ("en", "English"), "e": ("es", "Spanish"),
    "f": ("fr", "French"), "h": ("hi", "Hindi"), "i": ("it", "Italian"),
    "j": ("ja", "Japanese"), "p": ("pt", "Portuguese"), "z": ("zh", "Chinese"),
    "d": ("de", "German"),
}


# The espeak-ng language a voice is phonemised with, by the voice id's first letter. An
# unknown prefix stays en-us. (d, the German model, passes "de" on its own path.)
ESPEAK_LANG_BY_PREFIX = {
    "a": "en-us", "b": "en-gb", "e": "es", "f": "fr-fr", "h": "hi", "i": "it",
    "j": "ja", "p": "pt-br", "z": "cmn",
}


# Voice quality by espeak language (2026-11-17, round trips: speak a sentence, transcribe it
# back, n=1 per language). This server phonemises every language with espeak-ng only (no
# misaki), which cannot do Japanese or Chinese well. A language not listed here is "good".
QUALITY_NOTES = {
    "ja": "Japanese: audio far too long, nonsense when transcribed (2026-11-17, n=1)",
    "cmn": "Chinese: fragments with wrong words when transcribed (2026-11-17, n=1)",
}


def voice_quality(voice):
    """("good", None) or ("experimental", note) for a voice id, by its espeak language."""
    note = QUALITY_NOTES.get(ESPEAK_LANG_BY_PREFIX.get(voice[:1]))
    return ("experimental", note) if note else ("good", None)


def session_providers(kokoro):
    """The providers of the onnxruntime session kokoro_onnx actually created
    (Kokoro.sess), in priority order; None if the object has no readable session."""
    try:
        return list(kokoro.sess.get_providers())
    except Exception:  # noqa: BLE001 - a fake or an older kokoro_onnx: unknown
        return None


def active_provider(kokoro):
    """The provider the session runs on (the first one), or None if not known."""
    providers = session_providers(kokoro) if kokoro is not None else None
    return providers[0] if providers else None


def exit_unless_on_gpu(kokoro, label="Kokoro"):
    """--require-gpu: exit 1 unless the session's first provider is CUDA. onnxruntime-gpu
    can list CUDAExecutionProvider as available and still fail to load it (1.32 with the
    CUDA 12 pip libraries), and then quietly runs on the CPU."""
    providers = session_providers(kokoro)
    if providers and providers[0] == GPU_PROVIDER:
        return
    if providers is None:
        got = "could not read the session's provider list (kokoro_onnx.Kokoro.sess)"
    else:
        first = providers[0] if providers else "no provider"
        got = f"the {label} session runs on {first} (providers: {providers})"
    try:
        import onnxruntime
        runtime = (f"onnxruntime {getattr(onnxruntime, '__version__', '?')}; available providers: "
                   f"{list(onnxruntime.get_available_providers())}")
    except Exception as error:  # noqa: BLE001 - only a diagnostic line
        runtime = f"onnxruntime not readable ({error})"
    print(f"Error: --require-gpu: {NO_CUDA_CAUSE}.\n"
          f"  {got}.\n"
          f"  {runtime}. (1.23.2 failing as well usually means the CUDA libraries were not on\n"
          f"  LD_LIBRARY_PATH when the server started.)\n"
          f"  Start the server through scripts/kokoro-gpu-start.sh (it puts the venv's nvidia\n"
          f"  libraries on LD_LIBRARY_PATH and sets ONNX_PROVIDER={GPU_PROVIDER}),\n"
          f"  or run without --require-gpu to accept the CPU.",
          file=sys.stderr, flush=True)
    sys.exit(1)


def german_files_exist():
    """The German model's files are downloaded (checked without loading the model)."""
    model_dir = _de_model_dir or Path("./models/kokoro-de-martin")
    return all((model_dir / name).exists() for name in ("kokoro-martin.onnx", "voices-martin.npz"))


def known_voices(kokoro):
    """The voice ids the loaded model has; everything (no check) if it cannot say."""
    try:
        return set(kokoro.get_voices())
    except Exception:  # noqa: BLE001 - an odd model: let kokoro_onnx decide, as before
        class _Everything:
            def __contains__(self, item):
                return True
        return _Everything()


def list_voices():
    """The voices this server speaks, in the Piper proxy's shape, so the voice pill can
    list them by language: every voice of the English model, plus dm_martin when the
    German files exist. A voice with an unknown prefix is left out."""
    ids = list(get_kokoro().get_voices())
    if german_files_exist():
        ids.append(DE_VOICE)
    data = []
    for voice in ids:
        lang = LANGUAGE_BY_PREFIX.get(voice[:1])
        if lang:
            entry = {"id": voice, "language": lang[0], "language_name": lang[1]}
            entry["quality"], note = voice_quality(voice)
            if note:
                entry["quality_note"] = note
            data.append(entry)
    return {"object": "list", "data": data}


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
            # The provider of the model already loaded (never loads it): CUDAExecutionProvider
            # on the GPU, CPUExecutionProvider when it runs on the CPU, null before loading.
            self._send_json(200, {"status": "ok", "engine": "kokoro-onnx",
                                  "provider": active_provider(_kokoro)})
        elif self.path == "/v1/audio/voices":
            try:
                self._send_json(200, list_voices())
            except Exception as e:
                print(f"[kokoro-onnx] Error listing voices: {e}")
                self._send_json(500, {"error": str(e)})
        elif self.path == "/v1/models":
            self._send_json(200, {
                "object": "list",
                "data": [{"id": "kokoro-onnx", "object": "model", "owned_by": "local",
                          "provider": active_provider(_kokoro)}]
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
            if voice_id == DE_VOICE:
                kokoro = get_kokoro_de()
                if kokoro is None:
                    self._send_json(400, {"error": f"{DE_VOICE} needs the German model; "
                                          "run kokoro-onnx-server.py --download-german"})
                    return
                voice_id = kokoro.get_voices()[0]
                lang = "de"
            else:
                kokoro = get_kokoro()
                if voice_id not in known_voices(kokoro):
                    # kokoro_onnx would raise KeyError, answered as a bare 500; voice-mode's
                    # fallback to an installed voice needs to see that the voice is unknown.
                    self._send_json(404, {"error": f"Voice '{voice}' not found"})
                    return
                lang = ESPEAK_LANG_BY_PREFIX.get(voice_id[:1], "en-us")
            start = time.time()
            with _INFERENCE_LOCK:
                waited = time.time() - start
                samples, sample_rate = kokoro.create(text, voice=voice_id, speed=speed, lang=lang)
            elapsed = time.time() - start - waited
            print(f"[kokoro-onnx] Generated {len(samples)/sample_rate:.1f}s audio "
                  f"in {elapsed:.1f}s (voice={voice_id})"
                  + (f", after waiting {waited:.1f}s for another request" if waited >= 0.1 else ""))

            body, content_type = encode_audio(samples, sample_rate, data.get("response_format"))

            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        except Exception as e:
            print(f"[kokoro-onnx] Error: {e}")
            self._send_json(500, {"error": str(e)})


def make_server(host, port):
    """The HTTP server, one thread per request (daemon threads, so a client that left
    cannot keep the process alive at shutdown)."""
    server = ThreadingHTTPServer((host, port), TTSHandler)
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser(description="Kokoro ONNX TTS Server")
    parser.add_argument("--port", type=int, default=8880, help="Port to listen on")
    # 0.0.0.0 by default, as before, so WSL (reached from Windows through localhost
    # forwarding) and Docker keep working. The native Windows launcher passes 127.0.0.1.
    parser.add_argument("--host", default="0.0.0.0", help="Address to bind (default: 0.0.0.0)")
    parser.add_argument("--model-dir", type=str, default="./models/kokoro",
                        help="Directory for model files")
    parser.add_argument("--de-model-dir", type=str, default=None,
                        help="Directory for the German model (default: <model-dir>-de-martin)")
    parser.add_argument("--download-german", action="store_true",
                        help="Download the German model (voice dm_martin) and exit")
    parser.add_argument("--no-download", action="store_true",
                        help="Never download: a missing English model file exits 1, naming it")
    parser.add_argument("--require-gpu", action="store_true",
                        help="Exit 1 unless the model runs on CUDAExecutionProvider "
                             "(also: env VOICEMODE_KOKORO_REQUIRE_GPU=1)")
    args = parser.parse_args()

    global _model_dir, _de_model_dir, _no_download, _require_gpu
    _no_download = args.no_download
    _require_gpu = args.require_gpu or os.environ.get("VOICEMODE_KOKORO_REQUIRE_GPU") == "1"
    _model_dir = Path(args.model_dir)
    _de_model_dir = Path(args.de_model_dir or f"{args.model_dir.rstrip('/')}-de-martin")

    if args.download_german:
        download_german()
        return

    if _no_download:
        # Before the kokoro_onnx import: the missing file is the error worth naming.
        exit_if_missing_without_download()

    # Pre-load models
    kokoro = get_kokoro()
    kokoro_de = get_kokoro_de()
    if _require_gpu:
        exit_unless_on_gpu(kokoro)
        if kokoro_de is not None:
            exit_unless_on_gpu(kokoro_de, "German Kokoro")
    print(f"Kokoro ONNX provider: {active_provider(kokoro) or 'unknown'}", flush=True)

    server = make_server(args.host, args.port)
    print(f"Kokoro ONNX TTS server listening on {args.host}:{args.port}", flush=True)
    print(f"  POST /v1/audio/speech  - Generate speech")
    print(f"  GET  /v1/models        - List models")
    print(f"  GET  /v1/audio/voices  - List voices")
    print(f"  GET  /health           - Health check")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
