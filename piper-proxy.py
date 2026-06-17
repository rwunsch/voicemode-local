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
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler


def resolve_piper_bin():
    """Locate the piper executable without relying on the launcher's PATH.

    The proxy is auto-started by voicemode-mcp from Claude Code's MCP
    environment, whose PATH does not include this project's .venv/bin. Trusting
    a bare "piper" on PATH therefore fails with FileNotFoundError. Resolve it
    explicitly, in priority order, falling back to "piper" so the original
    error message still surfaces if nothing is found.
    """
    # 1. Explicit override
    env_bin = os.environ.get("PIPER_BIN")
    if env_bin and os.path.exists(env_bin):
        return env_bin
    # 2. On PATH (works when launched from an activated venv / proper shell)
    found = shutil.which("piper")
    if found:
        return found
    # 3. The venv that sits next to this script (the canonical install spot)
    script_dir = pathlib.Path(__file__).resolve().parent
    cand = script_dir / ".venv" / "bin" / "piper"
    if cand.exists():
        return str(cand)
    # 4. Alongside the python interpreter running us (venv python case)
    cand = pathlib.Path(sys.executable).resolve().parent / "piper"
    if cand.exists():
        return str(cand)
    return "piper"


class PiperProxyHandler(BaseHTTPRequestHandler):

    voices_config = {}
    models_dir = pathlib.Path("models/piper")
    piper_bin = "piper"

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
            # Emit BOTH shapes: "data" for OpenAI-style clients, and "voices"
            # for voice-mode's discovery probe (voice_mode/voices.py only
            # accepts a {"voices": [...]} wrapper or a bare list — an
            # {"object","data"} envelope is rejected as "malformed", which
            # silently hides every Piper voice from the voice://voices
            # resource and makes callers fall back to OpenAI for German).
            self._json_response({"object": "list", "data": voices, "voices": voices})
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
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp_path = tmp.name

            result = subprocess.run(
                [self.piper_bin, "--model", str(model_path), "--output_file", tmp_path],
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
            self.send_error(
                500,
                f"piper CLI not found at '{self.piper_bin}' - install piper-tts "
                "(pip install piper-tts) or set PIPER_BIN",
            )
            return
        except subprocess.TimeoutExpired:
            self.send_error(500, "piper timed out")
            return
        finally:
            if tmp_path and os.path.exists(tmp_path):
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
        # Examples: de_DE-thorsten-high, de_DE-eva_k-x_low, ko_KR-x_medium
        # The name and quality are everything between the first and last hyphen,
        # but some models have underscores in names (eva_k) or single-segment
        # names (x). We split from both ends to handle all cases.
        parts = piper_model.split("-")
        # First part is always lang_REGION
        lang_region = parts[0]             # de_DE
        # Last part is always quality
        quality = parts[-1] if len(parts) > 2 else (parts[1] if len(parts) == 2 else "medium")
        # Middle parts (joined) are the name
        if len(parts) > 2:
            name = "-".join(parts[1:-1])
        elif len(parts) == 2:
            # ambiguous: could be name or quality. Treat as name with default quality.
            name = parts[1]
        else:
            name = "unknown"

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
    PiperProxyHandler.piper_bin = resolve_piper_bin()

    server = HTTPServer(("127.0.0.1", args.port), PiperProxyHandler)
    print(f"[piper-proxy] Listening on http://127.0.0.1:{args.port}")
    print(f"[piper-proxy] Voices file: {voices_path}")
    print(f"[piper-proxy] Models dir:  {models_dir}")
    print(f"[piper-proxy] Piper binary: {PiperProxyHandler.piper_bin}")
    print(f"[piper-proxy] OpenAI endpoint: POST /v1/audio/speech")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[piper-proxy] Shutting down")
        server.server_close()


if __name__ == "__main__":
    main()
