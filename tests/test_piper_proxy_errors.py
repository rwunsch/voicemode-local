"""Tests for piper-proxy.py error handling paths.

Exercises POST /v1/audio/speech with invalid inputs. Requires the proxy
to be running but does NOT require the piper CLI (errors hit before synthesis).
"""

import json
import subprocess
import sys
import time
import urllib.request
import urllib.error
import pathlib

PROXY_SCRIPT = str(pathlib.Path(__file__).parent.parent / "piper-proxy.py")
VOICES_FILE = str(pathlib.Path(__file__).parent.parent / "voices" / "piper-voices.json")
TEST_PORT = 18882
BASE_URL = f"http://127.0.0.1:{TEST_PORT}"


START_TIMEOUT = 5.0


def _start_proxy():
    """Start the proxy in a subprocess and wait for it to accept connections. On any failure,
    a health check that times out included, the proxy is stopped before the error goes on."""
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
    try:
        deadline = time.time() + START_TIMEOUT
        while time.time() < deadline:
            try:
                urllib.request.urlopen(f"{BASE_URL}/health", timeout=1).close()
                return proc  # server is ready
            except OSError:  # URLError, refused, reset, TimeoutError: not up yet
                time.sleep(0.1)
        raise RuntimeError(f"piper-proxy did not start within {START_TIMEOUT:g} seconds on port {TEST_PORT}")
    except BaseException:
        _stop_proxy(proc)
        raise


def _stop_proxy(proc):
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _post_speech(payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/v1/audio/speech",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# ── Error paths (no piper CLI needed) ────────────────────────────────────


def test_missing_input_field():
    proc = _start_proxy()
    try:
        status, _ = _post_speech({"voice": "p_de_thorsten"})
        assert status == 400, f"Expected 400 for missing input, got {status}"
    finally:
        _stop_proxy(proc)


def test_empty_input_field():
    proc = _start_proxy()
    try:
        status, _ = _post_speech({"input": "", "voice": "p_de_thorsten"})
        assert status == 400, f"Expected 400 for empty input, got {status}"
    finally:
        _stop_proxy(proc)


def test_unknown_voice():
    proc = _start_proxy()
    try:
        status, _ = _post_speech({"input": "Hello", "voice": "nonexistent_voice"})
        assert status == 404, f"Expected 404 for unknown voice, got {status}"
    finally:
        _stop_proxy(proc)


def test_invalid_json_body():
    """Sending non-JSON body should return 400."""
    proc = _start_proxy()
    try:
        req = urllib.request.Request(
            f"{BASE_URL}/v1/audio/speech",
            data=b"this is not json",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                status = resp.status
        except urllib.error.HTTPError as e:
            status = e.code
        assert status == 400, f"Expected 400 for invalid JSON, got {status}"
    finally:
        _stop_proxy(proc)


def test_post_to_wrong_endpoint():
    proc = _start_proxy()
    try:
        req = urllib.request.Request(
            f"{BASE_URL}/v1/audio/wrong",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                status = resp.status
        except urllib.error.HTTPError as e:
            status = e.code
        assert status == 404, f"Expected 404, got {status}"
    finally:
        _stop_proxy(proc)
