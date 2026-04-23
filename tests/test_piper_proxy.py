"""
Tests for piper-proxy.py GET endpoints.

POST /v1/audio/speech requires the piper CLI to be installed; those tests are
skipped here. Only the side-effect-free GET endpoints are exercised.
"""

import json
import subprocess
import sys
import time
import urllib.request
import urllib.error
import pathlib
import os
import signal

# Path to the proxy script (one level up from this file)
PROXY_SCRIPT = str(pathlib.Path(__file__).parent.parent / "piper-proxy.py")
VOICES_FILE = str(pathlib.Path(__file__).parent.parent / "voices" / "piper-voices.json")
TEST_PORT = 18881
BASE_URL = f"http://127.0.0.1:{TEST_PORT}"


def _start_proxy():
    """Start the proxy in a subprocess and wait for it to accept connections."""
    proc = subprocess.Popen(
        [
            sys.executable,
            PROXY_SCRIPT,
            "--port", str(TEST_PORT),
            "--voices-file", VOICES_FILE,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Poll until the server is up (max ~5 s)
    deadline = time.time() + 5.0
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"{BASE_URL}/health", timeout=1)
            return proc  # server is ready
        except (urllib.error.URLError, ConnectionRefusedError):
            time.sleep(0.1)

    proc.terminate()
    raise RuntimeError(f"piper-proxy did not start within 5 seconds on port {TEST_PORT}")


def _stop_proxy(proc):
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _get(path):
    """Perform a GET request and return (status_code, parsed_json)."""
    req = urllib.request.Request(f"{BASE_URL}{path}")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, {}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_health_endpoint():
    proc = _start_proxy()
    try:
        status, body = _get("/health")
        assert status == 200, f"Expected 200, got {status}"
        assert body == {"status": "ok"}, f"Unexpected body: {body}"
    finally:
        _stop_proxy(proc)


def test_models_endpoint():
    proc = _start_proxy()
    try:
        status, body = _get("/v1/models")
        assert status == 200, f"Expected 200, got {status}"
        assert body.get("object") == "list", "Expected object=list"
        ids = [m["id"] for m in body.get("data", [])]
        assert "piper" in ids, f"'piper' model not found in {ids}"
    finally:
        _stop_proxy(proc)


def test_voices_endpoint():
    proc = _start_proxy()
    try:
        status, body = _get("/v1/audio/voices")
        assert status == 200, f"Expected 200, got {status}"
        assert body.get("object") == "list", "Expected object=list"
        voice_ids = [v["id"] for v in body.get("data", [])]
        assert "p_de_thorsten" in voice_ids, f"'p_de_thorsten' not found in {voice_ids}"
    finally:
        _stop_proxy(proc)


def test_404_for_unknown_path():
    proc = _start_proxy()
    try:
        status, _ = _get("/nonexistent/path")
        assert status == 404, f"Expected 404, got {status}"
    finally:
        _stop_proxy(proc)
