"""Tests for whisper-proxy.py endpoints.

Mirrors the structure of test_piper_proxy.py — starts the proxy as a
subprocess and exercises the GET endpoints. POST /v1/audio/transcriptions
requires the Whisper backend, so only side-effect-free endpoints are tested.
"""

import json
import subprocess
import sys
import time
import urllib.request
import urllib.error
import pathlib

PROXY_SCRIPT = str(pathlib.Path(__file__).parent.parent / "whisper-proxy.py")
TEST_PORT = 12022
BASE_URL = f"http://127.0.0.1:{TEST_PORT}"


def _start_proxy():
    proc = subprocess.Popen(
        [
            sys.executable,
            PROXY_SCRIPT,
            "--port", str(TEST_PORT),
            "--whisper-url", "http://127.0.0.1:19999",  # fake backend, won't be called
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 5.0
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"{BASE_URL}/health", timeout=1)
            return proc
        except (urllib.error.URLError, ConnectionRefusedError):
            time.sleep(0.1)
    proc.terminate()
    raise RuntimeError(f"whisper-proxy did not start within 5 seconds on port {TEST_PORT}")


def _stop_proxy(proc):
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _get(path):
    req = urllib.request.Request(f"{BASE_URL}{path}")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, {}


def _post(path, body=None, content_type="application/json"):
    data = json.dumps(body).encode() if body else b""
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        headers={"Content-Type": content_type},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# ── GET endpoints ─────────────────────────────────────────────────────────


def test_health_endpoint():
    proc = _start_proxy()
    try:
        status, body = _get("/health")
        assert status == 200
        assert body == {"status": "ok"}
    finally:
        _stop_proxy(proc)


def test_models_endpoint():
    proc = _start_proxy()
    try:
        status, body = _get("/v1/models")
        assert status == 200
        assert body["object"] == "list"
        ids = [m["id"] for m in body["data"]]
        assert "whisper-1" in ids
    finally:
        _stop_proxy(proc)


def test_404_for_unknown_get():
    proc = _start_proxy()
    try:
        status, _ = _get("/nonexistent")
        assert status == 404
    finally:
        _stop_proxy(proc)


# ── POST endpoint error paths ────────────────────────────────────────────


def test_404_for_unknown_post():
    proc = _start_proxy()
    try:
        status, _ = _post("/nonexistent")
        assert status == 404
    finally:
        _stop_proxy(proc)


def test_transcription_rejects_non_multipart():
    """POST /v1/audio/transcriptions without multipart boundary returns 400."""
    proc = _start_proxy()
    try:
        status, _ = _post("/v1/audio/transcriptions", body={"file": "test"})
        assert status == 400
    finally:
        _stop_proxy(proc)
