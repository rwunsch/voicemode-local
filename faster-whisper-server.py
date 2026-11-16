#!/usr/bin/env python3
"""
faster-whisper server: native speech recognition, OpenAI-compatible.

Runs faster-whisper (CTranslate2) in-process, on the NVIDIA GPU when there is one,
with no Docker and no proxy. voice-mode talks to it exactly as it talks to
whisper-proxy.py:

    POST /v1/audio/transcriptions   multipart: file, model, language, prompt,
                                    response_format (json | text | verbose_json)
    GET  /health                    200 only once the model is loaded AND a warm-up
                                    transcription succeeded; 503 before (and after a
                                    failed load), so a dead backend never reads as up
    GET  /v1/models                 minimal list (503 until ready, like /health)

Start it through scripts/faster-whisper-start.sh, not directly: the CUDA libraries
come from the venv's nvidia-cublas-cu12 / nvidia-cudnn-cu12 packages, and
ctranslate2 dlopens them at import, so LD_LIBRARY_PATH must be set before the
process starts. See docs/native-gpu-stt/README.md.

Audio: WAV is decoded with the stdlib `wave` module + numpy (resampled to 16 kHz
mono float32). faster_whisper.decode_audio is not used: PyAV 19 breaks it
("open() got an unexpected keyword argument 'metadata_errors'"). voice-mode sends
WAV when its first STT endpoint is local, but mp3 when a remote one comes first
(hybrid mode: OpenAI, then this server). Anything that is not WAV goes through an
`ffmpeg` binary on PATH (`ffmpeg -i pipe:0 -f s16le -ac 1 -ar 16000 pipe:1`); with
no ffmpeg it gets a 415 naming the format, and an ffmpeg failure gets a 422.

Usage:
    scripts/faster-whisper-start.sh <venv> [--host 127.0.0.1] [--port 2022] [--model small]
        [--device cuda|cpu|auto] [--compute-type float16|int8|...] [--model-dir DIR]
"""

import argparse
import io
import itertools
import json
import re
import shutil
import subprocess
import sys
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

SAMPLE_RATE = 16000
RESPONSE_FORMATS = ("json", "text", "verbose_json")
FFMPEG_TIMEOUT_S = 30


def log(message):
    # Flushed: a detached server's block-buffered stdout looked like a silent component
    # for hours once (docs: the "proxy logs never flushed" lesson).
    print(f"[faster-whisper] {time.strftime('%H:%M:%S')} {message}", flush=True)


# --- audio ---------------------------------------------------------------------------

class UnsupportedAudio(ValueError):
    """The upload is not a WAV this server can decode. The message names what it is."""


class AudioDecodeError(ValueError):
    """ffmpeg was there but could not turn the upload into audio."""


def sniff_format(data):
    """A best guess at the container of an upload, by its magic bytes."""
    head = data[:16]
    if head[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if head[:3] == b"ID3" or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
        return "mp3"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:4] == b"fLaC":
        return "flac"
    if data[4:8] == b"ftyp":
        return "m4a"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "webm"
    return "unknown"


def decode_wav(data):
    """WAV bytes -> 16 kHz mono float32 in [-1, 1]. Raises UnsupportedAudio otherwise."""
    kind = sniff_format(data)
    if kind != "wav":
        what = "an unrecognised format" if kind == "unknown" else f"{kind} audio"
        raise UnsupportedAudio(f"this server decodes WAV only; the upload is {what}")
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            channels, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
            raw = w.readframes(w.getnframes())
    except (wave.Error, EOFError) as e:
        raise UnsupportedAudio(f"this server decodes PCM WAV only; the WAV could not be read: {e}")

    if width == 1:
        audio = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 2:
        audio = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        ints = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        ints = np.where(ints & 0x800000, ints - 0x1000000, ints)
        audio = ints.astype(np.float32) / 8388608.0
    elif width == 4:
        audio = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise UnsupportedAudio(f"this server decodes 8/16/24/32-bit PCM WAV; the WAV has {width * 8}-bit samples")

    if channels > 1:
        audio = audio[: len(audio) - len(audio) % channels].reshape(-1, channels).mean(axis=1)
    if rate != SAMPLE_RATE and len(audio):
        n_out = int(round(len(audio) * SAMPLE_RATE / rate))
        x_old = np.arange(len(audio), dtype=np.float64) / rate
        x_new = np.arange(n_out, dtype=np.float64) / SAMPLE_RATE
        audio = np.interp(x_new, x_old, audio)
    return np.ascontiguousarray(audio, dtype=np.float32)


def decode_with_ffmpeg(data, ffmpeg, kind):
    """Any container ffmpeg reads -> 16 kHz mono float32. No shell; bounded in time."""
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
           "-i", "pipe:0", "-f", "s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), "pipe:1"]
    try:
        done = subprocess.run(cmd, input=data, capture_output=True, timeout=FFMPEG_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise AudioDecodeError(f"ffmpeg timed out after {FFMPEG_TIMEOUT_S}s decoding the {kind} upload")
    except OSError as e:
        raise AudioDecodeError(f"ffmpeg could not be run to decode the {kind} upload: {e}")
    if done.returncode != 0:
        why = (done.stderr or b"").decode(errors="replace").strip().splitlines()
        raise AudioDecodeError(f"ffmpeg could not decode the {kind} upload (exit {done.returncode}): "
                               f"{why[-1] if why else 'no message'}")
    pcm = done.stdout[: len(done.stdout) - len(done.stdout) % 2]
    if not pcm:
        raise AudioDecodeError(f"ffmpeg produced no audio from the {kind} upload")
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0


def decode_audio(data):
    """Any upload -> 16 kHz mono float32. WAV in-process; the rest through ffmpeg if it
    is on PATH. Raises UnsupportedAudio (no ffmpeg) or AudioDecodeError (ffmpeg failed)."""
    kind = sniff_format(data)
    if kind == "wav":
        try:
            return decode_wav(data)
        except UnsupportedAudio:
            if not shutil.which("ffmpeg"):
                raise                     # e.g. a float WAV, and nothing else can read it
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return decode_wav(data)           # raises UnsupportedAudio naming the format
    return decode_with_ffmpeg(data, ffmpeg, "unrecognised" if kind == "unknown" else kind)


# --- the model -----------------------------------------------------------------------

def default_compute_type(device):
    return "float16" if device == "cuda" else "int8"


def resolve_device(device):
    if device != "auto":
        return device
    try:
        import ctranslate2
        return "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
    except Exception:
        return "cpu"


def faster_whisper_loader(model, device, compute_type, model_dir=None):
    """The real loader. faster_whisper is imported here, not at module top, so the
    server (and its tests) import without it."""
    def load():
        from faster_whisper import WhisperModel
        return WhisperModel(model, device=device, compute_type=compute_type, download_root=model_dir)
    return load


class Transcriber:
    """Owns the model. One transcription at a time (the GPU is shared): requests that
    arrive while one runs wait for the lock, they are not rejected."""

    def __init__(self, loader, name="small", device="cpu"):
        self._loader = loader
        self.name, self.device = name, device
        self._model = None
        self._lock = threading.Lock()
        self.ready = False
        self.error = None

    def load(self):
        """Load the model and run a warm-up transcription; only then is it ready."""
        t0 = time.perf_counter()
        try:
            model = self._loader()
            self._model = model
            text, _, _ = self._run(np.zeros(SAMPLE_RATE, dtype=np.float32), None, None)
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            self.ready = False
            log(f"model {self.name} on {self.device} FAILED to load: {self.error}")
            raise
        self.ready = True
        self.error = None
        log(f"model {self.name} on {self.device} ready in {time.perf_counter() - t0:.1f}s (warm-up done)")

    def _run(self, audio, language, prompt):
        kwargs = {"language": language, "beam_size": 5}
        if prompt:
            kwargs["initial_prompt"] = prompt
        with self._lock:
            segments, info = self._model.transcribe(audio, **kwargs)
            segments = list(segments)     # lazy generator: the work happens here, under the lock
        text = "".join(s.text for s in segments).strip()
        return text, segments, info

    def transcribe(self, audio, language=None, prompt=None):
        return self._run(audio, language, prompt)


# --- HTTP ----------------------------------------------------------------------------

def parse_multipart(body, boundary):
    """(file_bytes or None, filename, {field: value}) from a multipart/form-data body."""
    file_data, file_name, fields = None, "audio.wav", {}
    for part in body.split(b"--" + boundary):
        header_end = part.find(b"\r\n\r\n")
        if header_end == -1 or b"Content-Disposition" not in part[:header_end]:
            continue
        header = part[:header_end].decode(errors="replace")
        data = part[header_end + 4:]
        if data.endswith(b"\r\n"):
            data = data[:-2]
        name = re.search(r'name="([^"]+)"', header)
        if not name:
            continue
        if name.group(1) == "file":
            file_data = data
            fn = re.search(r'filename="([^"]*)"', header)
            if fn:
                file_name = fn.group(1)
        else:
            fields[name.group(1)] = data.decode(errors="replace")
    return file_data, file_name, fields


class Handler(BaseHTTPRequestHandler):
    transcriber = None      # set by make_server
    _seq = itertools.count(1)

    def log_message(self, fmt, *args):
        pass                # one line per transcription instead, below

    def _send(self, code, body, content_type):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj), "application/json")

    def _error(self, code, message, kind="invalid_request_error"):
        self._json(code, {"error": {"message": message, "type": kind}})

    def _not_ready(self):
        t = self.transcriber
        self._json(503, {"status": "error" if t.error else "loading", "error": t.error,
                         "model": t.name, "device": t.device})

    def do_GET(self):
        t = self.transcriber
        if self.path == "/health":
            if not t.ready:
                return self._not_ready()
            self._json(200, {"status": "ok", "engine": "faster-whisper", "model": t.name, "device": t.device})
        elif self.path == "/v1/models":
            if not t.ready:
                return self._not_ready()
            self._json(200, {"object": "list", "data": [
                {"id": "whisper-1", "object": "model", "created": 1700000000, "owned_by": "local"},
                {"id": t.name, "object": "model", "created": 1700000000, "owned_by": "local"},
            ]})
        else:
            self._error(404, "not found")

    def do_POST(self):
        if self.path != "/v1/audio/transcriptions":
            return self._error(404, "not found")
        rid = next(self._seq)
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length)
        t = self.transcriber
        if not t.ready:
            return self._not_ready()

        boundary = re.search(r"boundary=([^;]+)", self.headers.get("Content-Type", ""))
        if not boundary:
            return self._error(400, "expected multipart/form-data with a boundary")
        file_data, file_name, fields = parse_multipart(body, boundary.group(1).strip().strip('"').encode())
        if not file_data:
            return self._error(400, "no audio file in the request (multipart field 'file')")

        response_format = fields.get("response_format", "json") or "json"
        if response_format not in RESPONSE_FORMATS:
            return self._error(400, f"response_format {response_format!r} is not supported; "
                                    f"use one of {', '.join(RESPONSE_FORMATS)}")
        language = (fields.get("language") or "").strip() or None
        if language == "auto":
            language = None
        prompt = fields.get("prompt") or None

        try:
            audio = decode_audio(file_data)
        except UnsupportedAudio as e:
            log(f"#{rid} 415 {file_name}: {e}")
            return self._error(415, f"{e}; install ffmpeg to accept it (file {file_name!r})",
                               "unsupported_media_type")
        except AudioDecodeError as e:
            log(f"#{rid} 422 {file_name}: {e}")
            return self._error(422, f"{e} (file {file_name!r})")

        audio_s = len(audio) / SAMPLE_RATE
        t0 = time.perf_counter()
        try:
            text, segments, info = t.transcribe(audio, language=language, prompt=prompt)
        except Exception as e:
            log(f"#{rid} FAILED audio={audio_s:.2f}s after {time.perf_counter() - t0:.2f}s: {type(e).__name__}: {e}")
            return self._error(500, f"transcription failed: {type(e).__name__}: {e}", "server_error")
        took = time.perf_counter() - t0
        log(f"#{rid} audio={audio_s:.2f}s transcribe={took:.2f}s lang={getattr(info, 'language', '?')} "
            f"chars={len(text)}")

        if response_format == "text":
            return self._send(200, text, "text/plain; charset=utf-8")
        if response_format == "json":
            return self._json(200, {"text": text})
        self._json(200, {
            "task": "transcribe",
            "language": getattr(info, "language", language),
            "duration": float(getattr(info, "duration", audio_s)),
            "text": text,
            "segments": [{
                "id": getattr(s, "id", i), "seek": getattr(s, "seek", 0),
                "start": float(s.start), "end": float(s.end), "text": s.text,
                "tokens": list(getattr(s, "tokens", []) or []),
                "temperature": float(getattr(s, "temperature", 0.0) or 0.0),
                "avg_logprob": float(getattr(s, "avg_logprob", 0.0) or 0.0),
                "compression_ratio": float(getattr(s, "compression_ratio", 0.0) or 0.0),
                "no_speech_prob": float(getattr(s, "no_speech_prob", 0.0) or 0.0),
            } for i, s in enumerate(segments)],
        })


def make_server(host, port, transcriber):
    """One thread per request (daemon threads); the Transcriber's lock serializes the GPU."""
    handler = type("BoundHandler", (Handler,), {"transcriber": transcriber})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def build_parser():
    parser = argparse.ArgumentParser(description="faster-whisper OpenAI-compatible STT server")
    # 127.0.0.1 by default. voicemode-switch passes FASTER_WHISPER_HOST, whose default
    # (0.0.0.0) is what whisper-proxy.py binds, so Windows keeps reaching WSL's 2022
    # through localhost forwarding when this server replaces the proxy.
    parser.add_argument("--host", default="127.0.0.1", help="Address to bind (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=2022, help="Port to listen on (default: 2022)")
    parser.add_argument("--model", default="small", help="Whisper model name or path (default: small)")
    parser.add_argument("--device", choices=("cuda", "cpu", "auto"), default="auto",
                        help="cuda, cpu, or auto (cuda when a GPU is visible; default: auto)")
    parser.add_argument("--compute-type", default=None,
                        help="CTranslate2 compute type (default: float16 on cuda, int8 on cpu)")
    parser.add_argument("--model-dir", default=None,
                        help="Download/cache dir for the model (default: the Hugging Face cache)")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    device = resolve_device(args.device)
    compute_type = args.compute_type or default_compute_type(device)
    transcriber = Transcriber(faster_whisper_loader(args.model, device, compute_type, args.model_dir),
                              name=args.model, device=device)

    # Listen first, so /health can answer 503 while the model loads (small: ~110 s
    # the first time, when it downloads).
    server = make_server(args.host, args.port, transcriber)
    log(f"listening on http://{args.host}:{args.port} (model {args.model}, {device}, {compute_type}); loading...")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        transcriber.load()
    except Exception:
        server.shutdown()
        server.server_close()
        return 1
    try:
        thread.join()
    except KeyboardInterrupt:
        log("shutting down")
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
