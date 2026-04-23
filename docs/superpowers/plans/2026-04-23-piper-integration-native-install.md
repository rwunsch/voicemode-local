# Piper TTS Integration & Native Install — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Piper TTS as a second local voice engine with curated multilingual voices, add native (non-Docker) install mode, and enable in-session mode switching via an MCP tool.

**Architecture:** A new `piper-proxy.py` exposes an OpenAI-compatible TTS API on port 8881, calling `piper-tts` for synthesis. `voicemode-switch` gains a `piper` mode and install-mode-aware service management. A patched MCP tool enables mode switching from within Claude Code. Voice routing is prefix-based in the converse prompt.

**Tech Stack:** Python 3.10+ (stdlib `http.server`), `piper-tts` library, Bash (install.sh, voicemode-switch)

**Spec:** `docs/superpowers/specs/2026-04-23-piper-integration-native-install-design.md`

---

## File Structure

| File | Responsibility |
|------|---------------|
| `piper-proxy.py` | **New.** OpenAI-compatible TTS proxy for Piper on port 8881 |
| `voices/piper-voices.json` | **New.** Curated voice catalog with model names and metadata |
| `voicemode-switch` | **Modify.** Add `piper` mode, Piper service management, install-mode awareness |
| `install.sh` | **Modify.** Add install mode prompt, native install path, Piper setup |
| `docker-compose.yml` | **Modify.** Add optional Piper container profile |
| `patches/converse.py` | **Modify.** Add Piper voices, language fallback, voice routing |
| `patches/switch_mode.py` | **New.** MCP tool for in-session mode switching |
| `patches/apply.sh` | **Modify.** Also patch switch_mode.py into tools directory |
| `CLAUDE.md` | **Modify.** Update voice lists and service table |
| `README.md` | **Modify.** Update architecture, add Piper section |
| `tests/test_piper_proxy.py` | **New.** Tests for piper-proxy endpoints |
| (voicemode-switch tested manually in Task 3) | Bash scripts tested via CLI, not pytest |

---

## Task 1: Curated Piper Voice Catalog

**Files:**
- Create: `voices/piper-voices.json`

- [ ] **Step 1: Create the voice catalog file**

```json
{
  "voices": [
    {
      "id": "p_de_thorsten",
      "language": "de",
      "language_name": "German",
      "gender": "male",
      "name": "Thorsten",
      "piper_model": "de_DE-thorsten-high",
      "quality": "high",
      "sample_rate": 22050
    },
    {
      "id": "p_de_eva",
      "language": "de",
      "language_name": "German",
      "gender": "female",
      "name": "Eva",
      "piper_model": "de_DE-eva_k-x_low",
      "quality": "medium",
      "sample_rate": 16000
    },
    {
      "id": "p_nl_nathalie",
      "language": "nl",
      "language_name": "Dutch",
      "gender": "female",
      "name": "Nathalie",
      "piper_model": "nl_NL-MLS_7432-low",
      "quality": "medium",
      "sample_rate": 16000
    },
    {
      "id": "p_pl_gosia",
      "language": "pl",
      "language_name": "Polish",
      "gender": "female",
      "name": "Gosia",
      "piper_model": "pl_PL-gosia-medium",
      "quality": "medium",
      "sample_rate": 22050
    },
    {
      "id": "p_ru_dmitri",
      "language": "ru",
      "language_name": "Russian",
      "gender": "male",
      "name": "Dmitri",
      "piper_model": "ru_RU-dmitri-medium",
      "quality": "medium",
      "sample_rate": 22050
    },
    {
      "id": "p_ko_hana",
      "language": "ko",
      "language_name": "Korean",
      "gender": "female",
      "name": "Hana",
      "piper_model": "ko_KR-x_medium",
      "quality": "medium",
      "sample_rate": 22050
    }
  ],
  "default_voice": "p_de_thorsten",
  "models_dir": "models/piper"
}
```

Write this to `voices/piper-voices.json`.

- [ ] **Step 2: Commit**

```bash
git add voices/piper-voices.json
git commit -m "feat: add curated Piper voice catalog"
```

---

## Task 2: Piper Proxy — Core Server

**Files:**
- Create: `piper-proxy.py`
- Create: `tests/test_piper_proxy.py`

- [ ] **Step 1: Write test for health endpoint**

Create `tests/test_piper_proxy.py`:

```python
"""Tests for piper-proxy.py OpenAI-compatible TTS proxy."""

import json
import subprocess
import sys
import time
import urllib.request
import urllib.error
import os
import signal

PROXY_PORT = 18881  # Non-standard port for testing
PROXY_URL = f"http://127.0.0.1:{PROXY_PORT}"
VOICES_FILE = os.path.join(os.path.dirname(__file__), "..", "voices", "piper-voices.json")


def _start_proxy():
    """Start piper-proxy on test port, return process."""
    proc = subprocess.Popen(
        [sys.executable, "piper-proxy.py", "--port", str(PROXY_PORT),
         "--voices-file", VOICES_FILE],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    # Wait for it to start
    for _ in range(20):
        try:
            urllib.request.urlopen(f"{PROXY_URL}/health", timeout=1)
            return proc
        except (urllib.error.URLError, ConnectionRefusedError):
            time.sleep(0.2)
    proc.kill()
    raise RuntimeError("Proxy did not start")


def _stop_proxy(proc):
    """Stop the proxy process."""
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=5)


def test_health_endpoint():
    proc = _start_proxy()
    try:
        resp = urllib.request.urlopen(f"{PROXY_URL}/health")
        data = json.loads(resp.read())
        assert data["status"] == "ok"
    finally:
        _stop_proxy(proc)


def test_models_endpoint():
    proc = _start_proxy()
    try:
        resp = urllib.request.urlopen(f"{PROXY_URL}/v1/models")
        data = json.loads(resp.read())
        assert data["object"] == "list"
        assert any(m["id"] == "piper" for m in data["data"])
    finally:
        _stop_proxy(proc)


def test_voices_endpoint():
    proc = _start_proxy()
    try:
        resp = urllib.request.urlopen(f"{PROXY_URL}/v1/audio/voices")
        data = json.loads(resp.read())
        assert "voices" in data
        voice_ids = [v["id"] for v in data["voices"]]
        assert "p_de_thorsten" in voice_ids
    finally:
        _stop_proxy(proc)


def test_404_for_unknown_path():
    proc = _start_proxy()
    try:
        try:
            urllib.request.urlopen(f"{PROXY_URL}/v1/nonexistent")
            assert False, "Should have raised"
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        _stop_proxy(proc)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /home/wunsch/git/voicemode-local
python -m pytest tests/test_piper_proxy.py -v
```

Expected: FAIL — `piper-proxy.py` doesn't exist yet.

- [ ] **Step 3: Write piper-proxy.py**

Create `piper-proxy.py`:

```python
#!/usr/bin/env python3
"""
Piper OpenAI-Compatible TTS Proxy

Exposes an OpenAI-compatible /v1/audio/speech endpoint backed by piper-tts.

Usage:
    python3 piper-proxy.py [--port 8881] [--voices-file voices/piper-voices.json]
"""

import argparse
import io
import json
import os
import subprocess
import tempfile
import wave
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path


class PiperProxyHandler(BaseHTTPRequestHandler):

    voices_config = {}
    models_dir = "models/piper"

    def do_POST(self):
        if self.path == "/v1/audio/speech":
            self._handle_speech()
        else:
            self.send_error(404, "Not Found")

    def do_GET(self):
        if self.path == "/health":
            self._json_response(200, {"status": "ok"})
        elif self.path == "/v1/models":
            self._json_response(200, {
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
            self._json_response(200, {
                "voices": self.voices_config.get("voices", [])
            })
        else:
            self.send_error(404, "Not Found")

    def _handle_speech(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        try:
            request = json.loads(body)
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return

        text = request.get("input", "")
        voice_id = request.get("voice", self.voices_config.get("default_voice", "p_de_thorsten"))

        if not text:
            self.send_error(400, "Missing 'input' field")
            return

        # Find the voice config
        voice_cfg = None
        for v in self.voices_config.get("voices", []):
            if v["id"] == voice_id:
                voice_cfg = v
                break

        if not voice_cfg:
            self.send_error(400, f"Unknown voice: {voice_id}")
            return

        piper_model = voice_cfg["piper_model"]
        model_path = os.path.join(self.models_dir, f"{piper_model}.onnx")

        # Download model if not present
        if not os.path.exists(model_path):
            if not self._download_model(piper_model, model_path):
                self.send_error(500, f"Failed to download model: {piper_model}")
                return

        # Generate speech with piper CLI
        try:
            audio_data = self._synthesize(text, model_path)
        except Exception as e:
            self.send_error(500, f"Synthesis error: {e}")
            return

        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(audio_data)))
        self.end_headers()
        self.wfile.write(audio_data)

    def _synthesize(self, text, model_path):
        """Run piper to synthesize text to WAV audio."""
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            proc = subprocess.run(
                ["piper", "--model", model_path, "--output_file", tmp_path],
                input=text.encode(),
                capture_output=True,
                timeout=30,
            )
            if proc.returncode != 0:
                raise RuntimeError(f"piper exited {proc.returncode}: {proc.stderr.decode()}")

            with open(tmp_path, "rb") as f:
                return f.read()
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def _download_model(self, piper_model, model_path):
        """Download a Piper model from Hugging Face."""
        os.makedirs(os.path.dirname(model_path), exist_ok=True)
        base_url = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0"

        # Piper model naming: language_REGION-name-quality
        # e.g., de_DE-thorsten-high -> de/de_DE/thorsten/high/
        parts = piper_model.split("-")
        lang_region = parts[0]  # e.g., de_DE
        lang = lang_region.split("_")[0]  # e.g., de
        name = parts[1] if len(parts) > 1 else "unknown"
        quality = parts[2] if len(parts) > 2 else "medium"

        onnx_url = f"{base_url}/{lang}/{lang_region}/{name}/{quality}/{piper_model}.onnx"
        json_url = f"{onnx_url}.json"

        import urllib.request
        import urllib.error

        try:
            print(f"[piper-proxy] Downloading model: {piper_model}")
            urllib.request.urlretrieve(onnx_url, model_path)
            urllib.request.urlretrieve(json_url, model_path + ".json")
            print(f"[piper-proxy] Model downloaded to {model_path}")
            return True
        except urllib.error.URLError as e:
            print(f"[piper-proxy] Download failed: {e}")
            return False

    def _json_response(self, code, data):
        response = json.dumps(data)
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response.encode())

    def log_message(self, format, *args):
        print(f"[piper-proxy] {args[0]}")


def main():
    parser = argparse.ArgumentParser(description="Piper OpenAI-compatible TTS proxy")
    parser.add_argument("--port", type=int, default=8881,
                        help="Port to listen on (default: 8881)")
    parser.add_argument("--voices-file", default="voices/piper-voices.json",
                        help="Path to curated voices JSON (default: voices/piper-voices.json)")
    parser.add_argument("--models-dir", default=None,
                        help="Directory for Piper models (default: from voices file)")
    args = parser.parse_args()

    # Load voice config
    voices_path = Path(args.voices_file)
    if voices_path.exists():
        with open(voices_path) as f:
            voices_config = json.load(f)
    else:
        print(f"[piper-proxy] WARNING: Voices file not found: {voices_path}")
        voices_config = {"voices": [], "default_voice": "p_de_thorsten", "models_dir": "models/piper"}

    PiperProxyHandler.voices_config = voices_config
    PiperProxyHandler.models_dir = args.models_dir or voices_config.get("models_dir", "models/piper")

    server = HTTPServer(("127.0.0.1", args.port), PiperProxyHandler)
    print(f"[piper-proxy] Listening on http://127.0.0.1:{args.port}")
    print(f"[piper-proxy] Voices loaded: {len(voices_config.get('voices', []))}")
    print(f"[piper-proxy] Models dir: {PiperProxyHandler.models_dir}")
    print(f"[piper-proxy] OpenAI endpoint: POST /v1/audio/speech")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[piper-proxy] Shutting down")
        server.server_close()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /home/wunsch/git/voicemode-local
python -m pytest tests/test_piper_proxy.py -v
```

Expected: All 4 tests pass (health, models, voices, 404).

Note: The `_handle_speech` endpoint requires `piper` CLI installed, so it's not tested here. Task 2 tests the server scaffolding; TTS synthesis is integration-tested manually.

- [ ] **Step 5: Commit**

```bash
git add piper-proxy.py tests/test_piper_proxy.py
git commit -m "feat: add piper-proxy with OpenAI-compatible TTS endpoints"
```

---

## Task 3: voicemode-switch — Add Piper Mode

**Files:**
- Modify: `voicemode-switch` (add `cmd_piper`, update `cmd_start`/`cmd_stop`/`cmd_status`)

- [ ] **Step 1: Add `cmd_piper` function**

Add after `cmd_hybrid()` (line 205 in `voicemode-switch`):

```bash
cmd_piper() {
    echo "=== Switching to PIPER mode ==="
    echo "  STT: Local Whisper (via proxy)"
    echo "  TTS: Local Piper (port 8881)"
    echo ""

    update_env "http://127.0.0.1:2022/v1" "http://127.0.0.1:8881/v1" "p_de_thorsten"

    echo ""
    warn "Restart Claude Code for changes to take effect"
    echo "  Make sure local services are running: voicemode-switch start"
}
```

- [ ] **Step 2: Add Piper proxy management to `cmd_start`**

In `cmd_start()`, after the existing Whisper proxy start block, add:

```bash
    echo ""
    echo "Starting Piper proxy..."
    PIPER_PID_FILE="/tmp/piper-proxy.pid"
    if [ -f "$PIPER_PID_FILE" ] && kill -0 "$(cat "$PIPER_PID_FILE")" 2>/dev/null; then
        warn "Piper proxy already running (PID $(cat "$PIPER_PID_FILE"))"
    else
        nohup python3 "$SCRIPT_DIR/piper-proxy.py" --port 8881 \
            --voices-file "$SCRIPT_DIR/voices/piper-voices.json" \
            --models-dir "$SCRIPT_DIR/models/piper" \
            > /tmp/piper-proxy.log 2>&1 &
        echo $! > "$PIPER_PID_FILE"
        sleep 1
        if kill -0 "$(cat "$PIPER_PID_FILE")" 2>/dev/null; then
            ok "Piper proxy started (PID $(cat "$PIPER_PID_FILE"))"
        else
            fail "Piper proxy failed to start — check /tmp/piper-proxy.log"
        fi
    fi
```

- [ ] **Step 3: Add Piper proxy stop to `cmd_stop`**

In `cmd_stop()`, after the existing proxy stop block, add:

```bash
    echo ""
    echo "Stopping Piper proxy..."
    PIPER_PID_FILE="/tmp/piper-proxy.pid"
    if [ -f "$PIPER_PID_FILE" ]; then
        kill "$(cat "$PIPER_PID_FILE")" 2>/dev/null && ok "Piper proxy stopped" || warn "Piper proxy was not running"
        rm -f "$PIPER_PID_FILE"
    else
        warn "No Piper proxy PID file found"
    fi
```

- [ ] **Step 4: Add Piper status to `cmd_status`**

In `cmd_status()`, after the existing proxy status checks, add:

```bash
    echo ""
    echo "Piper:"
    PIPER_PID_FILE="/tmp/piper-proxy.pid"
    if [ -f "$PIPER_PID_FILE" ] && kill -0 "$(cat "$PIPER_PID_FILE")" 2>/dev/null; then
        ok "Piper proxy is running (PID $(cat "$PIPER_PID_FILE"))"
    else
        fail "Piper proxy is not running"
    fi
    check_service "Piper Proxy (OpenAI-compat)" "http://127.0.0.1:8881/health" || true
```

- [ ] **Step 5: Add `piper` to the case statement**

Update the case statement at the bottom of `voicemode-switch`:

```bash
case "${1:-}" in
    local)  cmd_local ;;
    openai) cmd_openai ;;
    hybrid) cmd_hybrid ;;
    piper)  cmd_piper ;;
    status) cmd_status ;;
    start)  cmd_start ;;
    stop)   cmd_stop ;;
    *)
        echo "Usage: voicemode-switch <command>"
        echo ""
        echo "Commands:"
        echo "  local    Use local Whisper STT + Kokoro TTS (free, private)"
        echo "  piper    Use local Whisper STT + Piper TTS (free, multilingual)"
        echo "  openai   Use OpenAI for both STT and TTS (best quality)"
        echo "  hybrid   Use local Kokoro TTS + OpenAI STT (cheap, good quality)"
        echo "  status   Show current config and service health"
        echo "  start    Start local Docker services + proxies"
        echo "  stop     Stop local services"
        echo ""
        echo "After switching modes, restart Claude Code for changes to take effect."
        ;;
esac
```

- [ ] **Step 6: Test manually**

```bash
voicemode-switch piper
voicemode-switch status
```

Expected: Mode switches to piper, status shows Piper proxy state.

- [ ] **Step 7: Commit**

```bash
git add voicemode-switch
git commit -m "feat: add piper mode to voicemode-switch"
```

---

## Task 4: install.sh — Install Mode Selection & Piper Setup

**Files:**
- Modify: `install.sh`

- [ ] **Step 1: Add install mode prompt at the top (after OPENAI_KEY parsing)**

Insert after line 44 (after the `for arg` loop):

```bash
# ─── Install mode selection ─────────────────────────────────────────────────
header "Install mode"

CONFIG_DIR="$HOME/.voicemode-local"
CONFIG_FILE="$CONFIG_DIR/config"
mkdir -p "$CONFIG_DIR"

INSTALL_MODE=""
if command -v docker >/dev/null 2>&1; then
    echo "  Docker detected. How would you like to install voice services?"
    echo ""
    echo "    1) Docker (recommended) — uses Docker containers"
    echo "    2) Native — installs directly on your system"
    echo ""
    read -r -p "  Choice [1]: " install_choice
    install_choice="${install_choice:-1}"
else
    warn "Docker not found. Using native install mode."
    install_choice="2"
fi

if [ "$install_choice" = "2" ]; then
    INSTALL_MODE="native"
    ok "Install mode: native"
else
    INSTALL_MODE="docker"
    ok "Install mode: docker"
fi

# Ask about Piper
echo ""
read -r -p "  Install Piper TTS for multilingual voices (German, Dutch, etc)? [Y/n]: " piper_choice
piper_choice="${piper_choice:-y}"
if [[ "$piper_choice" =~ ^[Yy] ]]; then
    PIPER_ENABLED="true"
    ok "Piper TTS: enabled"
else
    PIPER_ENABLED="false"
    ok "Piper TTS: disabled"
fi

# Save config
cat > "$CONFIG_FILE" << EOF
INSTALL_MODE=$INSTALL_MODE
PIPER_ENABLED=$PIPER_ENABLED
EOF
ok "Config saved to $CONFIG_FILE"
```

- [ ] **Step 2: Add Piper install step (new step after Step 7)**

Add before the existing patch step:

```bash
# ─── Step 7b: Install Piper TTS ────────────────────────────────────────────
if [ "$PIPER_ENABLED" = "true" ]; then
    header "Step 7b: Piper TTS"

    if [ "$INSTALL_MODE" = "native" ]; then
        # Install piper-tts via pip
        if command -v pip3 >/dev/null 2>&1; then
            pip3 install --user piper-tts 2>&1 | tail -1
            ok "piper-tts installed"
        else
            fail "pip3 not found — install Python packages first"
        fi
    fi

    # Create models directory
    mkdir -p "$SCRIPT_DIR/models/piper"
    ok "Piper models directory created"

    # Start piper proxy
    echo "  Starting Piper proxy..."
    PIPER_PID_FILE="/tmp/piper-proxy.pid"
    if [ -f "$PIPER_PID_FILE" ] && kill -0 "$(cat "$PIPER_PID_FILE")" 2>/dev/null; then
        warn "Piper proxy already running"
    else
        nohup python3 "$SCRIPT_DIR/piper-proxy.py" --port 8881 \
            --voices-file "$SCRIPT_DIR/voices/piper-voices.json" \
            --models-dir "$SCRIPT_DIR/models/piper" \
            > /tmp/piper-proxy.log 2>&1 &
        echo $! > "$PIPER_PID_FILE"
        sleep 1
        if kill -0 "$(cat "$PIPER_PID_FILE")" 2>/dev/null; then
            ok "Piper proxy started"
        else
            fail "Piper proxy failed — check /tmp/piper-proxy.log"
        fi
    fi
fi
```

- [ ] **Step 3: Update the "Next steps" output to mention Piper**

Update the final output section to include `piper` in the mode list:

```bash
echo "    1. Switch mode:    voicemode-switch local|piper|hybrid|openai"
```

- [ ] **Step 4: Commit**

```bash
git add install.sh
git commit -m "feat: add install mode selection and Piper setup to install.sh"
```

---

## Task 5: docker-compose.yml — Optional Piper Profile

**Files:**
- Modify: `docker-compose.yml`

- [ ] **Step 1: Add Piper service with Docker Compose profile**

```yaml
services:
  whisper:
    image: onerahmet/openai-whisper-asr-webservice:latest
    container_name: voicemode-whisper
    ports:
      - "9000:9000"
    environment:
      - ASR_MODEL=base
      - ASR_ENGINE=faster_whisper
    restart: unless-stopped

  kokoro:
    image: ghcr.io/remsky/kokoro-fastapi-cpu:latest
    container_name: voicemode-kokoro
    ports:
      - "8880:8880"
    restart: unless-stopped

  piper:
    image: rhasspy/wyoming-piper:latest
    container_name: voicemode-piper
    ports:
      - "10200:10200"
    volumes:
      - ./models/piper:/data/models/piper
    command: --voice de_DE-thorsten-high
    restart: unless-stopped
    profiles:
      - piper
```

The `profiles: [piper]` means the Piper container only starts when explicitly requested with `docker compose --profile piper up -d`.

- [ ] **Step 2: Commit**

```bash
git add docker-compose.yml
git commit -m "feat: add optional Piper Docker service with compose profile"
```

---

## Task 6: Mode Switching MCP Tool Patch

**Files:**
- Create: `patches/switch_mode.py`
- Modify: `patches/apply.sh`

- [ ] **Step 1: Create the MCP tool patch**

Create `patches/switch_mode.py`:

```python
"""Mode switching tool for VoiceMode (patched by voicemode-local)."""

import json
import os
from voice_mode.server import mcp

MODES = {
    "local": {
        "description": "Local Whisper STT + Kokoro TTS (free, private)",
        "STT_BASE_URL": "http://127.0.0.1:2022/v1",
        "TTS_BASE_URL": "http://127.0.0.1:8880/v1",
        "TTS_VOICE": "af_sky",
    },
    "piper": {
        "description": "Local Whisper STT + Piper TTS (free, multilingual)",
        "STT_BASE_URL": "http://127.0.0.1:2022/v1",
        "TTS_BASE_URL": "http://127.0.0.1:8881/v1",
        "TTS_VOICE": "p_de_thorsten",
    },
    "openai": {
        "description": "OpenAI cloud STT + TTS (best quality, ~$0.01/min)",
        "STT_BASE_URL": "",
        "TTS_BASE_URL": "",
        "TTS_VOICE": "",
    },
    "hybrid": {
        "description": "OpenAI STT + local Kokoro TTS (~$0.006/min)",
        "STT_BASE_URL": "",
        "TTS_BASE_URL": "http://127.0.0.1:8880/v1",
        "TTS_VOICE": "af_sky",
    },
}


@mcp.tool()
def switch_mode(mode: str) -> str:
    """Switch VoiceMode between local, piper, openai, and hybrid modes.

    Changes the STT/TTS configuration in ~/.claude.json.
    After switching, Claude Code needs to reconnect the MCP server
    for the new settings to take effect.

    Args:
        mode: One of 'local', 'piper', 'openai', 'hybrid'
    """
    if mode not in MODES:
        available = ", ".join(MODES.keys())
        return f"Unknown mode: {mode}. Available modes: {available}"

    config = MODES[mode]
    claude_json = os.path.expanduser("~/.claude.json")

    try:
        with open(claude_json) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return f"Error: Could not read {claude_json}"

    env = data.get("mcpServers", {}).get("voicemode", {}).get("env", {})
    openai_key = env.get("OPENAI_API_KEY", "")

    new_env = {"OPENAI_API_KEY": openai_key}
    for key in ("STT_BASE_URL", "TTS_BASE_URL", "TTS_VOICE"):
        value = config[key]
        if value:
            new_env[key] = value

    data["mcpServers"]["voicemode"]["env"] = new_env

    with open(claude_json, "w") as f:
        json.dump(data, f, indent=2)

    return (
        f"Switched to {mode} mode: {config['description']}\n\n"
        f"Settings updated in ~/.claude.json.\n"
        f"Please restart Claude Code for changes to take effect."
    )
```

- [ ] **Step 2: Update patches/apply.sh to also patch tools**

Replace the content of `patches/apply.sh` with:

```bash
#!/bin/bash
# Apply voicemode-local patches to the installed voice_mode package.
# Portable: works on Linux/WSL, macOS, and Windows (Git Bash/WSL).
#
# Usage: ./patches/apply.sh [venv-path]
#   venv-path defaults to the .venv in this repo directory.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
VENV_DIR="${1:-$REPO_DIR/.venv}"

# Find the voice_mode package directory
VM_DIR=""
for pyver in "$VENV_DIR"/lib/python*/site-packages/voice_mode; do
    if [ -d "$pyver" ]; then
        VM_DIR="$pyver"
        break
    fi
done

# Also check Windows-style venv layout
if [ -z "$VM_DIR" ] && [ -d "$VENV_DIR/Lib/site-packages/voice_mode" ]; then
    VM_DIR="$VENV_DIR/Lib/site-packages/voice_mode"
fi

if [ -z "$VM_DIR" ]; then
    echo "[patches] ERROR: Could not find voice_mode in $VENV_DIR"
    exit 1
fi

PROMPTS_DIR="$VM_DIR/prompts"
TOOLS_DIR="$VM_DIR/tools"

# Apply converse prompt patch
if [ -f "$SCRIPT_DIR/converse.py" ]; then
    cp "$SCRIPT_DIR/converse.py" "$PROMPTS_DIR/converse.py"
    echo "[patches] Applied converse.py → $PROMPTS_DIR/converse.py"
fi

# Apply switch_mode tool patch
if [ -f "$SCRIPT_DIR/switch_mode.py" ] && [ -d "$TOOLS_DIR" ]; then
    cp "$SCRIPT_DIR/switch_mode.py" "$TOOLS_DIR/switch_mode.py"
    echo "[patches] Applied switch_mode.py → $TOOLS_DIR/switch_mode.py"
fi

echo "[patches] Done. Restart Claude Code for changes to take effect."
```

- [ ] **Step 3: Commit**

```bash
git add patches/switch_mode.py patches/apply.sh
git commit -m "feat: add mode switching MCP tool patch"
```

---

## Task 7: Converse Prompt — Piper Voices & Language Fallback

**Files:**
- Modify: `patches/converse.py`

- [ ] **Step 1: Update the converse prompt with Piper voices and routing**

Replace `patches/converse.py` with:

```python
"""Conversation prompts for voice interactions (patched by voicemode-local)."""

from voice_mode.server import mcp


@mcp.prompt()
def converse() -> str:
    """Have an ongoing two-way voice conversation with the user."""
    return """- You are in an ongoing two-way voice conversation with the user
- If this is a new conversation with no prior context, greet briefly and ask what they'd like to work on
- If continuing an existing conversation, acknowledge and continue from where you left off
- Use tools from voice-mode to converse
- End the chat when the user indicates they want to end it
- Keep your utterances brief unless a longer response is requested or necessary

## Voice Selection (on first message of a new conversation)

When starting a NEW voice conversation (no prior voice context), offer voice selection:

> "Starting voice mode. Want me to pick a random voice, or use the default? You can also name a specific voice."

- **Random voice**: Pick one from the random pool below (based on the active TTS provider)
- **Named voice**: Use it directly via the `voice` parameter
- **Default / "just start"**: Proceed without specifying a voice

Pass the selected voice as the `voice` parameter on every converse call for the session.
If the user asks to switch voice mid-conversation, change it on the next call.

## Voice Routing

Voices are identified by prefix. Use the correct `tts_provider` parameter based on the voice:

| Voice prefix | Engine | Notes |
|-------------|--------|-------|
| `af_`, `am_`, `bf_`, `bm_`, `ef_`, `em_`, `ff_`, `hf_`, `hm_`, `if_`, `im_`, `jf_`, `jm_`, `pf_`, `pm_`, `zf_`, `zm_` | Kokoro | Default local engine |
| `p_` | Piper | Multilingual engine (German, Dutch, etc.) — use tts_provider="kokoro" but route voice to port 8881 |
| `alloy`, `echo`, `fable`, `nova`, `onyx`, `shimmer` | OpenAI | Cloud TTS |

When switching between Kokoro and Piper voices, the user may need to use `/voicemode:switch-mode` to change the TTS endpoint, or you can suggest it.

## Language Fallback

If the user requests a language not available in the current TTS engine:
- Kokoro supports: English, French, Italian, Spanish, Hindi, Japanese, Portuguese, Chinese
- Piper supports: German, Dutch, Polish, Russian, Korean (and many others)
- OpenAI supports: Most languages

When a language is requested that the current engine can't handle, explicitly ask:
> "German isn't available in Kokoro. Want me to switch to Piper? I'd recommend p_de_thorsten, a natural-sounding German male voice."

Do NOT silently switch engines.

## Voices by TTS Provider

**Kokoro (local TTS)** — port 8880

Random pool (distinct-sounding): af_bella, af_heart, af_nova, am_adam, am_eric, am_puck, bf_emma, bm_daniel, bm_george, ff_siwis, if_sara, im_nicola

All voices:
- American: af_sky (default), af_bella, af_heart, af_jessica, af_nicole, af_nova, af_sarah, af_alloy, am_adam, am_echo, am_eric, am_michael, am_liam, am_puck, am_fenrir
- British: bf_alice, bf_emma, bf_lily, bm_daniel, bm_george, bm_lewis
- French: ff_siwis | Italian: if_sara, im_nicola | Spanish: ef_dora, em_alex
- Hindi: hf_alpha, hm_omega | Japanese: jf_alpha, jm_kumo
- Portuguese: pf_dora, pm_alex | Chinese: zf_xiaobei, zm_yunxi

**Piper (local TTS)** — port 8881

Curated high-quality voices for languages Kokoro doesn't cover:
- German: p_de_thorsten (male, high quality), p_de_eva (female)
- Dutch: p_nl_nathalie (female)
- Polish: p_pl_gosia (female)
- Russian: p_ru_dmitri (male)
- Korean: p_ko_hana (female)

**OpenAI TTS** — cloud fallback

Random pool: echo, fable, nova, onyx, shimmer
All voices: alloy (default), echo, fable, nova, onyx, shimmer

## Mode Switching

To switch between voice engines without leaving Claude Code, use `/voicemode:switch-mode`.
Available modes: local (Kokoro), piper, openai, hybrid. After switching, Claude Code needs to reconnect."""
```

- [ ] **Step 2: Apply the patch**

```bash
cd /home/wunsch/git/voicemode-local
./patches/apply.sh
```

- [ ] **Step 3: Commit**

```bash
git add patches/converse.py
git commit -m "feat: extend converse prompt with Piper voices, language fallback, mode switching"
```

---

## Task 8: Update CLAUDE.md & README.md

**Files:**
- Modify: `CLAUDE.md`
- Modify: `README.md`

- [ ] **Step 1: Update CLAUDE.md with Piper voices and services**

Add to the voice list section in `CLAUDE.md`:

```markdown
### Piper TTS (multilingual, port 8881)
- German: `p_de_thorsten` (M, high quality), `p_de_eva` (F)
- Dutch: `p_nl_nathalie` (F)
- Polish: `p_pl_gosia` (F)
- Russian: `p_ru_dmitri` (M)
- Korean: `p_ko_hana` (F)
```

Update the Services table:

```markdown
| Service | Port | Purpose |
|---------|------|---------|
| whisper-proxy | 2022 | Translates OpenAI-compatible STT to Whisper `/asr` |
| voicemode-whisper | 9000 | Whisper ASR (Docker or native) |
| voicemode-kokoro | 8880 | Kokoro TTS (Docker or native) |
| piper-proxy | 8881 | OpenAI-compatible TTS via Piper |
```

- [ ] **Step 2: Update README.md architecture diagram**

Update the architecture diagram to show the Piper proxy alongside Kokoro, add the Piper section to modes table, and document `voicemode-switch piper`.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md README.md
git commit -m "docs: update CLAUDE.md and README.md with Piper integration"
```

---

## Task 9: End-to-End Integration Test

- [ ] **Step 1: Start all services**

```bash
voicemode-switch start
```

Verify output shows Whisper, Kokoro, and Piper proxies all running.

- [ ] **Step 2: Test Piper proxy health**

```bash
curl -s http://127.0.0.1:8881/health
curl -s http://127.0.0.1:8881/v1/models
curl -s http://127.0.0.1:8881/v1/audio/voices
```

Expected: All return valid JSON.

- [ ] **Step 3: Test mode switching from CLI**

```bash
voicemode-switch piper
voicemode-switch status
voicemode-switch local
```

Verify config changes in `~/.claude.json`.

- [ ] **Step 4: Apply all patches**

```bash
./patches/apply.sh
```

Verify both `converse.py` and `switch_mode.py` are applied.

- [ ] **Step 5: Test TTS synthesis (requires piper CLI installed)**

```bash
# Install piper if not present
pip install piper-tts

# Test synthesis via proxy
curl -X POST http://127.0.0.1:8881/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{"input": "Hallo, ich bin Thorsten.", "voice": "p_de_thorsten"}' \
  --output /tmp/test-piper.wav

# Play if possible
aplay /tmp/test-piper.wav 2>/dev/null || echo "Audio saved to /tmp/test-piper.wav"
```

- [ ] **Step 6: Final commit with any fixes**

```bash
git add -A
git status  # Review what's staged
git commit -m "test: verify end-to-end Piper integration"
```
