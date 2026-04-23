#!/usr/bin/env python3
"""
Piper OpenAI-Compatible TTS Proxy

Exposes OpenAI-compatible TTS endpoints on port 8881, backed by piper-tts.

VoiceMode expects:  POST /v1/audio/speech  (JSON: input, voice, model)
Piper CLI:          piper --model <path> --output_file <tmp.wav>

Usage:
    python3 piper-proxy.py [--port 8881] [--voices-file voices/piper-voices.json] [--models-dir models/piper]
"""

import argparse
import json
import os
import pathlib
import subprocess
import tempfile
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler


class PiperProxyHandler(BaseHTTPRequestHandler):

    voices_config = {}
    models_dir = pathlib.Path("models/piper")

    def do_GET(self):
        if self.path == "/health":
            self._json_response({"status": "ok"})
        elif self.path == "/v1/models":
            self._json_response({
                "object": "list",
                "data": [
                    {
                        "id": "piper",
                        "object": "model",
                        "created": 1700000000,
                        "owned_by": "local",
                    }
                ],
            })
        elif self.path == "/v1/audio/voices":
            voices = self.voices_config.get("voices", [])
            self._json_response({"object": "list", "data": voices})
        else:
            self.send_error(404, "Not Found")

    def do_POST(self):
        if self.path == "/v1/audio/speech":
            self._handle_speech()
        else:
            self.send_error(404, "Not Found")

    def _handle_speech(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON body")
            return

        text = payload.get("input", "")
        voice_id = payload.get("voice", self.voices_config.get("default_voice", ""))

        if not text:
            self.send_error(400, "Missing 'input' field")
            return

        # Find voice entry in config
        voices = self.voices_config.get("voices", [])
        voice_entry = next((v for v in voices if v["id"] == voice_id), None)
        if voice_entry is None:
            self.send_error(404, f"Voice '{voice_id}' not found")
            return

        piper_model = voice_entry["piper_model"]
        model_path = self.models_dir / f"{piper_model}.onnx"

        # Download model if missing
        if not model_path.exists():
            try:
                self._download_model(piper_model, model_path)
            except Exception as e:
                self.send_error(502, f"Failed to download model: {e}")
                return

        # Run piper CLI
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp_path = tmp.name

            result = subprocess.run(
                ["piper", "--model", str(model_path), "--output_file", tmp_path],
                input=text.encode(),
                capture_output=True,
                timeout=60,
            )

            if result.returncode != 0:
                self.send_error(500, f"piper failed: {result.stderr.decode()[:200]}")
                return

            with open(tmp_path, "rb") as f:
                wav_bytes = f.read()
        except FileNotFoundError:
            self.send_error(500, "piper CLI not found — install piper-tts")
            return
        except subprocess.TimeoutExpired:
            self.send_error(500, "piper timed out")
            return
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(wav_bytes)))
        self.end_headers()
        self.wfile.write(wav_bytes)

    def _download_model(self, piper_model, model_path):
        """Download .onnx and .onnx.json from HuggingFace rhasspy/piper-voices.

        Model name format: de_DE-thorsten-high
          lang        = de
          lang_REGION = de_DE
          name        = thorsten
          quality     = high
        """
        # Parse model name: <lang_REGION>-<name>-<quality>
        # lang_REGION may be like de_DE or ko_KR
        parts = piper_model.split("-", 1)  # ["de_DE", "thorsten-high"]
        lang_region = parts[0]             # de_DE
        rest = parts[1] if len(parts) > 1 else ""

        # Split rest into name and quality (quality is last segment)
        rest_parts = rest.rsplit("-", 1)
        name = rest_parts[0] if len(rest_parts) > 1 else rest
        quality = rest_parts[1] if len(rest_parts) > 1 else "medium"

        lang = lang_region.split("_")[0]   # de

        base_url = (
            f"https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0"
            f"/{lang}/{lang_region}/{name}/{quality}/{piper_model}"
        )

        model_path.parent.mkdir(parents=True, exist_ok=True)

        for suffix in [".onnx", ".onnx.json"]:
            url = base_url + suffix
            dest = model_path.parent / (model_path.name.replace(".onnx", suffix) if suffix == ".onnx.json" else model_path.name)
            print(f"[piper-proxy] Downloading {url}")
            urllib.request.urlretrieve(url, dest)

    def _json_response(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        print(f"[piper-proxy] {args[0]}")


def main():
    parser = argparse.ArgumentParser(description="Piper OpenAI-compatible TTS proxy")
    parser.add_argument("--port", type=int, default=8881, help="Port to listen on (default: 8881)")
    parser.add_argument(
        "--voices-file",
        default="voices/piper-voices.json",
        help="Path to voices config JSON (default: voices/piper-voices.json)",
    )
    parser.add_argument(
        "--models-dir",
        default=None,
        help="Directory for piper model files (default: models_dir from voices config or models/piper)",
    )
    args = parser.parse_args()

    voices_path = pathlib.Path(args.voices_file)
    if not voices_path.exists():
        print(f"[piper-proxy] WARNING: voices file not found: {voices_path}")
        voices_config = {}
    else:
        with open(voices_path) as f:
            voices_config = json.load(f)

    # Resolve models_dir: CLI arg > config file > default
    if args.models_dir:
        models_dir = pathlib.Path(args.models_dir)
    else:
        models_dir = pathlib.Path(voices_config.get("models_dir", "models/piper"))

    PiperProxyHandler.voices_config = voices_config
    PiperProxyHandler.models_dir = models_dir

    server = HTTPServer(("127.0.0.1", args.port), PiperProxyHandler)
    print(f"[piper-proxy] Listening on http://127.0.0.1:{args.port}")
    print(f"[piper-proxy] Voices file: {voices_path}")
    print(f"[piper-proxy] Models dir:  {models_dir}")
    print(f"[piper-proxy] OpenAI endpoint: POST /v1/audio/speech")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[piper-proxy] Shutting down")
        server.server_close()


if __name__ == "__main__":
    main()
